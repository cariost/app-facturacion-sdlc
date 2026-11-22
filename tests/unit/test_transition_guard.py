from dataclasses import dataclass

import pytest

from specforge.core.enums import ApprovalDecision, Phase
from specforge.core.errors import InvalidPhaseTransitionError
from specforge.core.phases import is_allowed
from specforge.core.transition_guard import (
    ChangeTarget,
    TransitionEvent,
    TransitionGuard,
    event_for_gate_decision,
)

E = TransitionEvent
guard = TransitionGuard()


@dataclass
class Snap:
    phase: str
    retry_count: int = 0
    max_retries: int = 3
    blocked_from_phase: str | None = None


@pytest.mark.parametrize(
    ("phase", "event", "expected"),
    [
        (Phase.INTAKE_PO, E.INTAKE_COMPLETED, Phase.STORY_REFINEMENT),
        (Phase.STORY_REFINEMENT, E.STORIES_READY, Phase.BLUEPRINT_DESIGN),
        (Phase.BLUEPRINT_DESIGN, E.BLUEPRINT_READY, Phase.HUMAN_GATE),
        (Phase.HUMAN_GATE, E.BLUEPRINT_APPROVED, Phase.IMPLEMENTATION),
        (Phase.HUMAN_GATE, E.BLUEPRINT_REJECTED, Phase.FAILED),
        (Phase.IMPLEMENTATION, E.IMPLEMENTATION_DONE, Phase.QA_VERIFICATION),
        (Phase.QA_VERIFICATION, E.QA_PASSED, Phase.COMPLETED),
    ],
)
def test_forward_events(phase: Phase, event: TransitionEvent, expected: Phase) -> None:
    decision = guard.decide(Snap(phase), event)
    assert decision.to_phase is expected
    assert not decision.count_retry
    assert not decision.new_blueprint_version


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (E.BLUEPRINT_CHANGES_REQUESTED, Phase.BLUEPRINT_DESIGN),
        (E.STORY_CHANGES_REQUESTED, Phase.STORY_REFINEMENT),
    ],
)
def test_changes_requested_bump_blueprint(event: TransitionEvent, expected: Phase) -> None:
    decision = guard.decide(Snap(Phase.HUMAN_GATE), event)
    assert decision.to_phase is expected
    assert decision.new_blueprint_version


def test_qa_failure_retries_until_budget_is_exhausted() -> None:
    for attempt in range(3):
        decision = guard.decide(Snap(Phase.QA_VERIFICATION, retry_count=attempt), E.QA_FAILED)
        assert decision.to_phase is Phase.IMPLEMENTATION
        assert decision.count_retry
        assert f"{attempt + 1}/3" in decision.reason
    exhausted = guard.decide(Snap(Phase.QA_VERIFICATION, retry_count=3), E.QA_FAILED)
    assert exhausted.to_phase is Phase.FAILED
    assert not exhausted.count_retry


def test_zero_retry_budget_fails_immediately() -> None:
    decision = guard.decide(Snap(Phase.QA_VERIFICATION, max_retries=0), E.QA_FAILED)
    assert decision.to_phase is Phase.FAILED


def test_block_and_resume() -> None:
    with pytest.raises(ValueError, match="motivo"):
        guard.decide(Snap(Phase.STORY_REFINEMENT), E.BLOCK)
    blocked = guard.decide(Snap(Phase.STORY_REFINEMENT), E.BLOCK, reason="falta contador")
    assert blocked.to_phase is Phase.BLOCKED
    assert blocked.blocked_reason == "falta contador"

    resumed = guard.decide(Snap(Phase.BLOCKED, blocked_from_phase="STORY_REFINEMENT"), E.RESUME)
    assert resumed.to_phase is Phase.STORY_REFINEMENT
    with pytest.raises(InvalidPhaseTransitionError):
        guard.decide(Snap(Phase.BLOCKED), E.RESUME)


@pytest.mark.parametrize(
    ("phase", "event"),
    [
        (Phase.INTAKE_PO, E.BLUEPRINT_APPROVED),
        (Phase.BLUEPRINT_DESIGN, E.BLUEPRINT_APPROVED),
        (Phase.IMPLEMENTATION, E.QA_PASSED),
        (Phase.QA_VERIFICATION, E.BLUEPRINT_CHANGES_REQUESTED),
        (Phase.BLOCKED, E.BLOCK),
        (Phase.COMPLETED, E.FAIL),
        (Phase.FAILED, E.RESUME),
    ],
)
def test_rejects_events_out_of_phase(phase: Phase, event: TransitionEvent) -> None:
    with pytest.raises(InvalidPhaseTransitionError):
        guard.decide(Snap(phase, blocked_from_phase="IMPLEMENTATION"), event, reason="x")


def test_every_decision_is_an_allowed_edge() -> None:
    for phase in Phase:
        snap = Snap(phase, blocked_from_phase="IMPLEMENTATION" if phase is Phase.BLOCKED else None)
        for event in guard.allowed_events(snap):
            decision = guard.decide(snap, event, reason="probe")
            assert is_allowed(decision.from_phase, decision.to_phase)


def test_allowed_events_at_human_gate() -> None:
    assert guard.allowed_events(Snap(Phase.HUMAN_GATE)) == {
        E.BLUEPRINT_APPROVED,
        E.BLUEPRINT_REJECTED,
        E.BLUEPRINT_CHANGES_REQUESTED,
        E.STORY_CHANGES_REQUESTED,
        E.BLOCK,
        E.FAIL,
    }


def test_gate_decision_mapping() -> None:
    assert event_for_gate_decision(ApprovalDecision.APPROVED) is E.BLUEPRINT_APPROVED
    assert event_for_gate_decision(ApprovalDecision.REJECTED) is E.BLUEPRINT_REJECTED
    assert (
        event_for_gate_decision(ApprovalDecision.CHANGES_REQUESTED) is E.BLUEPRINT_CHANGES_REQUESTED
    )
    assert (
        event_for_gate_decision(ApprovalDecision.CHANGES_REQUESTED, ChangeTarget.STORIES)
        is E.STORY_CHANGES_REQUESTED
    )
    with pytest.raises(ValueError, match="CLARIFIED"):
        event_for_gate_decision(ApprovalDecision.CLARIFIED)
