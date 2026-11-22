"""El `TransitionGuard` y los triggers de PostgreSQL deben coincidir en cada decisión."""

from __future__ import annotations

import pytest

from specforge.core.enums import AgentRole, ArtifactType, Phase
from specforge.core.transition_guard import TransitionEvent, TransitionGuard
from specforge.db.models import Feature
from specforge.schemas import FeatureDeliveryState, StoryRefinementOutput, load_as, serialize
from specforge.schemas.stories import StoryChange, StoryChangeType, validate_progression
from tests.factories import blueprint, inquiry_story, intake_brief, refinement_v1, reminder_story

guard = TransitionGuard()
E = TransitionEvent


async def _step(tx, feature: Feature, event: TransitionEvent, reason: str | None = None) -> Feature:
    decision = guard.decide(feature, event, reason=reason)
    updated = await tx(
        lambda uow: uow.features.apply(
            feature.feature_id, decision, expected_row_version=feature.row_version
        )
    )
    assert updated.phase == decision.to_phase
    # La proyección local del State coincide con lo que dejó el trigger.
    state = FeatureDeliveryState.from_feature(feature)
    projected = state.model_copy(
        update=state.after_transition(decision, row_version=updated.row_version)
    )
    assert FeatureDeliveryState.from_feature(updated).model_dump(
        exclude={"qa_errors"}
    ) == projected.model_dump(exclude={"qa_errors"})
    return updated


async def _store(tx, feature: Feature, contract, role: AgentRole):
    serialized = serialize(contract)
    return await tx(
        lambda uow: uow.artifacts.add(
            feature_id=feature.feature_id,
            artifact_type=serialized.artifact_type,
            content=serialized.content,
            produced_by=role,
            schema_name=serialized.schema_name,
            schema_version=serialized.schema_version,
            phase=Phase(feature.phase),
            blueprint_version=feature.blueprint_version,
            thread_id=feature.current_thread_id,
        )
    )


@pytest.fixture
async def feature(tx) -> Feature:
    return await tx(
        lambda uow: uow.features.create(
            module="CRM",
            tenant_id="acme",
            title="Recordatorios y consultas",
            request="r",
            max_retries=2,
        )
    )


async def test_full_lifecycle_driven_by_guard(tx, feature: Feature) -> None:
    brief = intake_brief()
    await _store(tx, feature, brief, AgentRole.PO)
    f = await _step(tx, feature, E.INTAKE_COMPLETED)

    refinement = refinement_v1()
    validate_progression(brief.draft_stories, refinement)
    stored = await _store(tx, f, refinement, AgentRole.BUSINESS_EXPERT)
    restored = load_as(
        StoryRefinementOutput, ArtifactType.STORY_REFINEMENT, stored.schema_name, stored.content
    )
    assert restored.ready_for_blueprint
    f = await _step(tx, f, E.STORIES_READY)

    bp1 = blueprint(1)
    bp1.check_against(restored)
    await _store(tx, f, bp1, AgentRole.ARCHITECT_TECH_LEAD)
    f = await _step(tx, f, E.BLUEPRINT_READY)

    # El humano pide cambios en las historias: nueva versión de blueprint y thread.
    f = await _step(tx, f, E.STORY_CHANGES_REQUESTED, "falta canal de envío")
    assert f.current_thread_id == f"sf:{f.feature_id}:v2:01"
    assert f.phase == Phase.STORY_REFINEMENT

    refined = reminder_story(
        i_want="que se avise al cliente por el canal configurado cuando le toque su servicio",
        revision=3,
    )
    refinement_v2 = StoryRefinementOutput(
        stories=[refined, inquiry_story()],
        changes=[
            StoryChange(story_id="US-001", change_type=StoryChangeType.MODIFIED, rationale="canal")
        ],
    )
    validate_progression(restored.stories, refinement_v2)
    v2 = await _store(tx, f, refinement_v2, AgentRole.BUSINESS_EXPERT)
    assert (v2.version, v2.supersedes_artifact_id) == (2, stored.artifact_id)

    f = await _step(tx, f, E.STORIES_READY)
    blueprint(2).check_against(refinement_v2)
    f = await _step(tx, f, E.BLUEPRINT_READY)
    f = await _step(tx, f, E.BLUEPRINT_APPROVED)

    # Ciclo Dev ↔ QA con presupuesto de 2 reintentos.
    f = await _step(tx, f, E.IMPLEMENTATION_DONE)
    f = await _step(tx, f, E.QA_FAILED)
    assert (f.phase, f.retry_count) == (Phase.IMPLEMENTATION, 1)
    f = await _step(tx, f, E.IMPLEMENTATION_DONE)
    f = await _step(tx, f, E.QA_PASSED)
    assert f.phase == Phase.COMPLETED
    assert f.closed_at is not None

    history = await tx(lambda uow: uow.features.phase_history(f.feature_id))
    assert [h.to_phase for h in history][-4:] == [
        "QA_VERIFICATION",
        "IMPLEMENTATION",
        "QA_VERIFICATION",
        "COMPLETED",
    ]
    assert [h.blueprint_version for h in history][-1] == 2


async def test_exhausted_retries_end_in_failed(tx, feature: Feature) -> None:
    f = feature
    for event in (
        E.INTAKE_COMPLETED,
        E.STORIES_READY,
        E.BLUEPRINT_READY,
        E.BLUEPRINT_APPROVED,
        E.IMPLEMENTATION_DONE,
    ):
        f = await _step(tx, f, event)
    for _ in range(2):
        f = await _step(tx, f, E.QA_FAILED)
        f = await _step(tx, f, E.IMPLEMENTATION_DONE)
    f = await _step(tx, f, E.QA_FAILED)
    assert (f.phase, f.retry_count) == (Phase.FAILED, 2)
