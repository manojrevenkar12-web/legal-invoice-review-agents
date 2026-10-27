"""Export evaluation sets in Amazon Bedrock Evaluations formats, so the same cases can be graded by Bedrock's
LLM-as-a-judge jobs (bring-your-own-inference) next to our own judge.

* RAG (retrieve-and-generate) dataset: conversationTurns with referenceResponses and our output with retrievedPassages.
* Model-evaluation dataset for proposed findings: prompt, referenceResponse, category, modelResponses.
* A custom metric definition ("grounded_in_clause") for create_evaluation_job's customMetricConfig.
Limits from the service: 1000 prompts per job and one modelIdentifier per job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_PROMPTS = 1000

GROUNDED_METRIC: dict[str, Any] = {
    "customMetricDefinition": {
        "name": "grounded_in_clause",
        "instructions": (
            "You are grading an automated legal invoice reviewer. The prompt contains a billing-guideline clause and a "
            "time entry; the response is the reviewer's finding and explanation.\n\nPrompt: {{prompt}}\nResponse: {{prediction}}\n"
            "Reference: {{ground_truth}}\n\nDoes the cited clause state the rule the finding applies, and would a careful "
            "reviewer applying that clause reach the same decision about this entry?"
        ),
        "ratingScale": [
            {"definition": "Grounded and correct", "value": {"floatValue": 1}},
            {"definition": "Grounded but the decision is debatable", "value": {"floatValue": 0.5}},
            {"definition": "Not grounded in the clause, or wrong", "value": {"floatValue": 0}},
        ],
    }
}


def rag_records(qa_rows: list[dict[str, Any]], kb_identifier: str = "lira-guidelines-hybrid") -> list[dict[str, Any]]:
    out = []
    for r in qa_rows[:MAX_PROMPTS]:
        out.append(
            {
                "conversationTurns": [
                    {
                        "prompt": {"content": [{"text": r["question"]}]},
                        "referenceResponses": [{"content": [{"text": r.get("reference", "")}]}],
                        "output": {
                            "text": r["answer"],
                            "modelIdentifier": r.get("model", "lira-qa"),
                            "knowledgeBaseIdentifier": kb_identifier,
                            "retrievedPassages": {
                                "retrievalResults": [
                                    {"name": p["clause_id"], "content": {"text": p["text"]}, "metadata": {"clause_id": p["clause_id"]}}
                                    for p in r.get("passages", [])
                                ]
                            },
                        },
                    }
                ]
            }
        )
    return out


def finding_records(items: list[dict[str, Any]], model_identifier: str = "lira-line-review") -> list[dict[str, Any]]:
    out = []
    for it in items[:MAX_PROMPTS]:
        prompt = (
            f"Guideline clause {it['clause_id']}: {it['clause_text']}\nEntry (line {it['line_no']}, {it['timekeeper_role']}, "
            f"{it['hours']} h): {it['description']}"
        )
        rec: dict[str, Any] = {
            "prompt": prompt,
            "category": it["kind"],
            "modelResponses": [{"response": f"{it['kind']} -> {it['action']}. {it['explanation']}", "modelIdentifier": model_identifier}],
        }
        if it.get("reference"):
            rec["referenceResponse"] = it["reference"]
        out.append(rec)
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
