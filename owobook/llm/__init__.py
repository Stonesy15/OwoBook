"""LLM providers. Pick one with LLM_PROVIDER=meta|baseten|fake.

With LLM_PROVIDER=meta and Baseten configured, Baseten is used automatically as a backup.
"""

from owobook.config import Settings, get_settings
from owobook.llm.base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage
from owobook.llm.fake import FakeProvider
from owobook.llm.openai_compat import FallbackProvider, OpenAICompatProvider

__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "Usage", "FakeProvider", "get_provider"]


def meta_provider(settings: Settings) -> OpenAICompatProvider:
    extra = {"reasoning_effort": settings.meta_reasoning_effort} if settings.meta_reasoning_effort else {}
    return OpenAICompatProvider(
        name="meta",
        base_url=settings.meta_api_base,
        api_key=settings.model_api_key,
        model=settings.meta_chat_model,
        extra_body=extra,
        timeout=settings.llm_timeout_seconds,
    )


def baseten_provider(settings: Settings) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="baseten",
        base_url=settings.baseten_base_url,
        api_key=settings.baseten_api_key,
        model=settings.baseten_model,
        timeout=settings.llm_timeout_seconds,
    )


def get_provider(name: str | None = None, settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    name = name or settings.llm_provider
    if name == "fake":
        return FakeProvider()
    if name == "baseten":
        return baseten_provider(settings)
    if name == "meta":
        primary = meta_provider(settings)
        if settings.baseten_api_key and settings.baseten_model:
            return FallbackProvider(primary, baseten_provider(settings))
        return primary
    raise ValueError(f"Unknown LLM provider {name!r}")
