"""Score guideline compilation against the answer key: which rules were found, with the right values and clause,
and what the verifier stopped (invented clauses, quotes that are not in the text)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from lira.guidelines.compiler import CompileReport, compile_guidelines
from lira.guidelines.documents import GuidelineDocument
from lira.llm.router import ModelRouter


def _key(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r["kind"], r.get("event"), r.get("code"))


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, list) or isinstance(b, list):
        return sorted(map(str, a or [])) == sorted(map(str, b or []))
    if isinstance(a, int | float | Decimal | str) and isinstance(b, int | float | Decimal | str):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except Exception:
            return str(a) == str(b)
    return a == b


def score_ruleset(report: CompileReport, answer: list[dict[str, Any]]) -> dict[str, Any]:
    got = {}
    for r in report.ruleset.rules:
        d = r.model_dump(mode="json")
        got[_key(d)] = d
    want = {_key(a): a for a in answer}
    tp = [k for k in want if k in got]
    fn = [k for k in want if k not in got]
    fp = [k for k in got if k not in want]
    wrong_value, wrong_clause = [], []
    for k in tp:
        a, g = want[k], got[k]
        for p, v in a.items():
            if p in ("kind", "clause_id") or v is None:
                continue
            if not _same(v, g.get(p)):
                wrong_value.append({"rule": list(map(str, k)), "param": p, "expected": v, "got": g.get(p)})
        if a["clause_id"] != g["clause_id"]:
            wrong_clause.append({"rule": list(map(str, k)), "expected": a["clause_id"], "got": g["clause_id"]})
    n = len(want)
    return {
        "rules_expected": n,
        "found": len(tp),
        "missed": [list(map(str, k)) for k in fn],
        "extra": [list(map(str, k)) for k in fp],
        "recall": round(len(tp) / n, 4) if n else None,
        "precision": round(len(tp) / len(got), 4) if got else None,
        "wrong_values": wrong_value,
        "wrong_clauses": wrong_clause,
        "exact": len(tp) - len({w["rule"][0] for w in wrong_value} | {w["rule"][0] for w in wrong_clause}),
        "rejected_by_verifier": len(report.rejected),
        "disagreements_with_patterns": len(report.disagreements),
    }


async def evaluate(
    docs: list[tuple[GuidelineDocument, str]], answers: dict[str, list[dict[str, Any]]], router: ModelRouter
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    totals: dict[str, Any] = {"expected": 0, "found": 0, "extra": 0, "wrong_values": 0, "rejected_by_verifier": 0}
    for doc, name in docs:
        rep = await compile_guidelines(doc, router, name)
        s = score_ruleset(rep, answers[f"{doc.tenant}@v{doc.version}"])
        s["rejected"] = rep.rejected
        out[f"{doc.tenant}@v{doc.version}"] = s
        totals["expected"] += s["rules_expected"]
        totals["found"] += s["found"]
        totals["extra"] += len(s["extra"])
        totals["wrong_values"] += len(s["wrong_values"])
        totals["rejected_by_verifier"] += s["rejected_by_verifier"]
    totals["recall"] = round(totals["found"] / totals["expected"], 4) if totals["expected"] else None
    return {"documents": out, "totals": totals}
