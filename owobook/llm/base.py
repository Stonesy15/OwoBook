"""The small interface every LLM provider implements (OpenAI-style chat + tool calls)."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""


class LLMProvider(Protocol):
    name: str
    model: str

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, tool_choice: str = "auto"
    ) -> LLMResponse: ...


class LLMError(Exception):
    """Provider failed after retries."""
