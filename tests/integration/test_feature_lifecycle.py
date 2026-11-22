"""Reglas deterministas del ciclo de vida forzadas por PostgreSQL."""

from __future__ import annotations

import psycopg
import pytest

from specforge.core.enums import Phase
from specforge.core.errors import (
    BlueprintVersionViolationError,
    ConcurrencyConflictError,
    ImmutableRecordError,
    InvalidPhaseTransitionError,
    RetryBudgetExceededError,
    RetryPolicyViolationError,
    UnknownModuleError,
)
from specforge.core.ids import FeatureId
from specforge.core.phases import transition_edges
from specforge.db.migrate import apply_migrations
from specforge.db.models import Feature

# Perfil de negocio de ejemplo: la política de recordatorios es configuración, no código.
WORKSHOP_CONFIG = {
    "reminder_policies": [
        {"trigger": "elapsed_time", "every": {"months": 6}, "channel": "whatsapp"},
        {"trigger": "usage_counter", "counter": "odometer_km", "every": 5000},
    ],
    "inquiry_automation": {"enabled": True, "handoff_after_turns": 3},
}


@pytest.fixture
async def feature(tx) -> Feature:
    async def create(uow):
        await uow.profiles.upsert(
            tenant_id="acme",
            code="taller-automotriz",
            display_name="Taller automotriz",
            industry="automotive_service",
            config=WORKSHOP_CONFIG,
        )
        return await uow.features.create(
            module="CRM",
            tenant_id="acme",
            business_profile="taller-automotriz",
            title="Aviso de mantenimiento periódico configurable",
            request="Avisar al cliente cuando le toque mantenimiento por tiempo o uso.",
        )

    return await tx(create)


async def _move(tx, feature: Feature, *phases: Phase, **kwargs) -> Feature:
    current = feature
    for phase in phases:
        current = await tx(
            lambda uow, p=phase, f=current: uow.features.transition(
                f.feature_id,
                to_phase=p,
                expected_row_version=f.row_version,
                reason=f"-> {p}",
                **kwargs,
            )
        )
    return current


async def test_migrations_are_idempotent(database_url: str) -> None:
    assert await apply_migrations(database_url) == []


async def test_phase_graph_parity_between_python_and_sql(database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        cur = await conn.execute("SELECT from_phase, to_phase FROM phase_transitions")
        sql_edges = {(Phase(a), Phase(b)) for a, b in await cur.fetchall()}
    assert sql_edges == transition_edges()


async def test_feature_creation_allocates_ids_and_thread(tx, feature: Feature) -> None:
    fid = FeatureId.parse(feature.feature_id)
    assert fid.module == "CRM"
    assert feature.phase == Phase.INTAKE_PO
    assert feature.current_thread_id == f"sf:{feature.feature_id}:v1:01"

    second = await tx(
        lambda uow: uow.features.create(module="CRM", tenant_id="acme", title="t", request="r")
    )
    assert FeatureId.parse(second.feature_id).seq == fid.seq + 1

    history = await tx(lambda uow: uow.features.phase_history(feature.feature_id))
    assert [(h.from_phase, h.to_phase) for h in history] == [(None, "INTAKE_PO")]


async def test_unknown_module_is_rejected(tx) -> None:
    with pytest.raises(UnknownModuleError):
        await tx(
            lambda uow: uow.features.create(module="HR", tenant_id="a", title="t", request="r")
        )


async def test_invalid_transition_is_rejected(tx, feature: Feature) -> None:
    with pytest.raises(InvalidPhaseTransitionError):
        await _move(tx, feature, Phase.IMPLEMENTATION)


async def test_stale_row_version_is_a_conflict(tx, feature: Feature) -> None:
    await _move(tx, feature, Phase.STORY_REFINEMENT)
    with pytest.raises(ConcurrencyConflictError):
        await _move(tx, feature, Phase.BLUEPRINT_DESIGN)  # usa el row_version viejo


async def test_changes_requested_bumps_blueprint_and_thread(tx, feature: Feature) -> None:
    at_gate = await _move(
        tx, feature, Phase.STORY_REFINEMENT, Phase.BLUEPRINT_DESIGN, Phase.HUMAN_GATE
    )
    with pytest.raises(BlueprintVersionViolationError):
        await _move(tx, at_gate, Phase.BLUEPRINT_DESIGN)

    revised = await _move(tx, at_gate, Phase.BLUEPRINT_DESIGN, new_blueprint_version=True)
    assert revised.blueprint_version == 2
    assert revised.run_seq == 1
    assert revised.current_thread_id == f"sf:{feature.feature_id}:v2:01"

    with pytest.raises(BlueprintVersionViolationError):
        await _move(tx, revised, Phase.HUMAN_GATE, new_blueprint_version=True)


async def test_dev_qa_retry_budget(tx, feature: Feature) -> None:
    current = await _move(
        tx,
        feature,
        Phase.STORY_REFINEMENT,
        Phase.BLUEPRINT_DESIGN,
        Phase.HUMAN_GATE,
        Phase.IMPLEMENTATION,
        Phase.QA_VERIFICATION,
    )
    with pytest.raises(RetryPolicyViolationError):
        await _move(tx, current, Phase.IMPLEMENTATION)  # sin contar el reintento

    for expected in (1, 2, 3):
        current = await _move(tx, current, Phase.IMPLEMENTATION, count_retry=True)
        assert current.retry_count == expected
        current = await _move(tx, current, Phase.QA_VERIFICATION)

    with pytest.raises(RetryBudgetExceededError):
        await _move(tx, current, Phase.IMPLEMENTATION, count_retry=True)

    failed = await _move(tx, current, Phase.FAILED)
    assert failed.closed_at is not None


async def test_blocked_resumes_only_to_previous_phase(tx, feature: Feature) -> None:
    refining = await _move(tx, feature, Phase.STORY_REFINEMENT)
    blocked = await tx(
        lambda uow: uow.features.transition(
            refining.feature_id,
            to_phase=Phase.BLOCKED,
            expected_row_version=refining.row_version,
            reason="ambigüedad crítica",
            blocked_reason="falta definir el contador de uso",
        )
    )
    assert blocked.blocked_from_phase == Phase.STORY_REFINEMENT

    with pytest.raises(InvalidPhaseTransitionError):
        await _move(tx, blocked, Phase.BLUEPRINT_DESIGN)

    resumed = await _move(tx, blocked, Phase.STORY_REFINEMENT)
    assert resumed.blocked_from_phase is None
    assert resumed.blocked_reason is None


async def test_completed_feature_is_frozen(tx, feature: Feature) -> None:
    done = await _move(
        tx,
        feature,
        Phase.STORY_REFINEMENT,
        Phase.BLUEPRINT_DESIGN,
        Phase.HUMAN_GATE,
        Phase.IMPLEMENTATION,
        Phase.QA_VERIFICATION,
        Phase.COMPLETED,
    )
    with pytest.raises(ImmutableRecordError):
        await tx(
            lambda uow: uow.features.update_working_memory(
                done.feature_id, expected_row_version=done.row_version, executive_summary="x"
            )
        )
    history = await tx(lambda uow: uow.features.phase_history(done.feature_id))
    assert history[-1].to_phase == Phase.COMPLETED


async def test_working_memory_and_new_run(tx, feature: Feature) -> None:
    updated = await tx(
        lambda uow: uow.features.update_working_memory(
            feature.feature_id,
            expected_row_version=feature.row_version,
            executive_summary="línea 1\nlínea 2",
            active_constraints=["CRM: recordatorios parametrizables por tenant"],
        )
    )
    assert updated.active_constraints == ["CRM: recordatorios parametrizables por tenant"]
    with pytest.raises(ValueError, match="15"):
        await tx(
            lambda uow: uow.features.update_working_memory(
                feature.feature_id,
                expected_row_version=updated.row_version,
                executive_summary="\n".join(["x"] * 16),
            )
        )
    rerun = await tx(
        lambda uow: uow.features.start_new_run(
            feature.feature_id, expected_row_version=updated.row_version
        )
    )
    assert rerun.current_thread_id == f"sf:{feature.feature_id}:v1:02"
