"""LLM providers. Pick one with LLM_PROVIDER=meta|baseten|fake."""

from owobook.config import Settings, get_settings
from owobook.llm.base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage
from owobook.llm.fake import FakeProvider

__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "Usage", "FakeProvider", "get_provider"]


def get_provider(name: str | None = None, settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    name = name or settings.llm_provider
    if name == "fake":
        return FakeProvider()
    if name in ("meta", "baseten"):
        raise NotImplementedError(f"The '{name}' provider arrives in Phase 2. Use --provider fake for now.")
    raise ValueError(f"Unknown LLM provider {name!r}")
