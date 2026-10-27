"""Tracing with OpenTelemetry. Model calls carry the GenAI semantic-convention attributes, which Langfuse reads
when it is configured (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST); otherwise spans go wherever the
process's tracer provider sends them (an in-memory exporter in tests, AWS X-Ray via the ADOT collector on ECS).

Span attributes never contain invoice text or names: ids, codes, counts, tokens and costs only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

_tracer = trace.get_tracer("lira")
_configured = False


def setup(service_name: str = "lira", exporter: SpanExporter | None = None) -> None:
    """Configure tracing once per process. Langfuse attaches its own span processor when its keys are present."""
    global _configured, _tracer
    if _configured and exporter is None:
        return
    provider = TracerProvider()
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider) if not _configured else None
    _tracer = provider.get_tracer("lira")
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        from langfuse import Langfuse

        Langfuse(tracer_provider=provider)  # exports this provider's spans to Langfuse
    _configured = True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with _tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, str | bool | int | float) else str(v))
        yield s


def model_call_attributes(task: str, model: str, provider: str, prompt_id: str, prompt_version: int, tenant: str) -> dict[str, Any]:
    return {
        "langfuse.observation.type": "generation",
        "gen_ai.operation.name": "chat",
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "lira.task": task,
        "lira.prompt": f"{prompt_id}@v{prompt_version}",
        "langfuse.observation.prompt.name": prompt_id,
        "langfuse.observation.prompt.version": prompt_version,
        "lira.tenant": tenant,
    }
