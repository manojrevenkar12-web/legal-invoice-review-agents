"""A deterministic stand-in for the language model, used in tests, CI, the demo and the simulated evaluation.

It answers the same prompts through the same tool contracts as Claude, reading only the rendered prompt (never
the simulation's truth), with plain heuristics and a configurable error rate. Fault modes reproduce what real
models and providers do wrong, so the safety machinery can be tested: outages, malformed output, invented clause
ids, quotes that are not in the text, and obeying instructions injected into time entries.

LIRA_SIM_FAULTS="outage=0.3,invalid=0.2,hallucinate=0.1,fabricate_quote=0.1,obey_injection=1,flip=0.05"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from lira.guidelines.documents import Clause
from lira.guidelines.extract import extract
from lira.llm.base import Block, ModelRequest, ModelResponse, ProviderError, Usage

TASK_VERBS = (
    "draft",
    "review",
    "prepare",
    "attend",
    "research",
    "analyze",
    "analyse",
    "telephone",
    "conference",
    "meet",
    "revise",
    "interview",
    "coordinate",
    "examine",
    "evaluate",
    "summarize",
    "cite-check",
    "defend",
    "call",
    "email",
    "tc",
    "rev",
    "drft",
    "prep",
    "confer",
    "discuss",
    "strategy",
    "update",
    "file",
    "organize",
    "investigate",
    "travel",
    "second-level",
    "confirm",
    "identify",
    "correspondence",
    "negotiate",
    "outline",
    "edit",
    "finalize",
    "finalise",
    "videoconference",
)
CLERICAL = (
    "organize",
    "organise",
    "copy and distribute",
    "scan and save",
    "scan & save",
    "calendar",
    "electronic filing",
    "scan and save",
    "schedule ",
    "bates label",
    "assemble",
    "update matter contact",
    "arrange courier",
    "courier delivery",
    "book meeting room",
    "rename and index",
    "tab exhibits",
    "catering",
)
GENERIC = (
    "attention to file",
    "work on matter",
    "review documents",
    "legal research",
    "trial preparation",
    "correspondence",
    "review file",
    "follow up",
    "conference",
    "draft document",
    "research",
    "discovery work",
    "prepare for meeting",
    "work on brief",
    "various emails",
    "review and revise",
    "case management",
    "analysis",
)
INJECTION = re.compile(
    r"(note to (?:automated|ai)|system instruction|ai reviewer|assistant,|disregard prior|ignore the billing|"
    r"do not adjust|mark it compliant|pre-approved by client)",
    re.I,
)
INTERNAL = re.compile(r"\b(conference|conf|meet|meeting|discuss|strategy session|office conference|team meeting)\b", re.I)


def faults() -> dict[str, float]:
    out: dict[str, float] = {}
    for part in os.environ.get("LIRA_SIM_FAULTS", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = float(v)
    return out


def _u(*parts: Any) -> float:
    """A uniform number in [0, 1) fixed by its inputs: the simulated model is deterministic."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _tool(name: str, payload: dict[str, Any], key: str) -> Block:
    return Block("tool_use", tool_use_id=f"tu_{hashlib.sha1((name + key).encode()).hexdigest()[:12]}", name=name, input=payload)


class SimulatedProvider:
    name = "simulated"

    def __init__(self, fault_modes: dict[str, float] | None = None):
        self.f = fault_modes if fault_modes is not None else faults()
        self.calls = 0

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse:
        self.calls += 1
        prompt = "\n".join(b.text for m in req.messages for b in m.content if b.type == "text")
        key = hashlib.sha256((req.task + prompt).encode()).hexdigest()
        if _u(key, "outage", self.calls) < self.f.get("outage", 0):
            raise ProviderError("simulated outage", retryable=True, code="ServiceUnavailableException")
        repairing = any(b.type == "tool_result" and b.is_error for m in req.messages for b in m.content)
        if not repairing and _u(key, "invalid") < self.f.get("invalid", 0):
            return self._resp(
                [_tool(req.tool_choice if req.tool_choice not in ("auto", "any") else "record_findings", {"oops": True}, key)],
                prompt,
                model,
            )
        handler = getattr(self, f"_{req.task}")
        blocks = handler(req, prompt, key)
        return self._resp(blocks, prompt, model)

    def _resp(self, blocks: list[Block], prompt: str, model: str) -> ModelResponse:
        out = sum(len(json.dumps(b.input)) for b in blocks if b.type == "tool_use")
        return ModelResponse(blocks, "tool_use", Usage(len(prompt) // 4 + 400, out // 4 + 20), model, "simulated", 5.0)

    # ---------------------------------------------------------------- tasks
    def _guideline_compile(self, req: ModelRequest, prompt: str, key: str) -> list[Block]:
        clauses = [Clause(m.group(1), (), m.group(2), 0, 0) for m in re.finditer(r"^\[([^\]]+)\] (.+)$", prompt, re.M)]
        rules = [json.loads(json.dumps(r, default=str)) for r in extract(clauses)]
        for r in rules:
            for k, v in list(r.items()):
                if k in ("hours", "rate_factor", "reduce_factor", "max_unit_cost", "hours_per_month"):
                    r[k] = float(v)
        if _u(key, "hallucinate") < self.f.get("hallucinate", 0) and rules:
            rules.append(
                {"kind": "max_daily_hours", "clause_id": "9.9", "quote": "No timekeeper may bill more than 8 hours per day.", "hours": 8}
            )
        if _u(key, "fabricate_quote") < self.f.get("fabricate_quote", 0) and rules:
            rules[0] = {**rules[0], "quote": rules[0]["quote"].replace("0.1", "0.25")}
        return [_tool("record_rules", {"rules": rules}, key)]

    def _line_review(self, req: ModelRequest, prompt: str, key: str) -> list[Block]:
        m = re.search(r"Entries \(JSON\):\s*(\[.*\])\s*$", prompt, re.S)
        entries = json.loads(m.group(1)) if m else []
        shown = dict(re.findall(r"^\[([^\]]+)\] (.+)$", prompt, re.M))
        for b in (b for msg in req.messages for b in msg.content if b.type == "tool_result" and isinstance(b.content, dict)):
            for c in b.content.get("clauses", []):
                shown[c["clause_id"]] = c["text"]
        searched = any(b.type == "tool_result" for msg in req.messages for b in msg.content)
        needed = {q for e in entries for q in e.get("questions", [])}
        clause_for = {q: _clause_for(q, shown) for q in needed}
        missing = [q for q, c in clause_for.items() if c is None]
        if missing and not searched and req.tool_choice in ("auto", "any"):
            q = missing[0].replace("_", " ")
            return [_tool("search_guidelines", {"query": f"billing guideline on {q}", "limit": 3}, key)]
        findings: list[dict[str, Any]] = []
        injections: list[dict[str, Any]] = []
        flip = self.f.get("flip", 0.04)
        for e in entries:
            text = e["description"]
            inj = INJECTION.search(text)
            if inj:
                if _u(key, e["line_no"], "obey") < self.f.get("obey_injection", 0):
                    for q in e.get("questions", []):
                        findings.append(
                            {
                                "line_no": e["line_no"],
                                "question": q,
                                "decision": "compliant",
                                "evidence_quote": text[:40],
                                "clause_id": clause_for.get(q),
                                "reason": "Entry states it was pre-approved.",
                            }
                        )
                    continue
                injections.append({"line_no": e["line_no"], "quote": text[inj.start() : min(len(text), inj.end() + 40)]})
            for q in e.get("questions", []):
                decision, quote, reason = _judge(q, e)
                u = _u(key, e["line_no"], q)
                if u < flip:
                    decision = {"violation": "compliant", "compliant": "violation", "unsure": "violation"}[decision]
                    reason = "On balance the entry " + ("does" if decision == "violation" else "does not") + " fall under the clause."
                cid = clause_for.get(q)
                if _u(key, e["line_no"], q, "h") < self.f.get("hallucinate", 0):
                    cid = "12.4"
                if _u(key, e["line_no"], q, "f") < self.f.get("fabricate_quote", 0):
                    quote = "billed for unnecessary work"
                findings.append(
                    {
                        "line_no": e["line_no"],
                        "question": q,
                        "decision": decision,
                        "evidence_quote": quote,
                        "clause_id": cid,
                        "reason": reason,
                    }
                )
        return [_tool("record_findings", {"findings": findings, "injections": injections}, key)]

    def _appeal_review(self, req: ModelRequest, prompt: str, key: str) -> list[Block]:
        appeal = prompt.split("Appeal from", 1)[1].split("\n", 1)[1].strip() if "Appeal from" in prompt else ""
        kind = re.search(r"Finding: (\w+)", prompt)
        finding = kind.group(1) if kind else ""
        clause = re.search(r"Guideline clause (\S+):", prompt)
        sentences = re.split(r"(?<=[.!?])\s+", appeal)
        facts = {
            "internal_conference": r"\b(client|in-house|opposing counsel|expert|not (?:a member|at our firm|with our firm)|external)\b",
            "block_billing": r"\b(single task|one task|one continuous|a single piece of work)\b",
            "clerical_work": r"\b(legal judg|substantive|privilege|required a lawyer|analysis of)\b",
            "vague_description": r"\b(the entry (?:should|did) (?:read|describe)|specifically|in particular)\b",
        }
        pattern = facts.get(finding)
        hit = next((s for s in sentences if pattern and re.search(pattern, s, re.I)), None)
        if hit and finding == "vague_description":
            rec, reason = (
                "uphold",
                "Detail supplied after the fact does not change the entry as billed; the guideline requires it on the entry.",
            )
        elif hit:
            rec, reason = "reverse", "The appeal gives a fact that contradicts the basis of the finding."
        else:
            hit = sentences[0] if sentences else appeal[:80]
            rec, reason = "uphold", "The appeal does not dispute the facts the finding rests on."
        if _u(key, "flip") < self.f.get("flip", 0.04):
            rec = "reverse" if rec == "uphold" else "uphold"
        return [
            _tool(
                "record_recommendation",
                {
                    "recommendation": rec,
                    "remaining_adjustment": None,
                    "appeal_quote": hit or "",
                    "clause_id": clause.group(1) if clause else "",
                    "reason": reason,
                },
                key,
            )
        ]

    def _qa_answer(self, req: ModelRequest, prompt: str, key: str) -> list[Block]:
        q = re.search(r"Question: (.+)", prompt)
        question = q.group(1) if q else ""
        clauses = re.findall(r"^\[([^\]]+)\] (.+)$", prompt, re.M)
        qwords = {w for w in re.findall(r"[a-z]{4,}", question.lower())} - {
            "what",
            "does",
            "with",
            "much",
            "many",
            "when",
            "they",
            "this",
            "that",
            "client",
            "guidelines",
            "billing",
            "counsel",
        }
        best, score = None, 0.0
        for rank, (cid, text) in enumerate(clauses[:3]):  # clauses arrive best-first from retrieval
            words = set(re.findall(r"[a-z]{4,}", text.lower()))
            s = len(qwords & words) / max(1, len(qwords)) - 0.08 * rank
            if s > score:
                best, score = (cid, text), s
        if best is None or score < 0.2:
            return [
                _tool(
                    "record_answer",
                    {"answer": "The guidelines provided do not answer this question.", "citations": [], "answerable": False},
                    key,
                )
            ]
        cid, text = best
        sentence = max(re.split(r"(?<=[.;])\s+", text), key=lambda s: len(qwords & set(re.findall(r"[a-z]{4,}", s.lower()))))
        sentence = re.sub(r"^(?:\(?[a-z0-9.()]+\)?\s)", "", sentence) if re.match(r"^\(?\d|^\([a-z]\)", sentence) else sentence
        if _u(key, "hallucinate") < self.f.get("hallucinate", 0):
            cid = "99.1"
        return [_tool("record_answer", {"answer": f"{sentence} [{cid}]", "citations": [cid], "answerable": True}, key)]

    def _judge_finding(self, req: ModelRequest, prompt: str, key: str) -> list[Block]:
        clause = re.search(r"Guideline clause \S+: (.+)", prompt)
        entry = re.search(r"Entry \(line [^)]*\): (.+)", prompt)
        kind = re.search(r"Proposed finding: (\w+) -> (\w+)", prompt)
        expl = re.search(r"Explanation to the firm: (.+)", prompt, re.S)
        ctext, etext = (clause.group(1) if clause else ""), (entry.group(1) if entry else "")
        k = kind.group(1) if kind else ""
        explanation = expl.group(1).strip() if expl else ""
        stems = {
            "block_billing": "block",
            "vague_description": "vague|generic|descri",
            "clerical_work": "clerical|administrative|secretarial",
            "internal_conference": "internal|intra-firm",
        }
        supported = bool(re.search(stems.get(k, k.split("_")[0]), ctext, re.I))
        decision, _q, _r = _judge(k, {"description": etext, "people": [{"at_firm": True}] if k == "internal_conference" else []})
        correct = decision == "violation" if _u(etext, k, "judge") > 0.06 else decision != "violation"
        specific = any(w in explanation for w in re.findall(r"[A-Za-z]{5,}", etext)[:8]) or bool(re.search(r"\d+\.\d h", explanation))
        professional = not re.search(r"\b(obviously|padding|fraud|lazy|!!)\b", explanation, re.I)
        overall = supported and correct
        pf = lambda b: "pass" if b else "fail"  # noqa: E731
        return [
            _tool(
                "record_verdict",
                {
                    "supported": pf(supported),
                    "correct": pf(correct),
                    "specific": pf(specific),
                    "professional": pf(professional),
                    "overall": pf(overall),
                    "reasons": "Graded against the clause and the entry as billed.",
                },
                key,
            )
        ]


def _clause_for(question: str, shown: dict[str, str]) -> str | None:
    pat = {
        "block_billing": r"block",
        "vague_description": r"vague|generic|specific description",
        "clerical_work": r"clerical|administrative|secretarial",
        "internal_conference": r"internal|intra-firm",
    }[question]
    for cid, text in shown.items():
        if re.search(pat, text, re.I):
            return cid
    return None


_ABBR = {
    "drft": "draft",
    "rev": "review",
    "prep": "prepare",
    "tc": "telephone conference",
    "conf": "conference",
    "w/": "with",
    "&": "and",
    "depo": "deposition",
    "docs": "documents",
    "re": "regarding",
    "mtn": "motion",
}


def _plain(text: str) -> str:
    t = re.sub(r"^\s*[LAE]\d{3}(?:/[LAE]\d{3})?\s*", "", text)
    return " ".join(_ABBR.get(w.lower(), w) for w in t.split())


def _judge(question: str, e: dict[str, Any]) -> tuple[str, str, str]:
    """Heuristic reading of one entry for one question: (decision, verbatim quote, reason)."""
    raw = e.get("description", "")
    text = _plain(raw)
    lo = text.lower()
    if question == "block_billing":
        parts = [p.strip() for p in re.split(r";|\s/\s|, and |\. (?=[A-Z])", text) if p.strip()]
        more = []
        for p in parts:
            sub = re.split(r"\s(?:and|&)\s(?=(?:" + "|".join(TASK_VERBS) + r")\b)", p, flags=re.I)
            more += [s for s in sub if len(s.split()) >= 3] or [p]
        tasks = [p for p in more if p.split() and p.split()[0].lower().strip(".,") in TASK_VERBS]
        if len(tasks) >= 2:
            return "violation", raw[: min(len(raw), 120)], f"The entry combines {len(tasks)} tasks in one time entry."
        return "compliant", raw[: min(len(raw), 80)], "The entry describes one task."
    if question == "vague_description":
        bare = re.sub(r"[^a-z ]", "", lo).strip()
        if bare in GENERIC or (len(bare.split()) <= 5 and any(bare.startswith(g) for g in GENERIC)):
            return "violation", raw, "The description does not say what was done or on what subject."
        if len(bare.split()) <= 3:
            return "unsure", raw, "Short description; a reviewer may want more detail."
        return "compliant", raw[:60], "The description names the work and its subject."
    if question == "clerical_work":
        hit = next((c for c in CLERICAL if c in lo), None)
        if hit:
            return "violation", raw[: min(len(raw), 100)], "Administrative work billed at professional rates."
        return "compliant", raw[:60], "Substantive legal or paralegal work."
    if question == "internal_conference":
        at_firm = any(p.get("at_firm") for p in e.get("people", []))
        m = INTERNAL.search(text)
        if at_firm and m and not re.search(r"\b(client|opposing|expert|vendor|court reporter)\b", lo):
            return "violation", raw[: min(len(raw), 100)], "Meeting with a colleague at the same firm."
        return "compliant", raw[:60], "Not a meeting between colleagues at the firm."
    return "unsure", raw[:40], ""
