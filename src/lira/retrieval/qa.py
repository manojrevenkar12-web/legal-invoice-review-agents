"""Answer questions about a client's guidelines from retrieved clauses, with citations that are checked.

An answer is shown only if every clause it cites was retrieved for this question and every number it states
appears in a cited clause. Otherwise the user gets "I could not find a supported answer" and the clauses that
were retrieved, never an unsupported answer.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

from lira.llm.base import Block, Message, ModelRequest, ModelResponse
from lira.llm.prompts import load
from lira.llm.router import ModelRouter, Unavailable
from lira.llm.schemas import RECORD_ANSWER, Answer
from lira.retrieval.index import Hit, HybridIndex, retrieve

NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


@dataclass
class QAResult:
    question: str
    answer: str
    citations: list[str]
    answerable: bool
    verified: bool
    problems: list[str] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)  # clause ids, best first
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    model: str = ""


def check_answer(ans: Answer, hits: list[Hit]) -> list[str]:
    problems: list[str] = []
    shown = {h.chunk.clause_id: h.chunk.text for h in hits}
    for c in ans.citations:
        if c not in shown:
            problems.append(f"cites clause {c}, which was not retrieved")
    if ans.answerable and not ans.citations:
        problems.append("answer without citations")
    cited = " ".join(shown.get(c, "") for c in ans.citations)
    for n in NUMBER.findall(re.sub(r"\[[^\]]*\]", "", ans.answer)):
        if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", cited):
            problems.append(f"number {n} is not in the cited clauses")
    return problems


async def ask(index: HybridIndex, router: ModelRouter, tenant: str, tenant_name: str, question: str, on: date | None = None,
              k: int = 4) -> QAResult:
    t0 = time.perf_counter()
    hits = retrieve(index, question, tenant, k=k, on=on or date.today())  # one version: the one in force on that date
    prompt = load("qa_answer")
    clauses = "\n".join(f"[{h.chunk.clause_id}] {h.chunk.text}" for h in hits) or "(none found)"
    req = ModelRequest(task="qa_answer", system=prompt.system,
                       messages=[Message("user", [Block.t(prompt.render_user(tenant_name=tenant_name, question=question, clauses=clauses))])],
                       tools=[RECORD_ANSWER], tool_choice="record_answer", max_tokens=800, prompt_id=prompt.id,
                       prompt_version=prompt.version, tenant=tenant)

    def parse(r: ModelResponse) -> Answer:
        return Answer.model_validate(r.tool_input("record_answer"))

    retrieved = [h.chunk.clause_id for h in hits]
    try:
        routed = await router.call(req, parse)
    except Unavailable as e:
        return QAResult(question, "The assistant is unavailable; the most relevant clauses are listed.", [], False, False,
                        [str(e)], retrieved, (time.perf_counter() - t0) * 1000)
    ans = routed.value
    problems = check_answer(ans, hits)
    if problems:
        return QAResult(question, "I could not find a supported answer in the guidelines. The closest clauses are listed.", [], False,
                        False, problems, retrieved, (time.perf_counter() - t0) * 1000, routed.cost_usd, routed.model.key)
    return QAResult(question, ans.answer, ans.citations, ans.answerable, True, [], retrieved, (time.perf_counter() - t0) * 1000,
                    routed.cost_usd, routed.model.key)
