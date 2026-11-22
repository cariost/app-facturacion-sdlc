"""Adaptador `LLMClient` para la API de Anthropic (SDK oficial `anthropic`, extra `llm`).

- Prompt caching: cada `TextBlock(cache=True)` lleva `cache_control: ephemeral`. Con el orden
  sistema → contexto estable → volátil, el prompt del rol y los Context Packs se leen desde
  cache entre llamadas del mismo rol y módulo.
- Salida estructurada: `output_config.format` con el JSON schema simplificado del contrato.
- `output_config.effort` controla la profundidad de razonamiento por tier.
- Fallback ante rechazos: en los modelos que lo admiten se activa el fallback del lado del
  servidor (`fallbacks="default"`); si toda la cadena rechaza, se lanza `LLMRefusalError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from specforge.core.errors import LLMInvocationError, LLMRefusalError
from specforge.llm.types import LLMResponse, LLMUsage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from specforge.llm.types import LLMRequest, TextBlock

FALLBACK_BETA = "server-side-fallback-2026-07-01"
SERVER_FALLBACK_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-fable-5-2",
        "claude-fable-5-1",
    }
)


def _block(block: TextBlock) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "text", "text": block.text}
    if block.cache:
        payload["cache_control"] = {"type": "ephemeral"}
    return payload


def to_anthropic_params(request: LLMRequest) -> dict[str, Any]:
    """Traduce un `LLMRequest` a los parámetros de `messages.create`."""
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": [_block(b) for b in request.system],
        "messages": [{"role": "user", "content": [_block(b) for b in request.user]}],
    }
    output_config: dict[str, Any] = {}
    if request.effort:
        output_config["effort"] = request.effort
    if request.output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": dict(request.output_schema)}
    if output_config:
        params["output_config"] = output_config
    return params


def from_anthropic_message(message: Any) -> LLMResponse:
    """Normaliza un `Message`/`BetaMessage` a `LLMResponse` (valida `stop_reason`)."""
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        raise LLMRefusalError(f"el modelo {message.model} rechazó la solicitud", category=category)
    if stop_reason == "max_tokens":
        raise LLMInvocationError("respuesta truncada por max_tokens")
    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    usage = message.usage
    iterations = getattr(usage, "iterations", None) or []
    return LLMResponse(
        text=text,
        usage=LLMUsage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        ),
        model=message.model,
        stop_reason=stop_reason,
        request_id=getattr(message, "_request_id", None),
        served_by_fallback=any(getattr(i, "type", None) == "fallback_message" for i in iterations),
    )


class AnthropicLLMClient:
    def __init__(
        self, client: AsyncAnthropic | None = None, *, refusal_fallback: bool = True
    ) -> None:
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depende del entorno
                raise ImportError("instala el extra `specforge[llm]`") from exc
            client = AsyncAnthropic()
        self._client = client
        self._refusal_fallback = refusal_fallback

    @property
    def provider(self) -> str:
        return "anthropic"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        params = to_anthropic_params(request)
        try:
            if self._refusal_fallback and request.model in SERVER_FALLBACK_MODELS:
                message: Any = await self._client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                message = await self._client.messages.create(**params)
        except anthropic.APIStatusError as exc:
            raise LLMInvocationError(f"HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMInvocationError(f"conexión: {exc}") from exc
        return from_anthropic_message(message)
