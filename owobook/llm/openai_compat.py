"""Providers for OpenAI-compatible chat completions: Meta Model API (Muse Spark) and Baseten.

Docs checked 27 Oct 2026:
- Meta:    POST https://api.meta.ai/v1/chat/completions, Bearer MODEL_API_KEY
- Baseten: POST https://inference.baseten.co/v1/chat/completions, Bearer BASETEN_API_KEY
"""

import json
import logging
import time
from typing import Any

import httpx

from owobook.http import HTTPError, request_with_retries
from owobook.llm.base import LLMError, LLMResponse, ToolCall, Usage

log = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 45.0,
        retries: int = 2,
    ):
        if not api_key:
            raise LLMError(f"{name}: API key is missing — set it in .env (or use LLM_PROVIDER=fake)")
        if not base_url or not model:
            raise LLMError(f"{name}: base URL and model must be set in .env")
        self.name = name
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.extra_body = extra_body or {}
        self.retries = retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, tool_choice: str = "auto"
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages, **self.extra_body}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        started = time.monotonic()
        try:
            resp = await request_with_retries(
                self._client, "POST", self.url, json=body, retries=self.retries, label=f"{self.name} chat"
            )
        except HTTPError as exc:
            raise LLMError(str(exc)) from exc
        log.info("%s %s answered in %.1fs", self.name, self.model, time.monotonic() - started)
        return parse_chat_response(resp.json(), provider=self.name, model=self.model)

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_chat_response(data: dict[str, Any], *, provider: str, model: str) -> LLMResponse:
    """Turn an OpenAI-style chat completion into our LLMResponse."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{provider}: unexpected response shape: {str(data)[:300]}") from exc

    calls = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        args_text = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_text) if isinstance(args_text, str) else dict(args_text)
        except json.JSONDecodeError:
            # The docs warn arguments can be invalid JSON. An empty dict fails validation,
            # and the error goes back to the model so it can try again.
            log.warning("%s returned invalid tool arguments: %s", provider, args_text[:200])
            args = {}
        calls.append(ToolCall(id=raw.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))

    usage = data.get("usage") or {}
    return LLMResponse(
        content=message.get("content"),
        tool_calls=calls,
        usage=Usage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0)),
        provider=provider,
        model=data.get("model") or model,
    )


class FallbackProvider:
    """Try the primary provider; if it fails (e.g. Meta account restricted), use the backup."""

    def __init__(self, primary, backup):
        self.primary, self.backup = primary, backup
        self.name = f"{primary.name}+{backup.name}"
        self.model = primary.model

    async def chat(self, messages, tools=None, tool_choice="auto") -> LLMResponse:
        try:
            return await self.primary.chat(messages, tools, tool_choice)
        except LLMError as exc:
            log.error("%s failed, falling back to %s: %s", self.primary.name, self.backup.name, exc)
            return await self.backup.chat(messages, tools, tool_choice)
