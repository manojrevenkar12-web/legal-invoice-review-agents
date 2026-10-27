"""Retrieval and question answering over the guidelines, scored against the answer key.

Questions are written per rule kind in the words a reviewer or a firm would use (not the guideline's wording),
plus questions the guidelines do not answer. Metrics: recall@k and MRR of the clause that states the rule,
answer correctness (the rule's value appears in the answer), citation correctness, abstention on unanswerable
questions, and latency. Every question is asked of one tenant; a hit from another tenant would be a failure.
"""

from __future__ import annotations

import statistics
from typing import Any

from lira.llm.router import ModelRouter
from lira.retrieval.index import HybridIndex, retrieve
from lira.retrieval.qa import ask

QUESTIONS: dict[str, list[str]] = {
    "billing_increment": ["In what units must lawyers record their time?", "Can we bill in quarter hours?"],
    "max_daily_hours": ["Is there a limit on how many hours one lawyer can bill per day?"],
    "travel_time": ["How is non-working travel time paid?", "Will the client pay for time spent travelling to a deposition?"],
    "internal_conference": ["Can two of our associates both bill for an internal strategy meeting?"],
    "block_billing": ["What happens if several tasks are combined in one time entry?"],
    "vague_description": ["Will an entry that just says 'attention to file' be paid?"],
    "clerical_work": ["Can a paralegal bill for organising files and scheduling?"],
    "nonbillable_role": ["Can summer associates bill time on these matters?"],
    "nonreimbursable_expense": ["Are meals reimbursed?", "Will word processing charges be reimbursed?"],
    "expense_unit_cap": ["How much per page can we charge for photocopies?"],
    "submission_deadline": ["How long after the end of the month do we have to submit an invoice?"],
    "max_attendees": ["How many lawyers can bill for attending the same deposition?"],
    "research_cap": ["Is there a monthly limit on legal research hours?"],
    "budget": ["What if fees go over the approved budget?"],
}
UNANSWERABLE = ["Does the client pay a success fee if we win at trial?", "What is the client's policy on contingency fee arrangements?",
                "Which arbitration institution must be used for fee disputes?"]
VALUE_KEYS = ("hours", "rate_factor", "days", "max_timekeepers", "max_unit_cost", "hours_per_month", "reduce_factor")


def _value_forms(rule: dict[str, Any]) -> list[str]:
    for k in VALUE_KEYS:
        if rule.get(k) is not None:
            v = float(rule[k])
            forms = [f"{v:g}", f"{v:.2f}"]
            if k in ("rate_factor", "reduce_factor"):
                forms += [f"{int(round(v * 100))}%"] + (["not billable", "not be paid", "will not be paid", "not paid"] if v == 0 else [])
            if k == "max_timekeepers":
                forms.append({1: "one", 2: "two", 3: "three"}.get(int(v), ""))
            return [f for f in forms if f]
    return []


def retrieval_only(index: HybridIndex, answers: dict[str, list[dict[str, Any]]], k: int = 5) -> dict[str, Any]:
    ranks: list[int | None] = []
    leaks = 0
    for key, rules in answers.items():
        tenant, version = key.split("@v")
        for r in rules:
            for q in QUESTIONS.get(r["kind"], []):
                hits = retrieve(index, q, tenant, k=k, on=None)
                hits = [h for h in hits if h.chunk.version == int(version)] or retrieve(index, q, tenant, k=k)
                leaks += sum(h.chunk.tenant != tenant for h in hits)
                ids = [h.chunk.clause_id for h in hits]
                ranks.append(ids.index(r["clause_id"]) + 1 if r["clause_id"] in ids else None)
    n = len(ranks)
    return {"questions": n, "recall@1": round(sum(1 for x in ranks if x == 1) / n, 4), "recall@3": round(sum(1 for x in ranks if x and x <= 3) / n, 4),
            f"recall@{k}": round(sum(1 for x in ranks if x) / n, 4), "mrr": round(sum(1 / x for x in ranks if x) / n, 4),
            "cross_tenant_hits": leaks}


async def qa(index: HybridIndex, router: ModelRouter, answers: dict[str, list[dict[str, Any]]], names: dict[str, str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latest: dict[str, str] = {}
    for key in answers:
        t, v = key.split("@v")
        if t not in latest or int(v) > int(latest[t].split("@v")[1]):
            latest[t] = key
    for t, key in latest.items():
        for r in answers[key]:
            for q in QUESTIONS.get(r["kind"], []):
                res = await ask(index, router, t, names[t], q)
                forms = _value_forms(r)
                value_ok = (not forms) or any(f.lower() in res.answer.lower() for f in forms)
                rows.append({"tenant": t, "kind": r["kind"], "question": q, "answerable": True, "answered": res.answerable,
                             "cited_right_clause": r["clause_id"] in res.citations, "value_in_answer": value_ok and res.answerable,
                             "verified": res.verified, "problems": res.problems, "latency_ms": res.latency_ms})
        for q in UNANSWERABLE:
            res = await ask(index, router, t, names[t], q)
            rows.append({"tenant": t, "kind": None, "question": q, "answerable": False, "answered": res.answerable,
                         "cited_right_clause": False, "value_in_answer": False, "verified": res.verified, "problems": res.problems,
                         "latency_ms": res.latency_ms})
    ans = [r for r in rows if r["answerable"]]
    un = [r for r in rows if not r["answerable"]]
    lat = sorted(r["latency_ms"] for r in rows)
    return {
        "questions": len(rows),
        "answered_with_right_clause": round(sum(r["answered"] and r["cited_right_clause"] for r in ans) / len(ans), 4),
        "correct_value": round(sum(r["value_in_answer"] for r in ans) / len(ans), 4),
        "abstained_when_unanswerable": round(sum(not r["answered"] for r in un) / len(un), 4) if un else None,
        "unsupported_answers_shown": sum(1 for r in rows if r["answered"] and not r["verified"]),
        "blocked_by_citation_check": sum(1 for r in rows if r["problems"] and "unavailable" not in " ".join(r["problems"])),
        "latency_ms_p50": round(statistics.median(lat), 1) if lat else None,
        "latency_ms_p95": round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else None,
        "failures": [r for r in ans if not (r["answered"] and r["cited_right_clause"])][:10],
    }
