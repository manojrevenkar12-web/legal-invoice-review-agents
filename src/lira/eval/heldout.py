"""Blind held-out sets: written by someone who has not seen the code, run once, and the first run kept.

Two kinds of set live under ``heldout/<set>/``:

* a guideline set (``guidelines.md`` + ``answer_key.yaml``): a client's billing guidelines and the rules a
  legal-operations analyst would record from them. It measures the guideline compiler on wording it was not
  built against.
* an entries set (``entries.yaml``): time entries with the judgement a reviewer would make on each
  (block billing, vague description, clerical work). Each entry is billed as its own LEDES invoice on a
  Kestrel matter and goes through the full review graph, so it measures what a reviewer would actually see.

The first run of a set is written to ``reports/heldout/<set>/first_run.json`` together with the sha256 of the
set's files, and is never rewritten. Later runs go to ``latest.json``. A set whose files changed after its
first run is refused: a held-out set that has been edited is no longer the set that was measured. If someone
studies a set's failures and changes code because of them, ``heldout/<set>/STATUS`` becomes ``regression``
and a new blind set is written from the guides in ``heldout/`` only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from lira import config, ledes
from lira.context import Context
from lira.domain import Invoice, LineItem, LineType, money

JUDGED = ("block_billing", "vague_description", "clerical_work")
TENANT, MATTER = "kestrel", "KES-2026-004"  # a matter whose firm, rates and task codes cover ordinary litigation work


class SetChanged(RuntimeError):
    pass


def heldout_dir() -> Path:
    return config.repo_root() / "heldout"


def set_sha256(folder: Path) -> str:
    """One hash over the set's content files (STATUS is bookkeeping and is left out)."""
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.name != "STATUS"):
        h.update(f.relative_to(folder).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def status(folder: Path) -> str:
    s = folder / "STATUS"
    return s.read_text().strip() if s.exists() else "blind"


async def run_set(
    name: str, force: bool = False, root: Path | None = None, reports: Path | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    folder = (root or heldout_dir()) / name
    if not folder.is_dir():
        raise FileNotFoundError(f"no held-out set {folder}")
    out_dir = (reports or config.reports_dir()) / "heldout" / name
    first = out_dir / "first_run.json"
    sha = set_sha256(folder)
    if first.exists():
        pinned = json.loads(first.read_text())["sha256"]
        if pinned != sha and not force:
            raise SetChanged(f"{name}: files changed since the first run (pinned {pinned[:12]}, now {sha[:12]})")

    if (folder / "guidelines.md").exists():
        result = await _run_guidelines(folder, ctx or Context.load())
    elif (folder / "entries.yaml").exists():
        result = await _run_entries(folder, ctx or Context.load())
    else:
        raise ValueError(f"{name}: neither guidelines.md nor entries.yaml")

    record = {
        "set": name,
        "status": status(folder),
        "sha256": sha,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_mode": config.model_mode(),
        **result,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("latest.json" if first.exists() else "first_run.json")
    target.write_text(json.dumps(record, indent=2, default=str) + "\n")
    record["written_to"] = str(target)
    return record


# ---------------------------------------------------------------- guideline sets


async def _run_guidelines(folder: Path, ctx: Context) -> dict[str, Any]:
    from lira.eval.compile_eval import score_ruleset
    from lira.guidelines.compiler import compile_guidelines
    from lira.guidelines.documents import parse

    meta = yaml.safe_load((folder / "answer_key.yaml").read_text())
    text = (folder / "guidelines.md").read_text()
    doc = parse(
        text, meta.get("tenant", "heldout"), int(meta.get("version", 1)), date.fromisoformat(str(meta.get("effective_from", "2026-01-01")))
    )
    report = await compile_guidelines(doc, ctx.router, meta.get("client_name", "the client"))
    answer = [{k: v for k, v in r.items() if k not in ("note", "quote")} for r in meta["rules"]]
    s = score_ruleset(report, answer)
    distractors = set(map(str, meta.get("not_rules", [])))
    s["rules_from_non_rule_clauses"] = sorted({r.clause_id for r in report.ruleset.rules} & distractors)
    s["rejected"] = report.rejected
    s["clauses_parsed"] = len(doc.clauses)
    return {"kind": "guidelines", "scores": s}


# ---------------------------------------------------------------- entry sets


def _invoices(entries: list[dict[str, Any]], ref: Any, set_name: str) -> list[tuple[dict[str, Any], Invoice]]:
    matter = ref.matters[MATTER]
    firm = ref.firms[matter.firm_id]
    tenant = ref.tenants[TENANT]
    start = date(2026, 10, 1)
    days = [start + timedelta(d) for d in range(31) if (start + timedelta(d)).weekday() < 5]
    by_class: dict[str, Any] = {}
    for tk in sorted(firm.timekeepers, key=lambda t: -t.seniority):
        if tk.id in matter.authorized_timekeepers and "first-year" not in tk.title.lower() and "summer" not in tk.title.lower():
            by_class.setdefault(tk.classification, tk)
    out = []
    for i, e in enumerate(entries):
        cls = str(e.get("timekeeper_class", "AS")).upper()
        tk = by_class.get(cls) or by_class["AS"]
        day = days[i % len(days)]
        rate = ref.approved_rate(TENANT, tk.id, day)
        units = Decimal(str(e["hours"]))
        task = str(e.get("task_code", "L190")).upper()
        if task not in matter.authorized_task_codes:
            task = "L190"
        li = LineItem(
            line_no="1",
            type=LineType.FEE,
            date=day,
            units=units,
            unit_cost=rate,
            total=money(units * rate),
            task_code=task,
            activity_code=str(e.get("activity_code", "A103")).upper(),
            timekeeper_id=tk.id,
            timekeeper_name=tk.name,
            timekeeper_class=tk.classification,
            description=str(e["description"]).strip(),
        )
        inv = Invoice(
            number=f"HO-{set_name}-{e['id']}"[:40],
            date=date(2026, 11, 3),
            client_id=tenant.client_id,
            law_firm_id=firm.id,
            law_firm_matter_id=matter.law_firm_matter_id,
            client_matter_id=matter.id,
            total=li.total,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            description="Held-out entry",
            lines=(li,),
        )
        out.append((e, inv))
    return out


def _pr(tp: int, fp: int, fn: int) -> dict[str, Any]:
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
    }


async def _run_entries(folder: Path, ctx: Context) -> dict[str, Any]:
    from lira.agents.review_graph import ReviewDeps, build_review_graph
    from lira.domain import Finding

    entries = yaml.safe_load((folder / "entries.yaml").read_text())["entries"]
    graph = build_review_graph(ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, ctx.router, ctx.index, human="skip"))
    thresholds = config.load("review")["candidates"]
    counts: dict[str, list[int]] = {q: [0, 0, 0] for q in JUDGED}  # system: tp fp fn
    screen_counts: dict[str, list[int]] = {q: [0, 0, 0] for q in JUDGED}  # classifier screen alone
    rows: list[dict[str, Any]] = []
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    pairs = _invoices(entries, ctx.ref, folder.name)
    probs = ctx.nlp.judgement_probs(
        [
            {
                "description": inv.lines[0].description,
                "timekeeper_class": inv.lines[0].timekeeper_class,
                "activity_code": inv.lines[0].activity_code,
                "units": float(inv.lines[0].units),
            }
            for _, inv in pairs
        ]
    )
    for i, (e, inv) in enumerate(pairs):
        state = await graph.ainvoke({"ledes_text": ledes.write(inv)})
        found = [Finding.model_validate(x) for x in state.get("findings", [])]
        predicted = {f.kind.value for f in found if f.kind.value in JUDGED and f.status != "rejected"}
        expected = {q for q in JUDGED if bool((e.get("labels") or {}).get(q))}
        screened = {q for q in JUDGED if float(probs[q][i]) >= float(thresholds[q])}
        for q in JUDGED:
            for c, got in ((counts, predicted), (screen_counts, screened)):
                if q in got and q in expected:
                    c[q][0] += 1
                elif q in got:
                    c[q][1] += 1
                elif q in expected:
                    c[q][2] += 1
        ok = predicted == expected
        cat = str(e.get("category", "uncategorised"))
        by_category[cat][0] += int(ok)
        by_category[cat][1] += 1
        rows.append(
            {
                "id": e["id"],
                "expected": sorted(expected),
                "predicted": sorted(predicted),
                "screen": sorted(screened),
                "agree": ok,
                "injection_flagged": any(f.kind.value == "prompt_injection" for f in found),
            }
        )
    n = len(rows)
    return {
        "kind": "entries",
        "entries": n,
        "exact_agreement": round(sum(r["agree"] for r in rows) / n, 4) if n else None,
        "per_question": {q: _pr(*counts[q]) for q in JUDGED},
        "classifier_screen": {q: _pr(*screen_counts[q]) for q in JUDGED},
        "by_category": {k: {"agree": v[0], "of": v[1]} for k, v in sorted(by_category.items())},
        "rows": rows,
    }
