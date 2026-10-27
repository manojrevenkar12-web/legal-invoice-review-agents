"""Publish approved guideline clauses to the Bedrock Knowledge Base data source (S3), one object per clause with
metadata the retrieval filter uses (tenant, version, clause_id, effective_from), then start an ingestion job.
Drafts are never published: only an approved RuleSet's document becomes searchable."""

from __future__ import annotations

import json
from typing import Any

from lira.guidelines.documents import GuidelineDocument
from lira.retrieval.index import chunk


def objects(doc: GuidelineDocument) -> list[tuple[str, bytes, bytes]]:
    out = []
    for c in chunk(doc):
        key = f"approved/{doc.tenant}/v{doc.version}/{c.id.split('#', 1)[1].replace('/', '_')}.txt"
        meta = {
            "metadataAttributes": {
                "tenant": doc.tenant,
                "version": doc.version,
                "clause_id": c.clause_id,
                "effective_from": doc.effective_from.isoformat(),
                "breadcrumb": c.breadcrumb,
            }
        }
        out.append((key, c.text.encode(), json.dumps(meta).encode()))
    return out


def publish(
    doc: GuidelineDocument,
    s3: Any,
    bucket: str,
    agent: Any | None = None,
    knowledge_base_id: str | None = None,
    data_source_id: str | None = None,
) -> dict[str, Any]:
    keys = []
    for key, body, meta in objects(doc):
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
        s3.put_object(Bucket=bucket, Key=key + ".metadata.json", Body=meta, ContentType="application/json")
        keys.append(key)
    job = None
    if agent and knowledge_base_id and data_source_id:
        job = agent.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id, description=f"{doc.tenant} guidelines v{doc.version} approved"
        )["ingestionJob"]["ingestionJobId"]
    return {"objects": len(keys), "ingestion_job": job}
