"""A small tool-use loop: the model may call read-only tools a bounded number of times, then must record its answer.

Tools are plain async functions over this tenant's data; they cannot write anything. The last step forces the
recording tool, so an agent cannot wander past its step budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from lira.llm.base import Block, InvalidOutput, Message, ModelRequest, ModelResponse
from lira.llm.router import Budget, ModelRouter, Routed

Tool = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class AgentRun[T]:
    value: T
    steps: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    model: str = ""


async def run_tool_agent[T](router: ModelRouter, req: ModelRequest, tools: dict[str, Tool], final_tool: str,
                         parse_final: Callable[[dict[str, Any]], T], max_steps: int, budget: Budget | None = None) -> AgentRun[T]:
    messages = list(req.messages)
    calls: list[dict[str, Any]] = []
    cost = 0.0
    model = ""
    for step in range(1, max_steps + 1):
        forced = final_tool if step == max_steps else "any"

        def parse(r: ModelResponse) -> tuple[str, Any]:
            uses = r.tool_uses()
            final = [u for u in uses if u.name == final_tool]
            if final:
                return "final", parse_final(final[0].input)
            other = [u for u in uses if u.name in tools]
            if other:
                return "tools", other
            raise InvalidOutput(f"expected a call to {final_tool} or one of {sorted(tools)}")

        r = ModelRequest(**{**req.__dict__, "messages": messages, "tool_choice": forced})
        routed: Routed[tuple[str, Any]] = await router.call(r, parse, budget)
        cost += routed.cost_usd
        model = routed.model.key
        kind, value = routed.value
        if kind == "final":
            return AgentRun(value, step, calls, cost, model)
        results: list[Block] = []
        for u in value:
            try:
                out = await tools[u.name](u.input)
                results.append(Block("tool_result", tool_use_id=u.tool_use_id, content=out))
            except Exception as e:  # a tool failure is reported to the model, not raised
                out = {"error": str(e)[:200]}
                results.append(Block("tool_result", tool_use_id=u.tool_use_id, content=out, is_error=True))
            calls.append({"tool": u.name, "input": u.input, "ok": "error" not in out})
        messages = messages + [Message("assistant", value), Message("user", results)]
    raise InvalidOutput(f"agent did not call {final_tool} within {max_steps} steps")
