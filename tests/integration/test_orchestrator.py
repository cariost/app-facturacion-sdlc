"""SpecForgeOrchestrator + LangGraph + AsyncPostgresSaver con agentes reales y LLM guionado."""

from __future__ import annotations

import pytest

from specforge.core.enums import ApprovalDecision, ApprovalGate, ArtifactType, Phase
from specforge.core.errors import GateNotPendingError, InvalidPhaseTransitionError
from specforge.graph import ClarificationResume, GateResume, RunStatus
from specforge.schemas import StoryRefinementOutput
from specforge.schemas.stories import StoryChange, StoryChangeType, StoryStatus
from tests.factories import (
    blueprint,
    dev_output,
    intake_brief,
    qa_report,
    refinement_v1,
    reminder_story,
)
from tests.fakes import Scripted, reply

REQUEST = (
    "Recordar al cliente su servicio periódico por tiempo o por contador de uso, y atender "
    "consultas y cotizaciones por un canal automatizado con derivación a un asesor."
)


async def _create(orchestrator, **kw) -> str:
    feature = await orchestrator.create_feature(
        module="CRM",
        tenant_id="acme",
        title="Recordatorios de servicio y consultas",
        request=REQUEST,
        business_profile="taller-automotriz",
        **kw,
    )
    return feature.feature_id


def _script_until_gate(llm, *, brief=None) -> None:
    llm.add("intake_po", reply(brief or intake_brief()))
    llm.add("story_refinement", reply(refinement_v1()))
    llm.add("blueprint_design", reply(blueprint(1)))


async def test_pauses_at_human_gate_and_completes_after_approval(orchestrator, llm, tx) -> None:
    _script_until_gate(llm)
    fid = await _create(orchestrator)

    paused = await orchestrator.run(fid)
    assert (paused.status, paused.phase) == (RunStatus.PAUSED, Phase.HUMAN_GATE)
    gate = paused.interrupt
    assert gate is not None
    assert gate.gate is ApprovalGate.BLUEPRINT_APPROVAL
    assert gate.thread_id == f"sf:{fid}:v1:01"
    assert gate.blueprint_version == 1
    assert ApprovalDecision.CHANGES_REQUESTED in gate.allowed_decisions

    artifacts = await tx(lambda uow: uow.artifacts.list_for_feature(fid))
    assert [a.artifact_type for a in artifacts] == [
        "INTAKE_BRIEF",
        "STORY_REFINEMENT",
        "ARCHITECTURE_BLUEPRINT",
    ]
    assert all(a.execution_log_id is not None for a in artifacts)
    assert gate.artifact_id == str(artifacts[-1].artifact_id)

    with pytest.raises(GateNotPendingError):
        await orchestrator.answer_clarification(
            fid, ClarificationResume(reviewer="ana", answers={"Q-01": "x"})
        )

    llm.add("implementation", reply(dev_output()))
    llm.add("qa_verification", reply(qa_report()))
    done = await orchestrator.decide_gate(
        fid, GateResume(decision=ApprovalDecision.APPROVED, reviewer="ana")
    )
    assert (done.status, done.phase) == (RunStatus.COMPLETED, Phase.COMPLETED)

    [approval] = await tx(lambda uow: uow.approvals.list_for_feature(fid))
    assert (approval.decision, approval.interrupt_id) == (
        "APPROVED",
        f"sf:{fid}:v1:01:BLUEPRINT_APPROVAL",
    )
    feature = await tx(lambda uow: uow.features.get(fid))
    lines = feature.executive_summary.splitlines()
    assert lines[0].startswith(f"■ {fid} · COMPLETED")
    assert "Gate blueprint v1: APPROVED (ana)" in lines
    assert lines[-1] == "QA: PASS en intento 1"

    calls = len(llm.calls)
    again = await orchestrator.run(fid)
    assert again.status is RunStatus.COMPLETED
    assert len(llm.calls) == calls  # una feature cerrada no vuelve a invocar modelos


async def test_intake_clarification_is_a_pause_and_answers_reach_the_po(orchestrator, llm, tx):
    asking = intake_brief(with_blocking_question=True)
    answered = asking.model_copy(
        update={"open_questions": [asking.open_questions[0].model_copy(update={"answer": "km"})]}
    )
    _script_until_gate(llm, brief=asking)
    llm.add("intake_po", reply(answered))
    fid = await _create(orchestrator)

    paused = await orchestrator.run(fid)
    assert (paused.status, paused.phase) == (RunStatus.PAUSED, Phase.INTAKE_PO)
    assert paused.interrupt is not None
    assert paused.interrupt.gate is ApprovalGate.INTAKE_CLARIFICATION
    assert [q.id for q in paused.interrupt.questions] == ["Q-01"]

    with pytest.raises(GateNotPendingError):
        await orchestrator.decide_gate(
            fid, GateResume(decision=ApprovalDecision.APPROVED, reviewer="ana")
        )

    resumed = await orchestrator.answer_clarification(
        fid, ClarificationResume(reviewer="ana", answers={"Q-01": "odometro_km"})
    )
    assert (resumed.status, resumed.phase) == (RunStatus.PAUSED, Phase.HUMAN_GATE)

    [clarified] = await tx(lambda uow: uow.approvals.list_for_feature(fid))
    assert (clarified.gate, clarified.decision) == ("INTAKE_CLARIFICATION", "CLARIFIED")
    assert clarified.payload == {"answers": {"Q-01": "odometro_km"}}
    second_po_call = llm.requests_for("intake_po")[1]
    assert any("odometro_km" in block.text for block in second_po_call.user)


async def test_llm_failure_blocks_and_resume_opens_a_new_run(orchestrator, llm, tx) -> None:
    llm.add("intake_po", Scripted(error=TimeoutError("proveedor caído")))
    fid = await _create(orchestrator)

    blocked = await orchestrator.run(fid)
    assert blocked.status is RunStatus.BLOCKED
    feature = await tx(lambda uow: uow.features.get(fid))
    assert feature.blocked_from_phase == "INTAKE_PO"
    assert "intake_po" in (feature.blocked_reason or "")

    _script_until_gate(llm)
    resumed = await orchestrator.resume(fid, reviewer="ops", reason="proveedor restablecido")
    assert (resumed.status, resumed.thread_id) == (RunStatus.PAUSED, f"sf:{fid}:v1:02")
    history = await tx(lambda uow: uow.features.phase_history(fid))
    assert {h.thread_id for h in history} == {f"sf:{fid}:v1:01", f"sf:{fid}:v1:02"}

    logs = await tx(lambda uow: uow.logs.list_for_feature(fid))
    assert logs[0].outcome == "LLM_ERROR"


async def test_rejection_closes_the_feature(orchestrator, llm, tx) -> None:
    _script_until_gate(llm)
    fid = await _create(orchestrator)
    await orchestrator.run(fid)

    result = await orchestrator.decide_gate(
        fid,
        GateResume(decision=ApprovalDecision.REJECTED, reviewer="ana", comments="fuera de alcance"),
    )
    assert (result.status, result.phase) == (RunStatus.FAILED, Phase.FAILED)
    with pytest.raises(InvalidPhaseTransitionError):
        await orchestrator.prepare_resume(fid, reviewer="ops", reason="reintentar")


async def test_refinement_rounds_are_capped(orchestrator, llm, tx) -> None:
    not_ready = StoryRefinementOutput(
        stories=[reminder_story(status=StoryStatus.REFINED, revision=2)],
        changes=[
            StoryChange(story_id="US-001", change_type=StoryChangeType.MODIFIED, rationale="x")
        ],
    )
    unchanged = StoryRefinementOutput(stories=not_ready.stories)
    llm.add("intake_po", reply(intake_brief()))
    llm.add("story_refinement", reply(not_ready), reply(unchanged), reply(unchanged))
    fid = await _create(orchestrator)

    result = await orchestrator.run(fid)
    assert result.status is RunStatus.BLOCKED
    refinements = await tx(
        lambda uow: uow.artifacts.list_for_feature(fid, artifact_type=ArtifactType.STORY_REFINEMENT)
    )
    assert len(refinements) == 3
    feature = await tx(lambda uow: uow.features.get(fid))
    assert "rondas de refinamiento" in (feature.blocked_reason or "")
