"""Decisiones de los agentes, ruteo del grafo y contratos HITL (sin BD ni LLM)."""

import pytest
from langgraph.graph import END
from pydantic import ValidationError

from specforge.agents import AgentDeps, AgentSuite
from specforge.core.enums import ApprovalDecision, ApprovalGate, Phase
from specforge.core.transition_guard import ChangeTarget, TransitionEvent
from specforge.graph import ClarificationResume, GateResume, GraphNodes
from specforge.graph.state import encode
from specforge.schemas import FeatureDeliveryState, PendingInterrupt, StoryRefinementOutput
from specforge.schemas.stories import OpenQuestion, QuestionSeverity, StoryStatus
from tests.factories import intake_brief, qa_report, refinement_v1, reminder_story

AGENTS = AgentSuite.build(AgentDeps(db=None, context=None, router=None))  # type: ignore[arg-type]
STATE = FeatureDeliveryState(feature_id="CRM-001", tenant_id="acme", module="CRM")


def test_suite_covers_every_agent_phase() -> None:
    phases = [a.phase for a in AGENTS.all()]
    assert phases == [
        Phase.INTAKE_PO,
        Phase.STORY_REFINEMENT,
        Phase.BLUEPRINT_DESIGN,
        Phase.IMPLEMENTATION,
        Phase.QA_VERIFICATION,
    ]
    with pytest.raises(KeyError):
        AGENTS.for_phase(Phase.HUMAN_GATE)


def test_po_pauses_for_critical_questions() -> None:
    blocked = AGENTS.product_owner.outcome(intake_brief(with_blocking_question=True), STATE)
    assert blocked.event is None
    assert blocked.clarification is not None
    assert blocked.clarification.gate is ApprovalGate.INTAKE_CLARIFICATION
    assert blocked.clarification.question_ids == ("Q-01",)
    ready = AGENTS.product_owner.outcome(intake_brief(), STATE)
    assert ready.event is TransitionEvent.INTAKE_COMPLETED


def test_business_expert_progresses_asks_or_iterates() -> None:
    agent = AGENTS.business_expert
    assert agent.outcome(refinement_v1(), STATE).event is TransitionEvent.STORIES_READY

    question = OpenQuestion(
        id="Q-02", question="¿Canal por defecto?", severity=QuestionSeverity.CRITICAL
    )
    asking = refinement_v1().model_copy(update={"open_questions": [question]})
    outcome = agent.outcome(asking, STATE)
    assert outcome.clarification is not None
    assert outcome.clarification.gate is ApprovalGate.STORY_CLARIFICATION

    draft = StoryRefinementOutput(stories=[reminder_story(status=StoryStatus.REFINED)])
    iterate = agent.outcome(draft, STATE)
    assert (iterate.event, iterate.clarification) == (None, None)
    assert "otra ronda" in iterate.summary


def test_qa_outcome_feeds_episodic_memory() -> None:
    failed = AGENTS.qa.outcome(qa_report(passed=False), STATE)
    assert failed.event is TransitionEvent.QA_FAILED
    assert failed.qa_errors is not None
    assert failed.qa_errors[0].startswith("QA-01 [CRITICAL] US-001/AC-02")
    passed = AGENTS.qa.outcome(qa_report(), STATE)
    assert (passed.event, passed.qa_errors) == (TransitionEvent.QA_PASSED, [])


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (Phase.INTAKE_PO, "intake_po"),
        (Phase.STORY_REFINEMENT, "story_refinement"),
        (Phase.BLUEPRINT_DESIGN, "blueprint_design"),
        (Phase.HUMAN_GATE, "human_gate"),
        (Phase.IMPLEMENTATION, "implementation"),
        (Phase.QA_VERIFICATION, "qa_verification"),
        (Phase.COMPLETED, END),
        (Phase.FAILED, END),
    ],
)
def test_route_follows_the_persisted_phase(phase: Phase, expected: str) -> None:
    state = STATE.model_copy(update={"phase": phase})
    assert GraphNodes.route({"run_thread": state.thread_id, "delivery": encode(state)}) == expected


def test_route_ends_on_thread_rollover_and_prioritizes_pauses() -> None:
    v2 = STATE.model_copy(update={"phase": Phase.BLUEPRINT_DESIGN, "blueprint_version": 2})
    assert GraphNodes.route({"run_thread": STATE.thread_id, "delivery": encode(v2)}) == END
    pending = STATE.model_copy(
        update={
            "pending_interrupt": PendingInterrupt(
                interrupt_id="x", gate=ApprovalGate.INTAKE_CLARIFICATION, reason="r"
            )
        }
    )
    routed = GraphNodes.route({"run_thread": STATE.thread_id, "delivery": encode(pending)})
    assert routed == "clarification"


def test_resume_contracts() -> None:
    changes = GateResume(
        decision=ApprovalDecision.CHANGES_REQUESTED,
        reviewer="ana",
        comments="agregar ciclo",
        target=ChangeTarget.STORIES,
    )
    assert changes.target is ChangeTarget.STORIES
    with pytest.raises(ValidationError, match="exige comentarios"):
        GateResume(decision=ApprovalDecision.CHANGES_REQUESTED, reviewer="ana")
    with pytest.raises(ValidationError, match="CLARIFIED"):
        GateResume(decision=ApprovalDecision.CLARIFIED, reviewer="ana")
    with pytest.raises(ValidationError, match="responde al menos"):
        ClarificationResume(reviewer="ana")
    assert ClarificationResume(reviewer="ana", reject=True).reject
