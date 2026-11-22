"""`ModelRouter`: elige el modelo, invoca, valida contra el contrato y registra cada llamada.

Por cada llamada al proveedor:
1. `RoutingPolicy` decide el modelo (FAST/REASONING) con un motivo explícito.
2. Se abre un log RUNNING en `agent_execution_logs` en su propia transacción, de modo que
   la llamada queda trazada aunque el proceso caiga a mitad de camino.
3. Se arma el request en orden de estabilidad para el cache de prompts:
   sistema (prompt del rol + contrato) → contexto estable (packs, perfil) → contexto volátil
   → tarea → feedback de reparación.
4. Se cierra el log con tokens (entrada total, salida, cacheados), latencia y outcome.
5. La salida se valida con Pydantic y con un validador determinista opcional; si falla,
   se reintenta con los errores (hasta `max_repairs`), escalando a REASONING.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from specforge.core.enums import ExecutionOutcome
from specforge.core.errors import (
    ContractViolationError,
    LLMInvocationError,
    LLMOutputValidationError,
    LLMRefusalError,
)
from specforge.db.uow import unit_of_work
from specforge.llm.observability import InvocationInfo, NoopHook
from specforge.llm.routing import RouteRequest
from specforge.llm.schema import output_schema
from specforge.llm.types import LLMRequest, TextBlock

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from specforge.context.builder import ContextBundle
    from specforge.core.enums import ModelTier
    from specforge.db.engine import Database
    from specforge.llm.observability import InvocationHook
    from specforge.llm.prompts import RolePrompt
    from specforge.llm.routing import ModelSpec, RoutingPolicy
    from specforge.llm.types import LLMClient, LLMResponse, LLMUsage
    from specforge.schemas.state import FeatureDeliveryState

T = TypeVar("T", bound=BaseModel)
MAX_REPAIR_ERRORS = 10


@dataclass(frozen=True, slots=True)
class CallRecord:
    log_id: uuid.UUID
    model: str
    tier: ModelTier
    outcome: ExecutionOutcome
    usage: LLMUsage | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class RoutedResult(Generic[T]):
    output: T
    calls: tuple[CallRecord, ...]

    @property
    def log_id(self) -> uuid.UUID:
        """Log de la llamada exitosa (se enlaza al artefacto persistido)."""
        return self.calls[-1].log_id

    @property
    def model(self) -> str:
        return self.calls[-1].model


def _format_validation_errors(exc: ValidationError) -> list[str]:
    errors = []
    for err in exc.errors()[:MAX_REPAIR_ERRORS]:
        location = ".".join(str(p) for p in err["loc"]) or "<raíz>"
        errors.append(f"{location}: {err['msg']}")
    return errors


class ModelRouter:
    def __init__(
        self,
        *,
        db: Database,
        client: LLMClient,
        policy: RoutingPolicy,
        max_repairs: int = 1,
        hook: InvocationHook | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._db = db
        self._client = client
        self._policy = policy
        self._max_repairs = max_repairs
        self._hook = hook or NoopHook()
        self._clock = clock

    async def invoke(
        self,
        *,
        state: FeatureDeliveryState,
        prompt: RolePrompt,
        bundle: ContextBundle,
        task: str,
        output_model: type[T],
        validator: Callable[[T], None] | None = None,
        force_tier: ModelTier | None = None,
    ) -> RoutedResult[T]:
        if prompt.role is not bundle.role:
            raise ValueError(f"prompt de {prompt.role.value} con contexto de {bundle.role.value}")
        system = (TextBlock(prompt.system_text(output_model), cache=True),)
        prompt_hash = prompt.prompt_hash(output_model)
        schema = output_schema(output_model)
        stable, volatile = bundle.stable_text(), bundle.volatile_text()
        attempt = state.retry_count + 1
        calls: list[CallRecord] = []
        repair_notes: list[str] = []

        for repair in range(self._max_repairs + 1):
            decision = self._policy.route(
                RouteRequest(
                    role=bundle.role,
                    node=bundle.node,
                    context_tokens=bundle.total_tokens,
                    attempt=attempt,
                    repair=repair,
                    force_tier=force_tier,
                )
            )
            spec = decision.spec
            user = self._user_blocks(stable, volatile, task, repair_notes)
            request = LLMRequest(
                model=spec.name,
                max_tokens=spec.max_output_tokens,
                system=system,
                user=user,
                effort=spec.effort,
                output_schema=schema,
                metadata={"feature_id": state.feature_id, "node": bundle.node},
            )
            span = self._hook.start(
                InvocationInfo(
                    feature_id=state.feature_id,
                    thread_id=state.thread_id,
                    node=bundle.node,
                    role=bundle.role.value,
                    model=spec.name,
                    tier=spec.tier.value,
                    prompt_id=prompt.prompt_id,
                    attempt=attempt,
                    repair=repair,
                )
            )
            log_id = await self._start_log(
                state=state,
                bundle=bundle,
                spec=spec,
                prompt=prompt,
                prompt_hash=prompt_hash,
                attempt=attempt,
                metadata={
                    "routing_reason": decision.reason,
                    "repair": repair,
                    "context_tokens_estimate": bundle.total_tokens,
                    "context_keys": bundle.segment_keys(),
                    "context_dropped": list(bundle.dropped),
                },
                trace_id=span.trace_id,
                span_id=span.span_id,
            )
            started = self._clock()
            try:
                response = await self._client.complete(request)
            except Exception as exc:
                latency = self._elapsed_ms(started)
                await self._finish_log(
                    log_id,
                    outcome=ExecutionOutcome.LLM_ERROR,
                    response=None,
                    latency_ms=latency,
                    error=exc,
                )
                span.end(
                    outcome=ExecutionOutcome.LLM_ERROR, usage=None, latency_ms=latency, error=exc
                )
                if isinstance(exc, LLMInvocationError):
                    raise
                raise LLMInvocationError(f"{type(exc).__name__}: {exc}") from exc

            latency = self._elapsed_ms(started)
            output, violations = self._validate(response.text, output_model, validator)
            outcome = (
                ExecutionOutcome.SUCCESS
                if output is not None
                else (ExecutionOutcome.VALIDATION_ERROR)
            )
            await self._finish_log(
                log_id,
                outcome=outcome,
                response=response,
                latency_ms=latency,
                error=None,
                violations=violations,
            )
            span.end(outcome=outcome, usage=response.usage, latency_ms=latency, error=None)
            calls.append(
                CallRecord(log_id, response.model, spec.tier, outcome, response.usage, latency)
            )
            if output is not None:
                return RoutedResult(output=output, calls=tuple(calls))
            repair_notes = violations

        raise LLMOutputValidationError(repair_notes)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _user_blocks(
        stable: str, volatile: str, task: str, repair_notes: list[str]
    ) -> tuple[TextBlock, ...]:
        blocks: list[TextBlock] = []
        if stable:
            blocks.append(TextBlock(stable, cache=True))
        if volatile:
            blocks.append(TextBlock(volatile))
        blocks.append(TextBlock(f"<task>\n{task.strip()}\n</task>"))
        if repair_notes:
            errors = "\n".join(f"- {e}" for e in repair_notes)
            blocks.append(
                TextBlock(
                    "<repair>\nTu respuesta anterior no cumplió el contrato. Corrige estos "
                    f"errores y responde de nuevo con el objeto completo:\n{errors}\n</repair>"
                )
            )
        return tuple(blocks)

    @staticmethod
    def _validate(
        text: str, output_model: type[T], validator: Callable[[T], None] | None
    ) -> tuple[T | None, list[str]]:
        try:
            output = output_model.model_validate_json(text)
        except ValidationError as exc:
            return None, _format_validation_errors(exc)
        if validator is not None:
            try:
                validator(output)
            except ContractViolationError as exc:
                return None, exc.violations[:MAX_REPAIR_ERRORS]
        return output, []

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    async def _start_log(
        self,
        *,
        state: FeatureDeliveryState,
        bundle: ContextBundle,
        spec: ModelSpec,
        prompt: RolePrompt,
        prompt_hash: str,
        attempt: int,
        metadata: dict[str, Any],
        trace_id: str | None,
        span_id: str | None,
    ) -> uuid.UUID:
        async with unit_of_work(self._db) as uow:
            log = await uow.logs.start(
                feature_id=state.feature_id,
                tenant_id=state.tenant_id,
                thread_id=state.thread_id,
                node_name=bundle.node,
                agent_role=bundle.role,
                phase=bundle.phase,
                attempt=attempt,
                model_provider=spec.provider,
                model_name=spec.name,
                model_tier=spec.tier,
                prompt_id=prompt.prompt_id,
                prompt_hash=prompt_hash,
                context_pack_refs=bundle.pack_refs,
                input_artifact_ids=bundle.input_artifact_ids,
                trace_id=trace_id,
                span_id=span_id,
                metadata=metadata,
            )
            return log.log_id

    async def _finish_log(
        self,
        log_id: uuid.UUID,
        *,
        outcome: ExecutionOutcome,
        response: LLMResponse | None,
        latency_ms: int,
        error: BaseException | None,
        violations: list[str] | None = None,
    ) -> None:
        usage = response.usage if response is not None else None
        extra: dict[str, Any] = {}
        if response is not None:
            extra |= {
                "served_model": response.model,
                "stop_reason": response.stop_reason,
                "cache_write_tokens": response.usage.cache_write_tokens,
                "served_by_fallback": response.served_by_fallback,
            }
            if response.request_id:
                extra["provider_request_id"] = response.request_id
        if violations:
            extra["violations"] = violations
        if isinstance(error, LLMRefusalError) and error.category:
            extra["refusal_category"] = error.category
        error_type = type(error).__name__ if error is not None else None
        if error is None and violations:
            error_type = "OutputValidation"
        message = str(error) if error is not None else ("; ".join(violations or []) or None)
        async with unit_of_work(self._db) as uow:
            await uow.logs.finish(
                log_id,
                outcome=outcome,
                # input_tokens = entrada total (sin cache + leída + escrita en cache);
                # cached_tokens = porción servida desde cache.
                input_tokens=usage.total_input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cached_tokens=usage.cache_read_tokens if usage else 0,
                latency_ms=latency_ms,
                error_type=error_type,
                error_message=message[:2000] if message else None,
                metadata=extra,
            )
