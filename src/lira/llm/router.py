"""Route each model call: the tenant's data policy picks which models may see the data, a fallback chain per
task, retries with back-off, a circuit breaker per model, one repair round for invalid output, and a cost cap.
When nothing can answer, callers get ``Unavailable`` and degrade (the review continues with rules and classical
NLP, and every judgement goes to a person).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import ValidationError

from lira import config
from lira.llm.base import Block, InvalidOutput, Message, ModelRequest, ModelResponse, Provider, ProviderError, Usage
from lira.obs import metrics
from lira.obs.tracing import model_call_attributes, span

T = TypeVar("T")


class Unavailable(Exception):
    """No allowed model could produce a valid answer."""

    def __init__(self, task: str, errors: list[str]):
        super().__init__(f"{task}: no model available ({'; '.join(errors[-3:])})")
        self.task = task
        self.errors = errors


class BudgetExceeded(Unavailable):
    pass


@dataclass(frozen=True)
class ModelInfo:
    key: str
    provider: str
    model_id: str
    region_group: str
    privacy: str
    price_in: float
    price_out: float

    def cost(self, u: Usage) -> float:
        return (u.input_tokens * self.price_in + u.output_tokens * self.price_out) / 1_000_000


@dataclass
class Breaker:
    failures: int = 0
    open_until: float = 0.0


@dataclass
class Routed(Generic[T]):
    response: ModelResponse
    value: T
    model: ModelInfo
    attempts: int
    cost_usd: float
    usage: Usage


@dataclass
class Budget:
    """Spend cap for one unit of work (an invoice review)."""

    limit_usd: float
    spent_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)
    calls: int = 0

    def charge(self, cost: float, u: Usage) -> None:
        self.spent_usd += cost
        self.usage = self.usage + u
        self.calls += 1


class ModelRouter:
    def __init__(self, providers: dict[str, Provider], settings: dict[str, Any] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = asyncio.sleep):
        cfg = settings or config.load("models")
        def price(v: dict[str, Any], key: str) -> float:
            src = cfg["models"].get(v.get("price_as", ""), v)
            return float(src.get(key, 0))

        self.models = {k: ModelInfo(k, v["provider"], v["model_id"], v.get("region_group", ""), v.get("privacy", ""),
                                    price(v, "price_in"), price(v, "price_out")) for k, v in cfg["models"].items()}
        self.tasks: dict[str, list[str]] = cfg["tasks"]
        self.policies: dict[str, list[str]] = cfg.get("tenant_policies", {})
        self.routing = cfg["routing"]
        self.providers = providers
        self.clock = clock
        self.sleep = sleep
        self.breakers: dict[str, Breaker] = {}
        self.simulated_only = set(providers) == {"simulated"}

    def chain(self, task: str, tenant: str) -> list[ModelInfo]:
        if self.simulated_only:
            return [self.models["simulated"]]
        allowed = set(self.policies.get(tenant, self.policies.get("default", [])))
        out = []
        for key in self.tasks[task]:
            m = self.models[key]
            if m.region_group in allowed and m.provider in self.providers:
                out.append(m)
        return out

    def _breaker(self, key: str) -> Breaker:
        return self.breakers.setdefault(key, Breaker())

    async def call(self, req: ModelRequest, parse: Callable[[ModelResponse], T], budget: Budget | None = None) -> Routed[T]:
        errors: list[str] = []
        chain = self.chain(req.task, req.tenant)
        if not chain:
            raise Unavailable(req.task, [f"no model allowed for tenant {req.tenant!r}"])
        attempts = 0
        for m in chain:
            br = self._breaker(m.key)
            if br.open_until > self.clock():
                errors.append(f"{m.key}: circuit open")
                continue
            if budget is not None:
                est = self._estimate(req, m)
                if budget.spent_usd + est > budget.limit_usd:
                    raise BudgetExceeded(req.task, errors + [f"budget {budget.limit_usd} USD reached"])
            provider = self.providers[m.provider]
            messages = list(req.messages)
            repairs = 0
            for attempt in range(int(self.routing["attempts_per_model"])):
                attempts += 1
                r = ModelRequest(**{**req.__dict__, "messages": messages})
                with span(f"model.{req.task}", **model_call_attributes(req.task, m.model_id, m.provider, req.prompt_id,
                                                                        req.prompt_version, req.tenant)) as s:
                    try:
                        resp = await asyncio.wait_for(provider.generate(r, m.model_id), timeout=float(self.routing["timeout_seconds"]))
                    except (ProviderError, TimeoutError) as e:
                        retryable = getattr(e, "retryable", True)
                        code = getattr(e, "code", "timeout")
                        s.set_attribute("error.type", code)
                        errors.append(f"{m.key}: {code}")
                        self._fail(m.key)
                        metrics.emit({"task": req.task, "model": m.key, "outcome": "error"}, calls=1)
                        if not retryable:
                            break
                        backoff = self.routing["backoff_seconds"]
                        await self.sleep(float(backoff[min(attempt, len(backoff) - 1)]))
                        continue
                    cost = m.cost(resp.usage)
                    s.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
                    s.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
                    s.set_attribute("lira.cost_usd", round(cost, 6))
                    if budget is not None:
                        budget.charge(cost, resp.usage)
                    try:
                        value = parse(resp)
                    except (InvalidOutput, ValidationError, KeyError, ValueError) as e:
                        s.set_attribute("error.type", "invalid_output")
                        errors.append(f"{m.key}: invalid output ({str(e)[:120]})")
                        metrics.emit({"task": req.task, "model": m.key, "outcome": "invalid"}, calls=1, cost_usd=cost)
                        if repairs < int(self.routing["repair_attempts"]):
                            repairs += 1
                            messages = messages + _repair_turn(resp, req, str(e))
                            continue
                        self._fail(m.key)
                        break
                    self._ok(m.key)
                    metrics.emit({"task": req.task, "model": m.key, "outcome": "ok"}, calls=1, cost_usd=cost,
                                 tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens, latency_ms=resp.latency_ms)
                    return Routed(resp, value, m, attempts, cost, resp.usage)
        raise Unavailable(req.task, errors)

    def _fail(self, key: str) -> None:
        br = self._breaker(key)
        br.failures += 1
        if br.failures >= int(self.routing["breaker_failures"]):
            br.open_until = self.clock() + float(self.routing["breaker_cooldown_seconds"])
            br.failures = 0

    def _ok(self, key: str) -> None:
        self._breaker(key).failures = 0

    def _estimate(self, req: ModelRequest, m: ModelInfo) -> float:
        chars = len(req.system) + sum(len(b.text) + len(str(b.input)) + len(str(b.content or "")) for msg in req.messages for b in msg.content)
        return m.cost(Usage(chars // 4, req.max_tokens // 4))


def _repair_turn(resp: ModelResponse, req: ModelRequest, error: str) -> list[Message]:
    """Show the model its invalid call and the validation error, and ask for the same tool call again."""
    tool = req.tool_choice if req.tool_choice not in ("auto", "any") else "the required tool"
    uses = resp.tool_uses()
    if uses:
        return [
            Message("assistant", uses),
            Message("user", [Block("tool_result", tool_use_id=u.tool_use_id, content={"error": error[:500]}, is_error=True) for u in uses]
                    + [Block.t(f"The call failed validation: {error[:500]}. Call {tool} again with corrected input.")]),
        ]
    return [Message("assistant", resp.content or [Block.t("(no output)")]),
            Message("user", [Block.t(f"You must call {tool}. Validation error: {error[:500]}")])]
