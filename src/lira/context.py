"""Everything a review needs, loaded once per process: reference data, approved rules, models, retrieval index, router."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lira import config
from lira.guidelines.store import DocumentStore, RuleStore
from lira.llm.factory import make_router
from lira.llm.router import ModelRouter
from lira.nlp.models import NlpModels
from lira.reference import Reference
from lira.retrieval.index import Embedder, HashingEmbedder, HybridIndex, SpacyEmbedder, chunk


def default_embedder() -> Embedder:
    choice = os.environ.get("LIRA_EMBEDDER", "spacy")
    if choice == "hashing":
        return HashingEmbedder()
    if choice == "titan":
        import boto3

        from lira.retrieval.aws import TitanEmbedder

        return TitanEmbedder(boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-central-1")))
    return SpacyEmbedder()


@dataclass
class Context:
    ref: Reference
    docs: DocumentStore
    rules: RuleStore
    nlp: NlpModels
    index: HybridIndex
    router: ModelRouter

    @classmethod
    def load(
        cls, data: Path | None = None, models: Path | None = None, router: ModelRouter | None = None, embedder: Embedder | None = None
    ) -> Context:
        data = data or config.data_dir()
        models = models or config.models_dir()
        ctx = cls(
            ref=Reference.load(data / "world"),
            docs=DocumentStore(data / "world" / "guidelines"),
            rules=RuleStore(data / "rulesets"),
            nlp=NlpModels.load(models / "nlp"),
            index=HybridIndex(embedder or default_embedder()),
            router=router or make_router(),
        )
        ctx.index_approved()
        return ctx

    def index_approved(self) -> int:
        """Index the documents behind approved RuleSets (drafts are never searchable)."""
        n = 0
        for tenant in self.docs.tenants():
            for rs in self.rules.all(tenant):
                if rs.status == "approved":
                    chunks = chunk(self.docs.get(tenant, rs.version))
                    self.index.add(chunks)
                    n += len(chunks)
        return n
