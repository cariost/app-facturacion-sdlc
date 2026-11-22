"""ContextBuilder contra PostgreSQL + pgvector con los Context Packs reales sembrados."""

from __future__ import annotations

import pytest

from specforge.context import NODE_SPECS, ContextBuilder, HashingEmbedder
from specforge.context.specs import NodeContextSpec
from specforge.context_packs import load_seed_packs, seed_context_packs
from specforge.core.enums import AgentRole, ApprovalDecision, ApprovalGate, ArtifactType, Phase
from specforge.core.errors import ContextAssemblyError, ContextBudgetExceededError
from specforge.core.transition_guard import TransitionEvent, TransitionGuard
from specforge.db.models import Feature
from specforge.schemas import FeatureDeliveryState, serialize
from specforge.schemas.delivery import DevOutput, FileChange, FileChangeType
from tests.factories import blueprint, intake_brief, refinement_v1

guard = TransitionGuard()
embedder = HashingEmbedder()
REMINDER_REQUEST = (
    "Avisar al cliente cuando le toque su servicio periódico, por tiempo transcurrido o por "
    "contador de uso que supera un umbral, respetando el consentimiento de contacto."
)


@pytest.fixture
async def seeded(tx) -> None:
    await tx(lambda uow: seed_context_packs(uow, embedder=embedder))


@pytest.fixture
async def feature(tx, seeded) -> Feature:
    async def create(uow):
        await uow.profiles.upsert(
            tenant_id="acme",
            code="taller-automotriz",
            display_name="Taller Acme",
            industry="automotriz",
            config={"reminders": {"time": {"interval": "P6M"}}},
        )
        return await uow.features.create(
            module="CRM",
            tenant_id="acme",
            title="Recordatorios de servicio",
            request=REMINDER_REQUEST,
            business_profile="taller-automotriz",
        )

    return await tx(create)


async def _advance(tx, feature: Feature, *events: TransitionEvent) -> Feature:
    for event in events:
        decision = guard.decide(feature, event, reason="test")
        feature = await tx(
            lambda uow, f=feature, d=decision: uow.features.apply(
                f.feature_id, d, expected_row_version=f.row_version
            )
        )
    return feature


async def _store(tx, feature: Feature, contract, role: AgentRole):
    s = serialize(contract)
    return await tx(
        lambda uow: uow.artifacts.add(
            feature_id=feature.feature_id,
            artifact_type=s.artifact_type,
            content=s.content,
            produced_by=role,
            schema_name=s.schema_name,
            schema_version=s.schema_version,
            phase=Phase(feature.phase),
            blueprint_version=feature.blueprint_version,
            thread_id=feature.current_thread_id,
        )
    )


async def test_seeding_is_idempotent(tx, seeded) -> None:
    again = await tx(lambda uow: seed_context_packs(uow, embedder=embedder))
    assert len(again) == len(load_seed_packs())
    assert not any(r.changed for r in again)
    assert {r.version for r in again} == {1}


async def test_intake_context_is_minimal_and_cache_friendly(tx, feature: Feature) -> None:
    state = FeatureDeliveryState.from_feature(feature)
    bundle = await tx(lambda uow: ContextBuilder(embedder=embedder).build(uow, state, "intake_po"))

    keys = bundle.segment_keys()
    assert keys[:2] == ["pack:CORE@1", "pack:CRM@1"]
    assert "pack:CRM.examples.automotive@1" in keys  # overlay del giro del perfil
    assert any(k.startswith("pack:CRM.reminders@1#") for k in keys)  # elegido por relevancia
    assert not any("SUNAT" in k or "SIRE" in k for k in keys)
    assert keys[-2:] == ["request", "state"]
    assert "profile:acme/taller-automotriz@1" in keys
    assert bundle.input_artifact_ids == ()
    assert bundle.total_tokens <= NODE_SPECS["intake_po"].budget_tokens
    assert "CRM.reminders@1" in bundle.pack_refs

    # El prefijo estable no depende de la feature: otra feature del mismo perfil lo comparte.
    stable = bundle.stable_text()
    assert REMINDER_REQUEST not in stable
    assert "Recordatorios de servicio" in bundle.volatile_text()


async def test_agnostic_profile_gets_no_industry_overlay(tx, seeded) -> None:
    feature = await tx(
        lambda uow: uow.features.create(
            module="CRM", tenant_id="beta", title="Consultas", request="cotizaciones por chat"
        )
    )
    state = FeatureDeliveryState.from_feature(feature)
    bundle = await tx(lambda uow: ContextBuilder(embedder=embedder).build(uow, state, "intake_po"))
    assert not any("automotive" in k for k in bundle.segment_keys())


async def test_refinement_requires_brief_and_includes_human_feedback(tx, feature) -> None:
    builder = ContextBuilder(embedder=embedder)
    f = await _advance(tx, feature, TransitionEvent.INTAKE_COMPLETED)
    state = FeatureDeliveryState.from_feature(f)
    with pytest.raises(ContextAssemblyError, match="INTAKE_BRIEF"):
        await tx(lambda uow: builder.build(uow, state, "story_refinement"))

    brief = await _store(tx, feature, intake_brief(), AgentRole.PO)
    refinement = await _store(tx, f, refinement_v1(), AgentRole.BUSINESS_EXPERT)
    f = await _advance(tx, f, TransitionEvent.STORIES_READY, TransitionEvent.BLUEPRINT_READY)
    bp = await _store(tx, f, blueprint(1), AgentRole.ARCHITECT_TECH_LEAD)
    await tx(
        lambda uow: uow.approvals.record(
            feature_id=f.feature_id,
            gate=ApprovalGate.BLUEPRINT_APPROVAL,
            decision=ApprovalDecision.CHANGES_REQUESTED,
            phase=Phase.HUMAN_GATE,
            blueprint_version=1,
            reviewer="ana",
            thread_id=f.current_thread_id,
            artifact_id=bp.artifact_id,
            comments="Agregar política por ciclo de calendario",
        )
    )
    f = await _advance(tx, f, TransitionEvent.STORY_CHANGES_REQUESTED)
    state = FeatureDeliveryState.from_feature(f).model_copy(
        update={"artifact_ids": {ArtifactType.INTAKE_BRIEF: brief.artifact_id}}
    )
    bundle = await tx(lambda uow: builder.build(uow, state, "story_refinement"))

    assert bundle.input_artifact_ids == (brief.artifact_id, refinement.artifact_id)
    feedback = next(s for s in bundle.segments if s.key.startswith("feedback:"))
    assert "ciclo de calendario" in feedback.content
    assert feedback.required
    assert bundle.segment("state").content.startswith(f"feature: {f.feature_id}")
    assert "blueprint v2" in bundle.segment("state").content


async def test_dev_retry_sees_previous_output_and_qa_errors(tx, feature) -> None:
    builder = ContextBuilder(embedder=embedder)
    await _store(tx, feature, intake_brief(), AgentRole.PO)
    f = await _advance(tx, feature, TransitionEvent.INTAKE_COMPLETED)
    await _store(tx, f, refinement_v1(), AgentRole.BUSINESS_EXPERT)
    f = await _advance(tx, f, TransitionEvent.STORIES_READY)
    await _store(tx, f, blueprint(1), AgentRole.ARCHITECT_TECH_LEAD)
    f = await _advance(tx, f, TransitionEvent.BLUEPRINT_READY, TransitionEvent.BLUEPRINT_APPROVED)

    first = await tx(
        lambda uow: builder.build(uow, FeatureDeliveryState.from_feature(f), "implementation")
    )
    assert [k.split("@")[0] for k in first.segment_keys() if k.startswith("artifact:")] == [
        "artifact:ARCHITECTURE_BLUEPRINT",
        "artifact:STORY_REFINEMENT",
    ]

    dev = DevOutput(
        blueprint_version=1,
        attempt=1,
        summary="motor de políticas",
        files=[
            FileChange(path="src/crm/reminders.py", change_type=FileChangeType.ADDED, summary="m")
        ],
        implemented=["US-001/AC-01"],
    )
    dev_artifact = await _store(tx, f, dev, AgentRole.DEV)
    f = await _advance(tx, f, TransitionEvent.IMPLEMENTATION_DONE, TransitionEvent.QA_FAILED)
    state = FeatureDeliveryState.from_feature(f).model_copy(
        update={"qa_errors": ["QA-01 [CRITICAL] US-001/AC-02: no dispara en el umbral exacto"]}
    )
    retry = await tx(lambda uow: builder.build(uow, state, "implementation"))
    assert dev_artifact.artifact_id in retry.input_artifact_ids
    assert "umbral exacto" in retry.segment("state").content
    assert "intento 2/4" in retry.segment("state").content


async def test_budget_is_enforced(tx, feature) -> None:
    state = FeatureDeliveryState.from_feature(feature)
    base = NODE_SPECS["intake_po"]
    tight = {"intake_po": NodeContextSpec(**{**_fields(base), "budget_tokens": 120})}
    with pytest.raises(ContextBudgetExceededError):
        await tx(lambda uow: ContextBuilder(specs=tight).build(uow, state, "intake_po"))

    small = {"intake_po": NodeContextSpec(**{**_fields(base), "budget_tokens": 1500})}
    bundle = await tx(lambda uow: ContextBuilder(specs=small).build(uow, state, "intake_po"))
    assert bundle.dropped  # sin embedder, los packs compiten por presupuesto
    assert bundle.total_tokens <= 1500
    assert {"request", "state"} <= set(bundle.segment_keys())


async def test_phase_mismatch_is_rejected(tx, feature) -> None:
    state = FeatureDeliveryState.from_feature(feature)
    with pytest.raises(ContextAssemblyError, match="BLUEPRINT_DESIGN"):
        await tx(lambda uow: ContextBuilder().build(uow, state, "blueprint_design"))


async def test_record_progress_compacts_and_persists(tx, feature) -> None:
    builder = ContextBuilder()
    state = FeatureDeliveryState.from_feature(feature)
    for i in range(20):
        state = await tx(
            lambda uow, s=state, i=i: builder.record_progress(
                uow, s, [f"evento {i}"], active_constraints=["sin hardcode de rubro"]
            )
        )
    stored = await tx(lambda uow: uow.features.get(feature.feature_id))
    assert stored.executive_summary == state.executive_summary
    assert len(stored.executive_summary.splitlines()) == 15
    assert stored.active_constraints == ["sin hardcode de rubro"]
    assert stored.row_version == state.row_version


def _fields(spec: NodeContextSpec) -> dict:
    return {name: getattr(spec, name) for name in spec.__dataclass_fields__}
