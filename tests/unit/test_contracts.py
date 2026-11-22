import uuid

import pytest
from pydantic import ValidationError

from specforge.core.enums import ArtifactType, Phase
from specforge.core.errors import ContractViolationError, StoryProgressionError
from specforge.core.transition_guard import TransitionEvent, TransitionGuard
from specforge.schemas import (
    DevOutput,
    FeatureDeliveryState,
    QAReport,
    StoryRefinementOutput,
    deserialize,
    serialize,
    validate_progression,
)
from specforge.schemas import delivery as d
from specforge.schemas.blueprint import Component, ComponentKind, Plane
from specforge.schemas.stories import (
    DeterministicRule,
    StoryChange,
    StoryChangeType,
    StoryStatus,
)
from tests.factories import (
    blueprint,
    inquiry_story,
    intake_brief,
    refinement_v1,
    reminder_story,
)


class TestStories:
    def test_contracts_forbid_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            reminder_story(sector_hardcodeado="automotriz")

    def test_rules_must_reference_declared_parameters(self) -> None:
        rule = DeterministicRule(id="RD-09", statement="x", parameter_keys=["no.declarado"])
        with pytest.raises(ValidationError, match=r"no\.declarado"):
            reminder_story(deterministic_rules=[rule])

    def test_ready_requires_criteria_and_conditions(self) -> None:
        with pytest.raises(ValidationError, match="READY requiere"):
            reminder_story(satisfaction_conditions=[])
        assert reminder_story(status=StoryStatus.DRAFT, satisfaction_conditions=[])

    def test_duplicate_ids_rejected(self) -> None:
        story = reminder_story()
        with pytest.raises(ValidationError, match="duplicados"):
            reminder_story(
                acceptance_criteria=[*story.acceptance_criteria, story.acceptance_criteria[0]]
            )

    def test_intake_blocking_question_requires_clarification(self) -> None:
        assert not intake_brief().requires_clarification
        brief = intake_brief(with_blocking_question=True)
        assert brief.requires_clarification
        assert [q.id for q in brief.blocking_questions] == ["Q-01"]

    def test_refinement_readiness(self) -> None:
        assert refinement_v1().ready_for_blueprint
        draft = StoryRefinementOutput(stories=[reminder_story(status=StoryStatus.REFINED)])
        assert not draft.ready_for_blueprint

    def test_refinement_change_must_match_story_set(self) -> None:
        with pytest.raises(ValidationError, match="REMOVED"):
            StoryRefinementOutput(
                stories=[reminder_story()],
                changes=[
                    StoryChange(
                        story_id="US-001", change_type=StoryChangeType.REMOVED, rationale="x"
                    )
                ],
            )


class TestProgression:
    def test_valid_progression_from_intake(self) -> None:
        validate_progression(intake_brief().draft_stories, refinement_v1())

    def test_unchanged_refinement_is_valid(self) -> None:
        current = refinement_v1()
        validate_progression(current.stories, StoryRefinementOutput(stories=current.stories))

    def test_modified_story_must_bump_revision(self) -> None:
        before = refinement_v1()
        edited = reminder_story(title="Aviso de servicio periódico (multicanal)", revision=2)
        after = StoryRefinementOutput(
            stories=[edited, inquiry_story()],
            changes=[
                StoryChange(story_id="US-001", change_type=StoryChangeType.MODIFIED, rationale="x")
            ],
        )
        with pytest.raises(StoryProgressionError, match="revision debe ser 3"):
            validate_progression(before.stories, after)

        fixed = after.model_copy(
            update={"stories": [edited.model_copy(update={"revision": 3}), inquiry_story()]}
        )
        validate_progression(before.stories, fixed)

    def test_silent_removal_and_undeclared_addition(self) -> None:
        before = refinement_v1()
        after = StoryRefinementOutput(stories=[inquiry_story(story_id="US-003")])
        with pytest.raises(StoryProgressionError) as excinfo:
            validate_progression(before.stories, after)
        violations = excinfo.value.violations
        assert "US-001 desapareció sin declararse REMOVED" in violations
        assert "US-003 es nueva y no se declaró ADDED" in violations

    def test_ready_cannot_regress_to_draft(self) -> None:
        before = refinement_v1()
        after = StoryRefinementOutput(
            stories=[reminder_story(status=StoryStatus.DRAFT, revision=3), inquiry_story()],
            changes=[
                StoryChange(story_id="US-001", change_type=StoryChangeType.MODIFIED, rationale="x")
            ],
        )
        with pytest.raises(StoryProgressionError, match="READY a DRAFT"):
            validate_progression(before.stories, after)


class TestBlueprint:
    def test_full_coverage_passes(self) -> None:
        blueprint().check_against(refinement_v1())

    def test_detects_missing_coverage_and_unknown_refs(self) -> None:
        partial = blueprint(
            components=[
                Component(
                    name="crm.reminder_policy_engine",
                    kind=ComponentKind.DOMAIN_SERVICE,
                    plane=Plane.DETERMINISTIC,
                    responsibility="x",
                    implements=["US-001/AC-01", "US-009/AC-01"],
                )
            ]
        )
        with pytest.raises(ContractViolationError) as excinfo:
            partial.check_against(refinement_v1())
        violations = excinfo.value.violations
        assert "crm.reminder_policy_engine implementa US-009/AC-01 inexistente" in violations
        assert "US-002/AC-01 sin componente que lo implemente" in violations

    def test_deterministic_rules_cannot_live_in_agentic_plane(self) -> None:
        agentic = blueprint().components[0].model_copy(update={"plane": Plane.AGENTIC})
        bad = blueprint(components=[agentic, *blueprint().components[1:]])
        with pytest.raises(ContractViolationError, match="no puede implementar la regla"):
            bad.check_against(refinement_v1())


def _qa(
    verdict: d.Verdict, *, failed: bool, issues: list[d.QAIssue] | None = None, attempt: int = 1
) -> QAReport:
    results = [
        d.TestResult(test_id="T-001", status=d.TestStatus.PASSED),
        d.TestResult(
            test_id="T-002",
            status=d.TestStatus.FAILED if failed else d.TestStatus.PASSED,
            detail="umbral off-by-one" if failed else None,
        ),
        d.TestResult(test_id="T-003", status=d.TestStatus.PASSED),
    ]
    return QAReport(
        blueprint_version=1, attempt=attempt, verdict=verdict, results=results, issues=issues or []
    )


class TestDeliveryContracts:
    def test_dev_output_retry_must_address_issues(self) -> None:
        files = [
            d.FileChange(
                path="src/crm/reminders.py", change_type=d.FileChangeType.ADDED, summary="motor"
            )
        ]
        with pytest.raises(ValidationError, match="reintento"):
            DevOutput(
                blueprint_version=1,
                attempt=2,
                summary="fix",
                files=files,
                implemented=["US-001/AC-02"],
            )
        with pytest.raises(ValidationError, match="fuera del repositorio"):
            d.FileChange(
                path="src/../../etc/passwd", change_type=d.FileChangeType.MODIFIED, summary="x"
            )

    def test_qa_verdict_must_match_results(self) -> None:
        with pytest.raises(ValidationError, match="PASS incompatible"):
            _qa(d.Verdict.PASS, failed=True)
        with pytest.raises(ValidationError, match="FAIL requiere"):
            _qa(d.Verdict.FAIL, failed=False)

    def test_qa_report_drives_the_guard_and_compacts_errors(self) -> None:
        issue = d.QAIssue(
            id="QA-01",
            severity=d.IssueSeverity.CRITICAL,
            ref="US-001/AC-02",
            description="no dispara al alcanzar exactamente el umbral",
            suggested_fix="usar >= en RD-02",
        )
        report = _qa(d.Verdict.FAIL, failed=True, issues=[issue])
        report.check_against(blueprint())
        assert report.transition_event is TransitionEvent.QA_FAILED
        assert report.to_qa_errors() == [
            "QA-01 [CRITICAL] US-001/AC-02: no dispara al alcanzar exactamente el umbral"
            " → usar >= en RD-02",
            "T-002 FAILED: umbral off-by-one",
        ]
        assert _qa(d.Verdict.PASS, failed=False).transition_event is TransitionEvent.QA_PASSED

    def test_qa_report_must_cover_test_plan(self) -> None:
        report = QAReport(
            blueprint_version=1,
            attempt=1,
            verdict=d.Verdict.PASS,
            results=[d.TestResult(test_id="T-001", status=d.TestStatus.PASSED)],
        )
        with pytest.raises(ContractViolationError, match="T-002 planificado sin resultado"):
            report.check_against(blueprint())


def test_registry_roundtrip() -> None:
    original = refinement_v1()
    serialized = serialize(original)
    assert serialized.artifact_type is ArtifactType.STORY_REFINEMENT
    assert serialized.schema_name == "StoryRefinementOutput"
    restored = deserialize(serialized.artifact_type, serialized.schema_name, serialized.content)
    assert restored == original
    with pytest.raises(TypeError):
        deserialize(ArtifactType.QA_REPORT, "StoryRefinementOutput", serialized.content)


class TestDeliveryState:
    def _state(self, **kw: object) -> FeatureDeliveryState:
        return FeatureDeliveryState(feature_id="CRM-001", tenant_id="acme", module="CRM", **kw)

    def test_thread_id_and_pointers(self) -> None:
        state = self._state()
        assert state.thread_id == "sf:CRM-001:v1:01"
        artifact = uuid.uuid4()
        update = state.with_artifact(ArtifactType.QA_REPORT, artifact)
        assert state.model_copy(update=update).artifact_id(ArtifactType.QA_REPORT) == artifact

    def test_rejects_payloads_and_long_summaries(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            self._state(raw_history=["..."])
        with pytest.raises(ValidationError, match="15 líneas"):
            self._state(executive_summary="\n".join(["x"] * 16))
        with pytest.raises(ValidationError):
            self._state(qa_errors=["e"] * 11)
        with pytest.raises(ValidationError, match="prefijo"):
            FeatureDeliveryState(feature_id="CRM-001", tenant_id="acme", module="SUNAT")

    def test_after_transition_mirrors_db_effects(self) -> None:
        guard = TransitionGuard()
        state = self._state(phase=Phase.HUMAN_GATE, retry_count=0, qa_errors=["QA-01 x"])
        decision = guard.decide(state, TransitionEvent.BLUEPRINT_CHANGES_REQUESTED)
        revised = state.model_copy(update=state.after_transition(decision, row_version=7))
        assert (revised.phase, revised.blueprint_version, revised.qa_errors) == (
            Phase.BLUEPRINT_DESIGN,
            2,
            [],
        )
        assert revised.thread_id == "sf:CRM-001:v2:01"

        qa = self._state(phase=Phase.QA_VERIFICATION, retry_count=1)
        retry = guard.decide(qa, TransitionEvent.QA_FAILED)
        after = FeatureDeliveryState.model_validate(
            {**qa.model_dump(), **qa.after_transition(retry, row_version=9)}
        )
        assert (after.phase, after.retry_count) == (Phase.IMPLEMENTATION, 2)
