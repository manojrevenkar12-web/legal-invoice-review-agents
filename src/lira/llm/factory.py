"""Build the model router for this process: simulated (default) or live Bedrock / Anthropic providers."""

from __future__ import annotations

import os

from lira import config
from lira.llm.base import Provider
from lira.llm.router import ModelRouter
from lira.llm.simulated import SimulatedProvider


def make_router(mode: str | None = None) -> ModelRouter:
    mode = mode or config.model_mode()
    if mode == "simulated":
        return ModelRouter({"simulated": SimulatedProvider()})
    providers: dict[str, Provider] = {}
    from lira.llm.bedrock import BedrockProvider

    guardrail = None
    if os.environ.get("LIRA_GUARDRAIL_ID"):
        guardrail = {"id": os.environ["LIRA_GUARDRAIL_ID"], "version": os.environ.get("LIRA_GUARDRAIL_VERSION", "DRAFT")}
    providers["bedrock"] = BedrockProvider(guardrail=guardrail)
    if os.environ.get("ANTHROPIC_API_KEY"):
        from lira.llm.anthropic_api import AnthropicProvider

        providers["anthropic"] = AnthropicProvider()
    return ModelRouter(providers)
