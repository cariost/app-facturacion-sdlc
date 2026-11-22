"""Hooks de trazas para cada invocación de modelo (OpenTelemetry o compatibles).

El `trace_id` y `span_id` del span se guardan en `agent_execution_logs`, de modo que un log
de la BD se puede abrir directamente en el backend de trazas (LangSmith acepta OTLP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specforge.core.enums import ExecutionOutcome
    from specforge.llm.types import LLMUsage


@dataclass(frozen=True, slots=True)
class InvocationInfo:
    feature_id: str
    thread_id: str
    node: str
    role: str
    model: str
    tier: str
    prompt_id: str
    attempt: int
    repair: int


class InvocationSpan(Protocol):
    @property
    def trace_id(self) -> str | None: ...

    @property
    def span_id(self) -> str | None: ...

    def end(
        self,
        *,
        outcome: ExecutionOutcome,
        usage: LLMUsage | None,
        latency_ms: int,
        error: BaseException | None,
    ) -> None: ...


class InvocationHook(Protocol):
    def start(self, info: InvocationInfo) -> InvocationSpan: ...


class _NoopSpan:
    trace_id: str | None = None
    span_id: str | None = None

    def end(
        self,
        *,
        outcome: ExecutionOutcome,
        usage: LLMUsage | None,
        latency_ms: int,
        error: BaseException | None,
    ) -> None:
        return None


class NoopHook:
    def start(self, info: InvocationInfo) -> InvocationSpan:
        return _NoopSpan()


class _OtelSpan:
    def __init__(self, span: Any) -> None:
        self._span = span
        ctx = span.get_span_context()
        self._trace_id = f"{ctx.trace_id:032x}" if ctx.trace_id else None
        self._span_id = f"{ctx.span_id:016x}" if ctx.span_id else None

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def span_id(self) -> str | None:
        return self._span_id

    def end(
        self,
        *,
        outcome: ExecutionOutcome,
        usage: LLMUsage | None,
        latency_ms: int,
        error: BaseException | None,
    ) -> None:
        attributes: dict[str, str | int] = {
            "specforge.outcome": outcome.value,
            "specforge.latency_ms": latency_ms,
        }
        if usage is not None:
            attributes |= {
                "gen_ai.usage.input_tokens": usage.total_input_tokens,
                "gen_ai.usage.output_tokens": usage.output_tokens,
                "gen_ai.usage.cache_read_input_tokens": usage.cache_read_tokens,
            }
        self._span.set_attributes(attributes)
        if error is not None:
            self._span.record_exception(error)
        self._span.end()


class OpenTelemetryHook:
    """Requiere `opentelemetry-api` (extra `observability`)."""

    def __init__(self, tracer_name: str = "specforge.llm") -> None:
        try:
            from opentelemetry import trace
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ImportError("instala el extra `specforge[observability]`") from exc
        self._tracer = trace.get_tracer(tracer_name)

    def start(self, info: InvocationInfo) -> InvocationSpan:
        attributes: Mapping[str, str | int] = {
            "specforge.feature_id": info.feature_id,
            "specforge.thread_id": info.thread_id,
            "specforge.node": info.node,
            "specforge.role": info.role,
            "specforge.prompt_id": info.prompt_id,
            "specforge.attempt": info.attempt,
            "specforge.repair": info.repair,
            "gen_ai.request.model": info.model,
            "specforge.model_tier": info.tier,
        }
        span = self._tracer.start_span(f"llm {info.node}", attributes=dict(attributes))
        return _OtelSpan(span)
