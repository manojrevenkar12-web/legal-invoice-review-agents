"""Law-firm appeals against adjustments: training messages for the intent classifier, and appeals against the
system's own findings for evaluation (justified when the answer key says the finding was wrong).

Simulation only.
"""

from __future__ import annotations

from typing import Any

import numpy as np

INTENTS = ("dispute_facts", "dispute_policy", "provide_detail", "accept", "request_info")

FACTS = {
    "internal_conference": [
        "The meeting recorded on line {line} was with {contact}, the client's in-house counsel, not with a colleague at our firm.",
        "Line {line} was a call with opposing counsel; nobody else from our firm attended, so it is not an internal conference.",
        "Please note that {contact} is not a member of our firm - she is the client's litigation manager.",
    ],
    "block_billing": [
        "Line {line} describes a single task: the review and the analysis were one continuous piece of work on the same document.",
        "The entry on line {line} is one task. Preparing for and attending the deposition is a single piece of work.",
    ],
    "clerical_work": [
        "The work on line {line} required a lawyer's judgement: it was a substantive privilege analysis, not filing.",
        "Line {line} was not administrative. It involved analysis of which exhibits to use at the hearing.",
    ],
    "vague_description": [
        "The entry on line {line} should read: review of the claimant's expert report on causation, sections 3 to 5.",
        "Specifically, line {line} covered the analysis of the settlement offer received on 12 October.",
    ],
}
POLICY = [
    "We have always billed this way on your matters and it has never been questioned before.",
    "The work on line {line} was urgent and had to be done at short notice, so we ask you to pay it in full.",
    "Our engagement partner approved this entry, and we believe the reduction on line {line} is unfair.",
    "This is standard practice in our market and we ask that the adjustment on line {line} be reversed.",
    "The time on line {line} was necessary for the case. We do not accept the reduction.",
]
DETAIL = [
    "To clarify line {line}: the work related to the {topic} and took the time recorded.",
    "For line {line}, the attorney was working on the {topic} issues raised by the other side.",
]
ACCEPT = [
    "We accept the adjustment on line {line} and will issue a corrected invoice.",
    "Understood - the reduction on line {line} is fine. Thank you for flagging it.",
    "No objection to the change on line {line}.",
]
INFO = [
    "Could you tell us which clause of the guidelines line {line} breaches?",
    "We do not understand the adjustment on line {line}. Please explain the basis.",
    "Which provision applies to line {line}? We could not find it.",
]
TOPICS = ["container loss", "warehouse fire", "temperature excursion", "customs seizure", "product recall"]
CONTACTS = ["Katrin Albers", "Eleanor Shaw", "Megan Liu", "Funmi Adeyemi", "Dominik Fuchs"]


def _pick(rng: np.random.Generator, xs: list[str]) -> str:
    return xs[int(rng.integers(len(xs)))]


def message(intent: str, kind: str, line: str, rng: np.random.Generator) -> str:
    fill = {"line": line, "contact": _pick(rng, CONTACTS), "topic": _pick(rng, TOPICS)}
    if intent == "dispute_facts":
        tmpl = _pick(rng, FACTS.get(kind) or FACTS["block_billing"])
    elif intent == "dispute_policy":
        tmpl = _pick(rng, POLICY)
    elif intent == "provide_detail":
        tmpl = _pick(rng, DETAIL)
    elif intent == "accept":
        tmpl = _pick(rng, ACCEPT)
    else:
        tmpl = _pick(rng, INFO)
    text = tmpl.format(**fill)
    if rng.random() < 0.4:
        text = "Dear reviewer, " + text[0].lower() + text[1:]
    if rng.random() < 0.4:
        text += " Kind regards, billing team"
    return text


def training_set(seed: int = 11, n: int = 600) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    kinds = list(FACTS)
    out = []
    for _ in range(n):
        intent = INTENTS[int(rng.integers(len(INTENTS)))]
        out.append({"message": message(intent, _pick(rng, kinds), str(int(rng.integers(1, 40))), rng), "intent": intent})
    return out


def appeal_for(finding: dict[str, Any], truth_kinds: set[str], rng: np.random.Generator) -> dict[str, Any]:
    """The firm's reply to one finding. A wrong finding is disputed with the fact that disproves it."""
    kind = finding["kind"]
    justified = kind not in truth_kinds
    if justified and kind in FACTS:
        intent = "dispute_facts"
    else:
        intent = str(rng.choice(["dispute_policy", "provide_detail", "accept", "request_info"], p=[0.45, 0.2, 0.2, 0.15]))
        if intent == "provide_detail" and kind != "vague_description":
            intent = "dispute_policy"
    return {
        "finding_id": finding["id"],
        "kind": kind,
        "line_no": finding["line_no"],
        "intent": intent,
        "justified": justified,
        "message": message(intent, kind, finding["line_no"], rng),
    }
