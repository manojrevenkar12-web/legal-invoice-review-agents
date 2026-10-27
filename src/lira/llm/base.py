"""Provider-neutral model calls: messages, tools, structured output, usage.

The shapes follow the Bedrock Converse API (content blocks, toolUse/toolResult), which also maps one to one
onto the Anthropic Messages API, so every provider adapter is a thin translation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class ProviderError(Exception):
    """A provider could not answer. ``retryable`` decides whether the router tries the next model."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class InvalidOutput(ProviderError):
    """The model answered, but not in the required shape (after repair attempts)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=True, code="invalid_output")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Block:
    type: Literal["text", "tool_use", "tool_result"]
    text: str = ""
    tool_use_id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: Any = None  # tool_result payload (JSON-serialisable)
    is_error: bool = False

    @staticmethod
    def t(text: str) -> Block:
        return Block("text", text=text)


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[Block]


@dataclass
class ModelRequest:
    task: str  # "guideline_compile", "line_review", "appeal_review", "qa_answer", "judge"
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: Literal["auto", "any"] | str = "auto"  # a tool name forces that tool (structured output)
    max_tokens: int = 2048
    temperature: float = 0.0
    prompt_id: str = ""
    prompt_version: int = 0
    tenant: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, o: Usage) -> Usage:
        return Usage(self.input_tokens + o.input_tokens, self.output_tokens + o.output_tokens)


@dataclass
class ModelResponse:
    content: list[Block]
    stop_reason: str
    usage: Usage
    model: str
    provider: str
    latency_ms: float = 0.0
    guardrail_action: str | None = None  # "GUARDRAIL_INTERVENED" when a Bedrock guardrail blocked or masked

    def tool_uses(self) -> list[Block]:
        return [b for b in self.content if b.type == "tool_use"]

    def text(self) -> str:
        return "".join(b.text for b in self.content if b.type == "text")

    def tool_input(self, name: str) -> dict[str, Any]:
        for b in self.tool_uses():
            if b.name == name:
                return b.input
        raise InvalidOutput(f"the model did not call {name}")


class Provider(Protocol):
    name: str

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse: ...


def dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
