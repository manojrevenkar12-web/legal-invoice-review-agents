"""AWS retrieval back ends with the same contract as the in-process index.

* OpenSearch Serverless (vector search collection): k-NN + BM25 in one hybrid query, scores combined by a
  search pipeline's normalization processor. The tenant filter sits in both sub-queries.
* Bedrock Knowledge Bases: Retrieve with HYBRID search and a metadata filter on tenant.
* Bedrock Rerank: rerank candidates with a reranking model.
* Titan Text Embeddings v2 for production embeddings.
Request shapes are validated against the botocore service models in tests (tests/test_aws_retrieval.py).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np

from lira.retrieval.index import Chunk, Hit

INDEX_MAPPING: dict[str, Any] = {
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "tenant": {"type": "keyword"},
            "version": {"type": "integer"},
            "effective_from": {"type": "date"},
            "clause_id": {"type": "keyword"},
            "breadcrumb": {"type": "text"},
            "text": {"type": "text", "analyzer": "english"},
            "embedding": {
                "type": "knn_vector",
                "dimension": 1024,
                "method": {
                    "name": "hnsw",
                    "engine": "faiss",
                    "space_type": "innerproduct",
                    "parameters": {"ef_construction": 128, "m": 16},
                },
            },
        }
    },
}

SEARCH_PIPELINE: dict[str, Any] = {
    "description": "Hybrid BM25 + k-NN for guideline clauses",
    "phase_results_processors": [
        {
            "normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {"technique": "arithmetic_mean", "parameters": {"weights": [0.4, 0.6]}},
            }
        }
    ],
}


def hybrid_query(query: str, vector: Sequence[float], tenant: str, version: int | None, k: int) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [{"term": {"tenant": tenant}}]
    if version is not None:
        filters.append({"term": {"version": version}})
    return {
        "size": k,
        "_source": {"excludes": ["embedding"]},
        "query": {
            "hybrid": {
                "queries": [
                    {"bool": {"must": [{"multi_match": {"query": query, "fields": ["text", "breadcrumb^0.5"]}}], "filter": filters}},
                    {"knn": {"embedding": {"vector": list(map(float, vector)), "k": k, "filter": {"bool": {"filter": filters}}}}},
                ]
            }
        },
    }


class OpenSearchIndex:
    def __init__(self, client: Any, embedder: Any, index: str = "guideline-clauses", pipeline: str = "clauses-hybrid"):
        self.client, self.embedder, self.index, self.pipeline = client, embedder, index, pipeline

    def ensure(self) -> None:
        if not self.client.indices.exists(index=self.index):
            self.client.indices.create(index=self.index, body=INDEX_MAPPING)
        self.client.transport.perform_request("PUT", f"/_search/pipeline/{self.pipeline}", body=SEARCH_PIPELINE)

    def add(self, chunks: Sequence[Chunk]) -> None:
        vecs = self.embedder.embed([c.embed_text for c in chunks])
        body: list[dict[str, Any]] = []
        for c, v in zip(chunks, vecs, strict=True):
            body.append({"index": {"_index": self.index, "_id": c.id}})
            body.append(
                {
                    "tenant": c.tenant,
                    "version": c.version,
                    "effective_from": c.effective_from.isoformat(),
                    "clause_id": c.clause_id,
                    "breadcrumb": c.breadcrumb,
                    "text": c.text,
                    "embedding": [float(x) for x in v],
                }
            )
        self.client.bulk(body=body)

    def search(self, query: str, tenant: str, k: int = 5, on: date | None = None, version: int | None = None) -> list[Hit]:
        if not tenant:
            raise ValueError("tenant is required")
        vec = self.embedder.embed([query])[0]
        resp = self.client.search(
            index=self.index, body=hybrid_query(query, vec, tenant, version, k), params={"search_pipeline": self.pipeline}
        )
        out = []
        for h in resp["hits"]["hits"]:
            s = h["_source"]
            if s["tenant"] != tenant:  # defence in depth: never return another tenant's clause
                continue
            c = Chunk(
                h["_id"],
                s["tenant"],
                int(s["version"]),
                date.fromisoformat(s["effective_from"]),
                s["clause_id"],
                s["text"],
                s.get("breadcrumb", ""),
            )
            out.append(Hit(c, float(h["_score"])))
        return out


class TitanEmbedder:
    name = "amazon.titan-embed-text-v2:0"
    dim = 1024

    def __init__(self, client: Any):
        self.client = client

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = []
        for t in texts:
            r = self.client.invoke_model(
                modelId=self.name,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({"inputText": t[:8000], "dimensions": self.dim, "normalize": True}),
            )
            out.append(json.loads(r["body"].read())["embedding"])
        return np.array(out, dtype=np.float32)


class BedrockKnowledgeBase:
    """Retrieve from a Bedrock Knowledge Base whose documents carry tenant/version/clause_id metadata."""

    def __init__(self, client: Any, knowledge_base_id: str):
        self.client, self.kb = client, knowledge_base_id

    def request(self, query: str, tenant: str, k: int, version: int | None = None) -> dict[str, Any]:
        f: dict[str, Any] = {"equals": {"key": "tenant", "value": tenant}}
        if version is not None:
            f = {"andAll": [f, {"equals": {"key": "version", "value": version}}]}
        return {
            "knowledgeBaseId": self.kb,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": k, "overrideSearchType": "HYBRID", "filter": f}},
        }

    def search(self, query: str, tenant: str, k: int = 5, on: date | None = None, version: int | None = None) -> list[Hit]:
        if not tenant:
            raise ValueError("tenant is required")
        resp = self.client.retrieve(**self.request(query, tenant, k, version))
        out = []
        for r in resp.get("retrievalResults", []):
            md = r.get("metadata", {})
            if md.get("tenant") != tenant:
                continue
            c = Chunk(
                str(r.get("location", {}).get("s3Location", {}).get("uri", "")),
                tenant,
                int(md.get("version", 0)),
                date.fromisoformat(str(md.get("effective_from", "1970-01-01"))),
                str(md.get("clause_id", "")),
                r["content"]["text"],
                str(md.get("breadcrumb", "")),
            )
            out.append(Hit(c, float(r.get("score", 0.0))))
        return out


class BedrockReranker:
    name = "bedrock-rerank"

    def __init__(self, client: Any, model_arn: str):
        self.client, self.model_arn = client, model_arn

    def request(self, query: str, hits: list[Hit], k: int) -> dict[str, Any]:
        return {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": [
                {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": h.chunk.embed_text}}} for h in hits
            ],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {"numberOfResults": min(k, len(hits)), "modelConfiguration": {"modelArn": self.model_arn}},
            },
        }

    def rerank(self, query: str, hits: list[Hit], k: int) -> list[Hit]:
        if not hits:
            return []
        resp = self.client.rerank(**self.request(query, hits, k))
        return [Hit(hits[r["index"]].chunk, float(r["relevanceScore"])) for r in resp["results"]]
