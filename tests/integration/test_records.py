"""Artefactos, aprobaciones, logs, perfiles, Context Packs y checkpointer de LangGraph."""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text

from specforge.core.enums import (
    AgentRole,
    ApprovalDecision,
    ApprovalGate,
    ArtifactType,
    ChunkSourceKind,
    ExecutionOutcome,
    ModelTier,
    Phase,
)
from specforge.core.errors import (
    IdempotencyConflictError,
    ImmutableRecordError,
    IntegrityViolationError,
)
from specforge.db.checkpointer import open_checkpointer, thread_config
from specforge.db.models import Feature
from specforge.db.repositories import ChunkInput
from tests.integration.conftest import one_hot


@pytest.fixture
async def feature(tx) -> Feature:
    return await tx(
        lambda uow: uow.features.create(
            module="CRM", tenant_id="acme", title="Registro de consultas", request="r"
        )
    )


def _add_story_set(uow, f: Feature, content: dict, key: str | None = None):
    return uow.artifacts.add(
        feature_id=f.feature_id,
        artifact_type=ArtifactType.USER_STORY_SET,
        content=content,
        produced_by=AgentRole.BUSINESS_EXPERT,
        schema_name="StoryRefinementOutput",
        phase=Phase.STORY_REFINEMENT,
        blueprint_version=f.blueprint_version,
        thread_id=f.current_thread_id,
        idempotency_key=key,
    )


async def test_artifacts_are_versioned_and_append_only(tx, feature: Feature) -> None:
    v1 = await tx(lambda uow: _add_story_set(uow, feature, {"stories": ["US-1"]}))
    v2 = await tx(lambda uow: _add_story_set(uow, feature, {"stories": ["US-1", "US-2"]}))
    assert (v1.version, v2.version) == (1, 2)
    assert v2.supersedes_artifact_id == v1.artifact_id

    latest = await tx(
        lambda uow: uow.artifacts.latest(feature.feature_id, ArtifactType.USER_STORY_SET)
    )
    assert latest is not None
    assert latest.artifact_id == v2.artifact_id
    resolved = await tx(lambda uow: uow.artifacts.get_many([v2.artifact_id, v1.artifact_id]))
    assert [a.version for a in resolved] == [2, 1]

    with pytest.raises(ImmutableRecordError):
        await tx(
            lambda uow: uow.session.execute(
                text("UPDATE feature_artifacts SET summary = 'x' WHERE artifact_id = :i"),
                {"i": v1.artifact_id},
            )
        )


async def test_artifact_idempotency(tx, feature: Feature) -> None:
    first = await tx(lambda uow: _add_story_set(uow, feature, {"s": 1}, key="k-1"))
    again = await tx(lambda uow: _add_story_set(uow, feature, {"s": 1}, key="k-1"))
    assert again.artifact_id == first.artifact_id
    with pytest.raises(IdempotencyConflictError):
        await tx(lambda uow: _add_story_set(uow, feature, {"s": 2}, key="k-1"))


async def test_blueprint_approval_rules(tx, feature: Feature) -> None:
    def approve(uow, decision, artifact_id=None, comments="", key=None):
        return uow.approvals.record(
            feature_id=feature.feature_id,
            gate=ApprovalGate.BLUEPRINT_APPROVAL,
            decision=decision,
            phase=Phase.HUMAN_GATE,
            blueprint_version=1,
            reviewer="tech-lead@acme",
            thread_id=feature.current_thread_id,
            artifact_id=artifact_id,
            comments=comments,
            idempotency_key=key,
        )

    with pytest.raises(IntegrityViolationError, match="feature_approvals_blueprint_decision"):
        await tx(lambda uow: approve(uow, ApprovalDecision.APPROVED))

    blueprint = await tx(
        lambda uow: uow.artifacts.add(
            feature_id=feature.feature_id,
            artifact_type=ArtifactType.ARCHITECTURE_BLUEPRINT,
            content={"components": []},
            produced_by=AgentRole.ARCHITECT_TECH_LEAD,
            schema_name="ArchitectureBlueprint",
            phase=Phase.BLUEPRINT_DESIGN,
            blueprint_version=1,
            thread_id=feature.current_thread_id,
        )
    )
    with pytest.raises(IntegrityViolationError, match="changes_need_detail"):
        await tx(
            lambda uow: approve(uow, ApprovalDecision.CHANGES_REQUESTED, blueprint.artifact_id)
        )

    ok = await tx(
        lambda uow: approve(
            uow, ApprovalDecision.CHANGES_REQUESTED, blueprint.artifact_id, "agregar índice", "a-1"
        )
    )
    same = await tx(
        lambda uow: approve(
            uow, ApprovalDecision.CHANGES_REQUESTED, blueprint.artifact_id, "agregar índice", "a-1"
        )
    )
    assert same.approval_id == ok.approval_id
    latest = await tx(
        lambda uow: uow.approvals.latest_for_gate(
            feature.feature_id, ApprovalGate.BLUEPRINT_APPROVAL
        )
    )
    assert latest is not None
    assert latest.approval_id == ok.approval_id


async def test_execution_logs_track_tokens(tx, feature: Feature) -> None:
    async def run(uow):
        log = await uow.logs.start(
            feature_id=feature.feature_id,
            tenant_id=feature.tenant_id,
            thread_id=feature.current_thread_id,
            node_name="po_intake",
            agent_role=AgentRole.PO,
            phase=Phase.INTAKE_PO,
            model_provider="anthropic",
            model_name="claude-haiku-5-5",
            model_tier=ModelTier.FAST,
            context_pack_refs=["CORE@1", "CRM@1"],
        )
        return await uow.logs.finish(
            log.log_id,
            outcome=ExecutionOutcome.SUCCESS,
            input_tokens=1200,
            output_tokens=300,
            cached_tokens=900,
            latency_ms=850,
            metadata={"stop_reason": "end_turn"},
        )

    log = await tx(run)
    assert log.finished_at is not None
    assert log.metadata_ == {"stop_reason": "end_turn"}
    with pytest.raises(ImmutableRecordError):
        await tx(lambda uow: uow.logs.finish(log.log_id, outcome=ExecutionOutcome.RETRY))

    usage = await tx(lambda uow: uow.logs.usage_by_tier(feature.feature_id))
    assert [(u.model_tier, u.calls, u.input_tokens, u.cached_tokens) for u in usage] == [
        ("FAST", 1, 1200, 900)
    ]


async def test_business_profile_config_version(tx) -> None:
    def upsert(uow, config):
        return uow.profiles.upsert(
            tenant_id="beta",
            code="clinica",
            display_name="Clínica",
            industry="healthcare",
            config=config,
        )

    first = await tx(lambda uow: upsert(uow, {"reminders": [{"trigger": "appointment"}]}))
    unchanged = await tx(lambda uow: upsert(uow, {"reminders": [{"trigger": "appointment"}]}))
    changed = await tx(lambda uow: upsert(uow, {"reminders": [{"trigger": "renewal"}]}))
    assert (first.config_version, unchanged.config_version, changed.config_version) == (1, 1, 2)


async def test_context_packs_and_scoped_vector_search(tx, feature: Feature) -> None:
    def publish(uow, content, industry=None, code="CRM"):
        return uow.context.publish_pack(
            pack_code=code, module="CRM", title="CRM", content=content, industry=industry
        )

    v1 = await tx(lambda uow: publish(uow, "reglas v1"))
    same = await tx(lambda uow: publish(uow, "reglas v1"))
    v2 = await tx(lambda uow: publish(uow, "reglas v2"))
    overlay = await tx(
        lambda uow: publish(uow, "overlay taller", "automotive_service", "CRM.automotive")
    )
    assert (v1.version, same.version, v2.version) == (1, 1, 2)

    def chunk(idx, pack, **kw):
        return ChunkInput(
            source_kind=ChunkSourceKind.CONTEXT_PACK,
            module="CRM",
            chunk_index=0,
            content=f"chunk {idx}",
            pack_code=pack.pack_code,
            pack_version=pack.version,
            embedding=one_hot(idx),
            embedding_model="test-embed",
            **kw,
        )

    await tx(
        lambda uow: uow.context.add_chunks(
            [
                chunk(0, v1),
                chunk(1, v2),
                chunk(2, overlay, industry="automotive_service"),
                ChunkInput(
                    source_kind=ChunkSourceKind.EPISODIC,
                    module="CRM",
                    chunk_index=0,
                    content="decisión de otro tenant",
                    feature_id=feature.feature_id,
                    tenant_id="other",
                    embedding=one_hot(3),
                    embedding_model="test-embed",
                ),
            ]
        )
    )

    agnostic = await tx(lambda uow: uow.context.search(one_hot(1), module="CRM", tenant_id="acme"))
    assert [h.chunk.content for h in agnostic] == ["chunk 1"]  # v1 inactivo, sin overlay

    with_giro = await tx(
        lambda uow: uow.context.search(
            one_hot(2), module="CRM", tenant_id="acme", industry="automotive_service"
        )
    )
    assert next(h.chunk.content for h in with_giro) == "chunk 2"
    assert "decisión de otro tenant" not in {h.chunk.content for h in with_giro}

    packs = await tx(lambda uow: uow.context.active_packs_for("CRM", industry="automotive_service"))
    assert [(p.pack_code, p.version) for p in packs] == [("CRM", 2), ("CRM.automotive", 1)]


class _GateState(TypedDict):
    decision: str


async def test_checkpointer_supports_interrupt_and_resume(settings) -> None:
    def human_gate(state: _GateState) -> _GateState:
        return {"decision": interrupt({"gate": "BLUEPRINT_APPROVAL"})}

    builder = StateGraph(_GateState)
    builder.add_node("human_gate", human_gate)
    builder.add_edge(START, "human_gate")
    builder.add_edge("human_gate", END)

    async with open_checkpointer(settings, setup=True) as saver:
        graph = builder.compile(checkpointer=saver)
        config = thread_config("sf:CRM-999:v1:01")
        paused = await graph.ainvoke({"decision": ""}, config)
        assert paused["__interrupt__"][0].value == {"gate": "BLUEPRINT_APPROVAL"}
        resumed = await graph.ainvoke(Command(resume="APPROVED"), config)
        assert resumed["decision"] == "APPROVED"

    # Un pool nuevo ve el mismo estado persistido.
    async with open_checkpointer(settings) as saver:
        snapshot = await builder.compile(checkpointer=saver).aget_state(config)
        assert snapshot.values["decision"] == "APPROVED"
