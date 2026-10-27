"""Appeals: a law firm disputes an adjustment.

    classify (intent model) -> accept | request_info | assess (appeal agent) -> human decision -> resolve

* "accept" closes the appeal without a model call; "request_info" is answered from the finding's own clause.
* Disputes go to the appeal agent, which recommends uphold / reverse / partial and quotes the sentence of the appeal
  it relies on. The quote is verified against the appeal text and the clause against the finding before a person
  sees the recommendation; the reviewer decides, and only a reviewer's "reverse" changes the invoice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from lira.llm.base import Block, Message, ModelRequest, ModelResponse
from lira.llm.prompts import load
from lira.llm.router import ModelRouter, Unavailable
from lira.llm.schemas import RECORD_RECOMMENDATION, Recommendation
from lira.nlp.models import NlpModels


class AppealState(TypedDict, total=False):
    appeal: dict[str, Any]  # {message, finding:{id, kind, line_no, adjustment, rationale, clause:{clause_id, quote}}, description, firm_name, tenant, tenant_name, currency}
    intent: str
    intent_confidence: float
    recommendation: dict[str, Any] | None
    problems: list[str]
    reply: str
    decision: dict[str, Any] | None
    outcome: str  # "closed_accepted" | "answered" | "upheld" | "reversed" | "partial" | "awaiting_decision"


@dataclass
class AppealDeps:
    nlp: NlpModels
    router: ModelRouter
    human: Literal["interrupt", "skip"] = "interrupt"
    errors: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def build_appeal_graph(deps: AppealDeps, checkpointer: Any = None) -> Any:
    def classify(state: AppealState) -> dict[str, Any]:
        intent, conf = deps.nlp.intents([state["appeal"]["message"]])[0]
        return {"intent": intent, "intent_confidence": conf}

    def route(state: AppealState) -> str:
        if state["intent"] == "accept" and state["intent_confidence"] >= 0.6:
            return "close"
        if state["intent"] == "request_info" and state["intent_confidence"] >= 0.6:
            return "answer"
        return "assess"

    def close(state: AppealState) -> dict[str, Any]:
        return {"outcome": "closed_accepted", "reply": "Thank you for confirming. The adjustment stands."}

    def answer(state: AppealState) -> dict[str, Any]:
        f = state["appeal"]["finding"]
        c = f.get("clause") or {}
        text = (f"The adjustment on line {f['line_no']} applies clause {c.get('clause_id')} of the billing guidelines: "
                f"\"{c.get('quote', '')}\". {f.get('rationale', '')}").strip()
        return {"outcome": "answered", "reply": text}

    async def assess(state: AppealState) -> dict[str, Any]:
        a = state["appeal"]
        f = a["finding"]
        c = f.get("clause") or {}
        prompt = load("appeal_review")
        user = prompt.render_user(tenant_name=a["tenant_name"], clause_id=c.get("clause_id", ""), clause_text=c.get("quote", ""),
                                  line_no=f["line_no"], description=a["description"], finding_kind=f["kind"],
                                  adjustment=f["adjustment"], currency=a["currency"], rationale=f.get("rationale", ""),
                                  firm_name=a["firm_name"], appeal=a["message"])
        req = ModelRequest(task="appeal_review", system=prompt.system, messages=[Message("user", [Block.t(user)])],
                           tools=[RECORD_RECOMMENDATION], tool_choice="record_recommendation", max_tokens=800,
                           prompt_id=prompt.id, prompt_version=prompt.version, tenant=a["tenant"])

        def parse(r: ModelResponse) -> Recommendation:
            return Recommendation.model_validate(r.tool_input("record_recommendation"))

        try:
            routed = await deps.router.call(req, parse)
        except Unavailable as e:
            deps.errors.append(str(e))
            return {"recommendation": None, "problems": ["assistant unavailable: a reviewer reads the appeal"]}
        rec = routed.value
        problems = []
        if _norm(rec.appeal_quote) not in _norm(a["message"]):
            problems.append("quoted sentence is not in the appeal")
        if rec.clause_id != c.get("clause_id"):
            problems.append(f"cites clause {rec.clause_id}, the finding rests on clause {c.get('clause_id')}")
        return {"recommendation": rec.model_dump() if not problems else None, "problems": problems}

    def decide(state: AppealState) -> dict[str, Any]:
        if deps.human == "skip":
            return {"outcome": "awaiting_decision"}
        decision = interrupt({"finding": state["appeal"]["finding"]["id"], "recommendation": state.get("recommendation"),
                              "problems": state.get("problems", [])})
        return {"decision": decision, "outcome": {"uphold": "upheld", "reverse": "reversed", "partial": "partial"}[decision["decision"]]}

    g = StateGraph(AppealState)
    for name, fn in (("classify", classify), ("close", close), ("answer", answer), ("assess", assess), ("decide", decide)):
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route, ["close", "answer", "assess"])
    g.add_edge("close", END)
    g.add_edge("answer", END)
    g.add_edge("assess", "decide")
    g.add_edge("decide", END)
    return g.compile(checkpointer=checkpointer)
