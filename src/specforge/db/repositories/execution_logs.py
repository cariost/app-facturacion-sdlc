"""Repositorio de `agent_execution_logs`: tokens, latencia y outcome por nodo/agente."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from specforge.core.enums import ExecutionOutcome
from specforge.core.errors import NotFoundError
from specforge.db.models import AgentExecutionLog

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from specforge.core.enums import AgentRole, ModelTier, Phase


@dataclass(frozen=True, slots=True)
class TokenUsage:
    model_tier: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int


class ExecutionLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def start(
        self,
        *,
        feature_id: str,
        tenant_id: str,
        thread_id: str,
        node_name: str,
        agent_role: AgentRole,
        phase: Phase,
        attempt: int = 1,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_tier: ModelTier | None = None,
        prompt_id: str | None = None,
        prompt_hash: str | None = None,
        context_pack_refs: Sequence[str] = (),
        input_artifact_ids: Sequence[uuid.UUID] = (),
        trace_id: str | None = None,
        span_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentExecutionLog:
        stmt = (
            pg_insert(AgentExecutionLog)
            .values(
                feature_id=feature_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                node_name=node_name,
                agent_role=agent_role.value,
                phase=phase.value,
                attempt=attempt,
                model_provider=model_provider,
                model_name=model_name,
                model_tier=model_tier.value if model_tier is not None else None,
                prompt_id=prompt_id,
                prompt_hash=prompt_hash,
                context_pack_refs=list(context_pack_refs),
                input_artifact_ids=list(input_artifact_ids),
                trace_id=trace_id,
                span_id=span_id,
                metadata_=dict(metadata or {}),
            )
            .returning(AgentExecutionLog)
        )
        return (await self._s.scalars(stmt)).one()

    async def finish(
        self,
        log_id: uuid.UUID,
        *,
        outcome: ExecutionOutcome,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        langsmith_run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentExecutionLog:
        """Cierra un log en curso. El trigger rechaza cerrar dos veces (SF004)."""
        if outcome is ExecutionOutcome.RUNNING:
            raise ValueError("finish() requiere un outcome final")
        values: dict[str, Any] = {
            "outcome": outcome.value,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "latency_ms": latency_ms,
            "error_type": error_type,
            "error_message": error_message,
            "langsmith_run_id": langsmith_run_id,
            "finished_at": func.clock_timestamp(),
        }
        if metadata:
            values["metadata_"] = AgentExecutionLog.metadata_.concat(dict(metadata))
        stmt = (
            update(AgentExecutionLog)
            .where(AgentExecutionLog.log_id == log_id)
            .values(**values)
            .returning(AgentExecutionLog)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        log = (await self._s.scalars(stmt)).one_or_none()
        if log is None:
            raise NotFoundError(f"log no encontrado: {log_id}")
        return log

    async def list_for_feature(self, feature_id: str) -> list[AgentExecutionLog]:
        stmt = (
            select(AgentExecutionLog)
            .where(AgentExecutionLog.feature_id == feature_id)
            .order_by(AgentExecutionLog.started_at)
        )
        return list((await self._s.scalars(stmt)).all())

    async def usage_by_tier(self, feature_id: str) -> list[TokenUsage]:
        """Consumo agregado por tier de modelo (base del reporte de costos)."""
        stmt = (
            select(
                AgentExecutionLog.model_tier,
                func.count(),
                func.coalesce(func.sum(AgentExecutionLog.input_tokens), 0),
                func.coalesce(func.sum(AgentExecutionLog.output_tokens), 0),
                func.coalesce(func.sum(AgentExecutionLog.cached_tokens), 0),
                func.coalesce(func.sum(AgentExecutionLog.latency_ms), 0),
            )
            .where(AgentExecutionLog.feature_id == feature_id)
            .group_by(AgentExecutionLog.model_tier)
            .order_by(AgentExecutionLog.model_tier)
        )
        rows = (await self._s.execute(stmt)).all()
        return [
            TokenUsage(tier, int(calls), int(inp), int(out), int(cached), int(lat))
            for tier, calls, inp, out, cached, lat in rows
        ]
