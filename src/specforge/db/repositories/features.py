"""Repositorio de features: identidad, fases (con lock optimista) y memoria de trabajo."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from specforge.core.enums import Phase
from specforge.core.errors import ConcurrencyConflictError, NotFoundError, UnknownModuleError
from specforge.core.ids import FeatureId, validate_module_code
from specforge.db.models import Feature, FeaturePhaseHistory, Module
from specforge.db.repositories._common import MAX_SUMMARY_LINES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from specforge.core.transition_guard import TransitionDecision


class FeatureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def allocate_id(self, module: str) -> FeatureId:
        """Reserva atómicamente el siguiente `<MODULO>-<NNN>` (lock de fila en `modules`)."""
        validate_module_code(module)
        stmt = (
            update(Module)
            .where(Module.code == module)
            .values(last_feature_seq=Module.last_feature_seq + 1)
            .returning(Module.last_feature_seq)
        )
        seq = (await self._s.execute(stmt)).scalar_one_or_none()
        if seq is None:
            raise UnknownModuleError(f"módulo no registrado: {module}")
        return FeatureId(module=module, seq=seq)

    async def create(
        self,
        *,
        module: str,
        tenant_id: str,
        title: str,
        request: str,
        business_profile: str | None = None,
        max_retries: int = 3,
        active_constraints: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> Feature:
        feature_id = await self.allocate_id(module)
        stmt = (
            pg_insert(Feature)
            .values(
                feature_id=str(feature_id),
                module=module,
                tenant_id=tenant_id,
                business_profile=business_profile,
                title=title,
                request=request,
                max_retries=max_retries,
                active_constraints=list(active_constraints),
                metadata_=dict(metadata or {}),
            )
            .returning(Feature)
        )
        return (await self._s.scalars(stmt)).one()

    async def get(self, feature_id: str) -> Feature:
        feature = await self._s.get(Feature, feature_id, populate_existing=True)
        if feature is None:
            raise NotFoundError(f"feature no encontrada: {feature_id}")
        return feature

    async def find(
        self,
        *,
        tenant_id: str | None = None,
        module: str | None = None,
        phase: Phase | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Feature]:
        stmt = select(Feature).order_by(Feature.updated_at.desc()).limit(limit).offset(offset)
        if tenant_id is not None:
            stmt = stmt.where(Feature.tenant_id == tenant_id)
        if module is not None:
            stmt = stmt.where(Feature.module == module)
        if phase is not None:
            stmt = stmt.where(Feature.phase == phase.value)
        return list((await self._s.scalars(stmt)).all())

    async def transition(
        self,
        feature_id: str,
        *,
        to_phase: Phase,
        expected_row_version: int,
        reason: str,
        blocked_reason: str | None = None,
        count_retry: bool = False,
        new_blueprint_version: bool = False,
    ) -> Feature:
        """Cambia de fase con lock optimista. La validez la decide el trigger de BD."""
        if (to_phase is Phase.BLOCKED) != (blocked_reason is not None):
            raise ValueError("blocked_reason es obligatorio solo al pasar a BLOCKED")
        values: dict[str, Any] = {"phase": to_phase.value, "last_transition_reason": reason}
        if blocked_reason is not None:
            values["blocked_reason"] = blocked_reason
        if count_retry:
            values["retry_count"] = Feature.retry_count + 1
        if new_blueprint_version:
            values["blueprint_version"] = Feature.blueprint_version + 1
        return await self._guarded_update(feature_id, expected_row_version, values)

    async def apply(
        self, feature_id: str, decision: TransitionDecision, *, expected_row_version: int
    ) -> Feature:
        """Persiste una decisión del `TransitionGuard` (misma regla que el trigger de BD)."""
        return await self.transition(
            feature_id,
            to_phase=decision.to_phase,
            expected_row_version=expected_row_version,
            reason=decision.reason,
            blocked_reason=decision.blocked_reason,
            count_retry=decision.count_retry,
            new_blueprint_version=decision.new_blueprint_version,
        )

    async def update_working_memory(
        self,
        feature_id: str,
        *,
        expected_row_version: int,
        executive_summary: str | None = None,
        active_constraints: Sequence[str] | None = None,
    ) -> Feature:
        values: dict[str, Any] = {}
        if executive_summary is not None:
            if len(executive_summary.splitlines()) > MAX_SUMMARY_LINES:
                raise ValueError(f"executive_summary excede {MAX_SUMMARY_LINES} líneas")
            values["executive_summary"] = executive_summary
        if active_constraints is not None:
            values["active_constraints"] = list(active_constraints)
        if not values:
            return await self.get(feature_id)
        return await self._guarded_update(feature_id, expected_row_version, values)

    async def start_new_run(self, feature_id: str, *, expected_row_version: int) -> Feature:
        """Nueva ejecución (nuevo `thread_id`) sobre la misma versión del blueprint."""
        return await self._guarded_update(
            feature_id, expected_row_version, {"run_seq": Feature.run_seq + 1}
        )

    async def phase_history(self, feature_id: str) -> list[FeaturePhaseHistory]:
        stmt = (
            select(FeaturePhaseHistory)
            .where(FeaturePhaseHistory.feature_id == feature_id)
            .order_by(FeaturePhaseHistory.history_id)
        )
        return list((await self._s.scalars(stmt)).all())

    async def _guarded_update(
        self, feature_id: str, expected_row_version: int, values: dict[str, Any]
    ) -> Feature:
        stmt = (
            update(Feature)
            .where(Feature.feature_id == feature_id, Feature.row_version == expected_row_version)
            .values(**values)
            .returning(Feature)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        feature = (await self._s.scalars(stmt)).one_or_none()
        if feature is not None:
            return feature
        current = await self._s.scalar(
            select(Feature.row_version).where(Feature.feature_id == feature_id)
        )
        if current is None:
            raise NotFoundError(f"feature no encontrada: {feature_id}")
        raise ConcurrencyConflictError(
            f"{feature_id}: row_version esperado {expected_row_version}, actual {current}"
        )
