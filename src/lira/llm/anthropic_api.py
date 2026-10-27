"""The Anthropic Messages API, used only for tenants whose data policy allows a vendor API (config/models.yaml)."""

from __future__ import annotations

import time
from typing import Any

from lira.llm.base import Block, Message, ModelRequest, ModelResponse, ProviderError, Usage


def to_messages_api(req: ModelRequest, model: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "system": req.system,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "messages": [_message(m) for m in req.messages],
    }
    if req.tools:
        params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in req.tools]
        if req.tool_choice in ("auto", "any"):
            params["tool_choice"] = {"type": req.tool_choice}
        else:
            params["tool_choice"] = {"type": "tool", "name": req.tool_choice}
    return params


def _message(m: Message) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for b in m.content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.tool_use_id, "name": b.name, "input": b.input})
        else:
            import json

            out.append({"type": "tool_result", "tool_use_id": b.tool_use_id, "content": json.dumps(b.content, default=str),
                        "is_error": b.is_error})
    return {"role": m.role, "content": out}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any = None):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(max_retries=0)
        self.client = client

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse:
        import anthropic

        t0 = time.perf_counter()
        try:
            r = await self.client.messages.create(**to_messages_api(req, model))
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic {e.status_code}", retryable=e.status_code in (408, 409, 429, 500, 502, 503, 529),
                                code=f"http_{e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("anthropic connection", retryable=True, code="transport") from e
        blocks: list[Block] = []
        for c in r.content:
            if c.type == "text":
                blocks.append(Block.t(c.text))
            elif c.type == "tool_use":
                blocks.append(Block("tool_use", tool_use_id=c.id, name=c.name, input=dict(c.input)))
        return ModelResponse(blocks, r.stop_reason or "end_turn", Usage(r.usage.input_tokens, r.usage.output_tokens), model,
                             "anthropic", (time.perf_counter() - t0) * 1000)
