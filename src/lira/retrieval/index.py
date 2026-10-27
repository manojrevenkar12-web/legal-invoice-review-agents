"""Retrieval over guideline clauses: chunking, embeddings, a hybrid (BM25 + dense) index, and reranking.

The in-process index is what tests, CI and the demo use. In AWS the same interface is served by OpenSearch
Serverless (``retrieval.opensearch``) or a Bedrock Knowledge Base (``retrieval.knowledge_base``). Every search
takes a tenant and filters on it before scoring: a firm's question about one client can never surface another
client's guidelines.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np

from lira.guidelines.documents import GuidelineDocument

_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "to",
        "and",
        "or",
        "in",
        "on",
        "for",
        "by",
        "with",
        "is",
        "are",
        "be",
        "will",
        "may",
        "must",
        "not",
        "any",
        "all",
        "this",
        "that",
        "these",
        "those",
        "our",
        "we",
        "you",
        "your",
        "it",
        "its",
        "as",
        "at",
        "from",
        "per",
        "such",
        "shall",
        "each",
        "other",
        "than",
        "which",
        "who",
        "whom",
        "what",
        "when",
        "where",
        "how",
        "does",
        "do",
        "can",
    ]
)


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOP]


@dataclass(frozen=True)
class Chunk:
    id: str  # "<tenant>/v<version>#<clause_id>[:<part>]"
    tenant: str
    version: int
    effective_from: date
    clause_id: str
    text: str
    breadcrumb: str

    @property
    def embed_text(self) -> str:
        return f"{self.breadcrumb}: {self.text}" if self.breadcrumb else self.text


def chunk(doc: GuidelineDocument, max_words: int = 160, overlap: int = 30) -> list[Chunk]:
    """One chunk per clause; long clauses are split on sentence boundaries with overlap. Headings ride along."""
    out: list[Chunk] = []
    for c in doc.clauses:
        ws = c.text.split()
        if len(ws) <= max_words:
            out.append(
                Chunk(f"{doc.tenant}/v{doc.version}#{c.id}", doc.tenant, doc.version, doc.effective_from, c.id, c.text, c.breadcrumb)
            )
            continue
        sentences = re.split(r"(?<=[.;])\s+", c.text)
        part, buf = 0, list[str]()
        for s in sentences:
            if buf and len(" ".join([*buf, s]).split()) > max_words:
                part += 1
                out.append(
                    Chunk(
                        f"{doc.tenant}/v{doc.version}#{c.id}:{part}",
                        doc.tenant,
                        doc.version,
                        doc.effective_from,
                        c.id,
                        " ".join(buf),
                        c.breadcrumb,
                    )
                )
                tail = " ".join(buf).split()[-overlap:]
                buf = [" ".join(tail)]
            buf.append(s)
        part += 1
        out.append(
            Chunk(
                f"{doc.tenant}/v{doc.version}#{c.id}:{part}", doc.tenant, doc.version, doc.effective_from, c.id, " ".join(buf), c.breadcrumb
            )
        )
    return out


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


class SpacyEmbedder:
    """Averaged word vectors from en_core_web_md (300 dimensions). Local, free, and good enough for short clauses;
    Bedrock Titan Text Embeddings v2 is the production embedder."""

    name = "spacy-en_core_web_md"
    dim = 300

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        from lira.nlp.entities import nlp

        vecs = []
        for doc in nlp().pipe([" ".join(words(t)) for t in texts], batch_size=64):
            v = doc.vector
            n = float(np.linalg.norm(v))
            vecs.append(v / n if n else v)
        return np.array(vecs, dtype=np.float32).reshape(len(texts), self.dim)


class HashingEmbedder:
    """Feature hashing of words and bigrams: a dependency-free embedder for unit tests."""

    name = "hashing-512"
    dim = 512

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            ws = words(t)
            for tok in ws + [a + "_" + b for a, b in itertools.pairwise(ws)]:
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 9) & 1 else -1.0
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25_rank: int | None = None
    dense_rank: int | None = None


@dataclass
class HybridIndex:
    embedder: Embedder
    k1: float = 1.2
    b: float = 0.75
    rrf_k: int = 60
    chunks: list[Chunk] = field(default_factory=list)
    _tf: list[Counter[str]] = field(default_factory=list)
    _len: list[int] = field(default_factory=list)
    _df: Counter[str] = field(default_factory=Counter)
    _vecs: np.ndarray | None = None

    def add(self, chunks: Sequence[Chunk]) -> None:
        new = [c for c in chunks if c.id not in {x.id for x in self.chunks}]
        if not new:
            return
        for c in new:
            tf = Counter(words(c.embed_text))
            self.chunks.append(c)
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            self._df.update(tf.keys())
        v = self.embedder.embed([c.embed_text for c in new])
        self._vecs = v if self._vecs is None else np.vstack([self._vecs, v])

    def _bm25(self, q: list[str], ids: list[int]) -> dict[int, float]:
        n = len(self.chunks)
        avg = sum(self._len) / max(1, n)
        out: dict[int, float] = {}
        for i in ids:
            s = 0.0
            tf = self._tf[i]
            for w in q:
                if w not in tf:
                    continue
                idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
                s += idf * tf[w] * (self.k1 + 1) / (tf[w] + self.k1 * (1 - self.b + self.b * self._len[i] / avg))
            if s > 0:
                out[i] = s
        return out

    def search(self, query: str, tenant: str, k: int = 5, on: date | None = None, versions: set[int] | None = None) -> list[Hit]:
        """Hybrid search within one tenant. ``on`` keeps the document version in force on that date (latest effective <= on)."""
        if not tenant:
            raise ValueError("tenant is required: retrieval never searches across clients")
        ids = [i for i, c in enumerate(self.chunks) if c.tenant == tenant and (versions is None or c.version in versions)]
        if on is not None:
            eligible = [c.version for c in (self.chunks[i] for i in ids) if c.effective_from <= on]
            if eligible:
                current = max(eligible)
                ids = [i for i in ids if self.chunks[i].version == current]
        if not ids:
            return []
        bm = self._bm25(words(query), ids)
        assert self._vecs is not None
        qv = self.embedder.embed([query])[0]
        dense = {i: float(self._vecs[i] @ qv) for i in ids}
        bm_rank = {i: r for r, i in enumerate(sorted(bm, key=lambda i: -bm[i]), start=1)}
        de_rank = {i: r for r, i in enumerate(sorted(dense, key=lambda i: -dense[i]), start=1)}
        fused = {i: (1 / (self.rrf_k + bm_rank[i]) if i in bm_rank else 0.0) + 1 / (self.rrf_k + de_rank[i]) for i in ids}
        best = sorted(fused, key=lambda i: -fused[i])[:k]
        return [Hit(self.chunks[i], fused[i], bm_rank.get(i), de_rank.get(i)) for i in best]


class LexicalReranker:
    """Rerank by how much of the query each candidate covers (with the clause heading), then by fused score.
    Production uses the Bedrock Rerank API (``retrieval.knowledge_base.BedrockReranker``) with the same interface."""

    name = "lexical-coverage"

    def rerank(self, query: str, hits: list[Hit], k: int) -> list[Hit]:
        q = set(words(query))
        if not q:
            return hits[:k]

        def cov(h: Hit) -> float:
            w = set(words(h.chunk.embed_text))
            return len(q & w) / len(q)

        return sorted(hits, key=lambda h: -(cov(h) + 30 * h.score))[:k]


def retrieve(
    index: HybridIndex, query: str, tenant: str, k: int = 4, on: date | None = None, reranker: Any = None, candidates: int = 12
) -> list[Hit]:
    hits = index.search(query, tenant, k=candidates, on=on)
    return (reranker or LexicalReranker()).rerank(query, hits, k)
