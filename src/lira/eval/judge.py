"""LLM-as-judge for proposed findings, and the human-evaluation protocol that keeps the judge honest.

Protocol (evals/annotation/PROTOCOL.md):
1. A stratified sample of proposed findings is drawn once per round and pinned (items.jsonl with its sha256).
2. Two annotators grade every item blind (no source, no confidence, shuffled order) on the judge's rubric.
3. Agreement between annotators (Cohen's kappa per criterion) is the ceiling: a judge is not trusted on a criterion
   where people do not agree with each other. Disagreements go to an adjudicator.
4. The judge grades the same items; its agreement with the adjudicated labels is reported per criterion, and it
   replaces people only on criteria where judge-vs-people kappa is close to people-vs-people kappa.
5. Bias checks: the judge's verdict must not change when the explanation is padded with irrelevant text.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from lira.llm.base import Block, Message, ModelRequest, ModelResponse
from lira.llm.prompts import load
from lira.llm.router import ModelRouter, Unavailable
from lira.llm.schemas import RECORD_VERDICT, Verdict

CRITERIA = ("supported", "correct", "specific", "professional", "overall")
PADDING = (" We remain committed to a constructive working relationship and appreciate the effort your team puts into every"
           " matter, and we hope this note is received in the spirit in which it is intended.")


def sample_items(runs: list[Any], clause_text: dict[tuple[str, str], str], n: int = 60, seed: int = 5) -> list[dict[str, Any]]:
    """Stratified by finding kind and source; one item per finding; deterministic for a seed."""
    pool: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in runs:
        inv = run.invoice
        for f in run.findings:
            if f.status != "proposed" or not f.line_no or f.kind.value not in (
                    "block_billing", "vague_description", "clerical_work", "internal_conference", "travel_time"):
                continue
            li = inv.line(f.line_no)
            tk = f.clause.clause_id if f.clause else ""
            pool.setdefault((f.kind.value, f.source), []).append({
                "id": hashlib.sha1(f.id.encode()).hexdigest()[:10], "finding_id": f.id, "kind": f.kind.value, "action": f.action,
                "clause_id": tk, "clause_text": clause_text.get((inv.number, f.id), f.clause.quote if f.clause else ""),
                "line_no": li.line_no, "timekeeper_role": li.timekeeper_class, "hours": str(li.units), "description": li.description,
                "explanation": f.explanation,
            })
    rng = random.Random(seed)
    strata = sorted(pool)
    out: list[dict[str, Any]] = []
    while len(out) < n and any(pool[s] for s in strata):
        for s in strata:
            if pool[s] and len(out) < n:
                out.append(pool[s].pop(rng.randrange(len(pool[s]))))
    rng.shuffle(out)
    return out


async def judge_item(router: ModelRouter, item: dict[str, Any], explanation: str | None = None) -> dict[str, str] | None:
    prompt = load("judge_finding")
    user = prompt.render_user(clause_id=item["clause_id"], clause_text=item["clause_text"], line_no=item["line_no"],
                              timekeeper_role=item["timekeeper_role"], hours=item["hours"], description=item["description"],
                              finding_kind=item["kind"], action=item["action"], explanation=explanation or item["explanation"])
    req = ModelRequest(task="judge_finding", system=prompt.system, messages=[Message("user", [Block.t(user)])], tools=[RECORD_VERDICT],
                       tool_choice="record_verdict", max_tokens=600, prompt_id=prompt.id, prompt_version=prompt.version)

    def parse(r: ModelResponse) -> Verdict:
        return Verdict.model_validate(r.tool_input("record_verdict"))

    try:
        routed = await router.call(req, parse)
    except Unavailable:
        return None
    return routed.value.model_dump()


def kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for two raters on the same items."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 4) if pe < 1 else (1.0 if po == 1 else 0.0)


def agreement(items: list[dict[str, Any]], a: dict[str, dict[str, str]], b: dict[str, dict[str, str]],
              judge: dict[str, dict[str, str]], adjudicated: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    ids = [i["id"] for i in items if i["id"] in a and i["id"] in b]
    out: dict[str, Any] = {"items": len(ids)}
    for c in CRITERIA:
        la, lb = [a[i][c] for i in ids], [b[i][c] for i in ids]
        gold = {i: (adjudicated or {}).get(i, {}).get(c) or (a[i][c] if a[i][c] == b[i][c] else None) for i in ids}
        settled = [i for i in ids if gold[i] is not None and i in judge]
        out[c] = {
            "annotators_kappa": kappa(la, lb),
            "annotators_agree": round(sum(x == y for x, y in zip(la, lb, strict=True)) / max(1, len(ids)), 4),
            "judge_vs_gold_kappa": kappa([judge[i][c] for i in settled], [gold[i] for i in settled]),  # type: ignore[misc]
            "judge_vs_gold_agree": round(sum(judge[i][c] == gold[i] for i in settled) / max(1, len(settled)), 4),
            "settled_items": len(settled),
            "unsettled_items": sum(1 for i in ids if gold[i] is None),
        }
    return out


def load_labels(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = {c: r[c] for c in CRITERIA if c in r}
    return out


async def run_judge(router: ModelRouter, items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    verdicts: dict[str, dict[str, str]] = {}
    flips = 0
    for it in items:
        v = await judge_item(router, it)
        if v is None:
            continue
        verdicts[it["id"]] = v
        padded = await judge_item(router, it, it["explanation"] + PADDING)
        if padded is not None and padded["overall"] != v["overall"]:
            flips += 1
    return verdicts, {"length_padding_flips": flips, "judged": len(verdicts)}
