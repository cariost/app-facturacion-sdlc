"""Tipos neutrales al proveedor para llamadas a modelos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    # Marca un punto de corte del cache de prompts: todo lo anterior es prefijo reutilizable.
    cache: bool = False


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    max_tokens: int
    system: tuple[TextBlock, ...]
    user: tuple[TextBlock, ...]
    effort: str | None = None
    output_schema: Mapping[str, Any] | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0  # entrada sin cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    model: str
    stop_reason: str | None = None
    request_id: str | None = None
    served_by_fallback: bool = False


class LLMClient(Protocol):
    @property
    def provider(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
