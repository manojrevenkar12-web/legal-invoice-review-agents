"""Amazon Bedrock providers: Converse (structured output through forced tool use), ApplyGuardrail, and
Knowledge Base retrieval live in their own modules. boto3 is synchronous, so calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from lira.llm.base import Block, Message, ModelRequest, ModelResponse, ProviderError, Usage

RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException", "InternalServerException",
             "ModelNotReadyException", "ServiceQuotaExceededException"}


def to_converse(req: ModelRequest, model: str, guardrail: dict[str, str] | None = None) -> dict[str, Any]:
    """Translate a request into Converse API parameters (validated against the service model in tests)."""
    params: dict[str, Any] = {
        "modelId": model,
        "system": [{"text": req.system}],
        "messages": [_message(m) for m in req.messages],
        "inferenceConfig": {"maxTokens": req.max_tokens, "temperature": req.temperature},
    }
    if req.tools:
        choice: dict[str, Any]
        if req.tool_choice == "auto":
            choice = {"auto": {}}
        elif req.tool_choice == "any":
            choice = {"any": {}}
        else:
            choice = {"tool": {"name": req.tool_choice}}
        params["toolConfig"] = {
            "tools": [{"toolSpec": {"name": t.name, "description": t.description, "inputSchema": {"json": t.input_schema}}}
                      for t in req.tools],
            "toolChoice": choice,
        }
    if guardrail:
        params["guardrailConfig"] = {"guardrailIdentifier": guardrail["id"], "guardrailVersion": guardrail["version"],
                                     "trace": "enabled"}
    if req.prompt_id:
        params["requestMetadata"] = {"prompt": f"{req.prompt_id}-v{req.prompt_version}", "tenant": req.tenant or "none"}
    return params


def _message(m: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for b in m.content:
        if b.type == "text":
            content.append({"text": b.text})
        elif b.type == "tool_use":
            content.append({"toolUse": {"toolUseId": b.tool_use_id, "name": b.name, "input": b.input}})
        else:
            payload = b.content if isinstance(b.content, dict) else {"result": b.content}
            content.append({"toolResult": {"toolUseId": b.tool_use_id, "content": [{"json": payload}],
                                           "status": "error" if b.is_error else "success"}})
    return {"role": m.role, "content": content}


def from_converse(resp: dict[str, Any], model: str, latency_ms: float) -> ModelResponse:
    blocks: list[Block] = []
    for c in resp["output"]["message"]["content"]:
        if "text" in c:
            blocks.append(Block.t(c["text"]))
        elif "toolUse" in c:
            tu = c["toolUse"]
            blocks.append(Block("tool_use", tool_use_id=tu["toolUseId"], name=tu["name"], input=tu.get("input") or {}))
    usage = resp.get("usage", {})
    stop = resp.get("stopReason", "end_turn")
    return ModelResponse(
        content=blocks, stop_reason=stop, usage=Usage(usage.get("inputTokens", 0), usage.get("outputTokens", 0)), model=model,
        provider="bedrock", latency_ms=float(resp.get("metrics", {}).get("latencyMs", latency_ms)),
        guardrail_action="GUARDRAIL_INTERVENED" if stop == "guardrail_intervened" else None,
    )


class BedrockProvider:
    name = "bedrock"

    REGIONS = {"eu.": "eu-central-1", "us.": "us-east-1", "apac.": "ap-northeast-1"}

    def __init__(self, client: Any = None, region: str = "eu-central-1", guardrail: dict[str, str] | None = None):
        """``client`` pins one client (tests); otherwise a client per region is created for the inference profile used."""
        self.client = client
        self.region = region
        self.guardrail = guardrail
        self._clients: dict[str, Any] = {}

    def client_for(self, model: str) -> Any:
        if self.client is not None:
            return self.client
        region = next((r for p, r in self.REGIONS.items() if model.startswith(p)), self.region)
        if region not in self._clients:
            import boto3

            self._clients[region] = boto3.client("bedrock-runtime", region_name=region,
                                                 config=Config(retries={"max_attempts": 1, "mode": "standard"}, read_timeout=60))
        return self._clients[region]

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse:
        params = to_converse(req, model, self.guardrail)
        t0 = time.perf_counter()
        try:
            resp = await asyncio.to_thread(self.client_for(model).converse, **params)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            raise ProviderError(f"bedrock {code}: {e}", retryable=code in RETRYABLE, code=code) from e
        except BotoCoreError as e:
            raise ProviderError(f"bedrock transport: {e}", retryable=True, code="transport") from e
        return from_converse(resp, model, (time.perf_counter() - t0) * 1000)
