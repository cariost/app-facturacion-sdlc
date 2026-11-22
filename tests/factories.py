"""Artefactos de ejemplo del caso CRM parametrizable (recordatorio por tiempo/uso + consultas).

Ningún valor de rubro está codificado en las reglas: todo intervalo o contador es un
`ConfigurableParameter` que se resuelve desde `business_profiles.config`.
"""

from __future__ import annotations

from typing import Any

from specforge.schemas import (
    AcceptanceCriterion,
    ArchitectureBlueprint,
    ConfigurableParameter,
    DeterministicRule,
    IntakeBrief,
    SatisfactionCondition,
    StoryException,
    StoryRefinementOutput,
    UserStoryWithConditions,
)
from specforge.schemas.blueprint import Component, ComponentKind, Plane, PlannedTest, TestKind
from specforge.schemas.stories import (
    ConditionKind,
    OpenQuestion,
    ParameterType,
    QuestionSeverity,
    StoryChange,
    StoryChangeType,
    StoryStatus,
)


def reminder_story(**overrides: Any) -> UserStoryWithConditions:
    data: dict[str, Any] = {
        "story_id": "US-001",
        "title": "Aviso de servicio periódico",
        "as_a": "responsable de postventa",
        "i_want": "que el sistema avise al cliente cuando le toque su servicio periódico",
        "so_that": "aumente la recurrencia sin seguimiento manual",
        "status": StoryStatus.READY,
        "revision": 1,
        "acceptance_criteria": [
            AcceptanceCriterion(
                id="AC-01",
                given="un activo con último servicio y política por tiempo",
                when="transcurre el intervalo configurado",
                then="se genera un recordatorio por el canal configurado",
            ),
            AcceptanceCriterion(
                id="AC-02",
                given="un activo con contador de uso",
                when="el contador supera el umbral configurado desde el último servicio",
                then="se genera un recordatorio",
            ),
        ],
        "satisfaction_conditions": [
            SatisfactionCondition(
                id="CS-01", statement="Un solo recordatorio por activo y ciclo (idempotente)"
            ),
            SatisfactionCondition(
                id="CS-02",
                kind=ConditionKind.NON_FUNCTIONAL,
                statement="Las políticas se configuran por tenant sin desplegar código",
            ),
        ],
        "deterministic_rules": [
            DeterministicRule(
                id="RD-01",
                statement="Vence cuando ahora >= último_servicio + intervalo",
                parameter_keys=["reminders.time.interval"],
            ),
            DeterministicRule(
                id="RD-02",
                statement="Vence cuando contador - contador_último_servicio >= umbral",
                parameter_keys=["reminders.usage.counter", "reminders.usage.threshold"],
            ),
        ],
        "exceptions": [
            StoryException(
                id="EX-01",
                condition="el cliente revocó el consentimiento de contacto",
                expected_behavior="no se envía y se registra la omisión",
                rule_ids=["RD-01", "RD-02"],
            )
        ],
        "configurable_parameters": [
            ConfigurableParameter(
                key="reminders.time.interval",
                value_type=ParameterType.DURATION,
                description="Intervalo entre servicios (p. ej. 6 meses en un taller)",
            ),
            ConfigurableParameter(
                key="reminders.usage.counter",
                value_type=ParameterType.STRING,
                description="Nombre del contador de uso (km, horas, copias…)",
            ),
            ConfigurableParameter(
                key="reminders.usage.threshold",
                value_type=ParameterType.INTEGER,
                description="Umbral de uso entre servicios",
            ),
        ],
    }
    data.update(overrides)
    return UserStoryWithConditions(**data)


def inquiry_story(**overrides: Any) -> UserStoryWithConditions:
    data: dict[str, Any] = {
        "story_id": "US-002",
        "title": "Registro de consulta o cotización",
        "as_a": "cliente",
        "i_want": "hacer una consulta o pedir una cotización por un canal automatizado",
        "so_that": "reciba respuesta sin esperar a un asesor",
        "status": StoryStatus.READY,
        "acceptance_criteria": [
            AcceptanceCriterion(
                id="AC-01",
                given="un mensaje entrante de un cliente identificado",
                when="se clasifica como consulta o cotización",
                then="se registra vinculado al cliente y activo",
            )
        ],
        "satisfaction_conditions": [
            SatisfactionCondition(id="CS-01", statement="Escala a un humano tras N turnos")
        ],
        "deterministic_rules": [
            DeterministicRule(
                id="RD-01",
                statement="Escalar cuando turnos >= límite configurado",
                parameter_keys=["inquiries.handoff_after_turns"],
            )
        ],
        "configurable_parameters": [
            ConfigurableParameter(
                key="inquiries.handoff_after_turns",
                value_type=ParameterType.INTEGER,
                description="Turnos automáticos antes de derivar a un asesor",
                default=3,
            )
        ],
    }
    data.update(overrides)
    return UserStoryWithConditions(**data)


def intake_brief(*, with_blocking_question: bool = False) -> IntakeBrief:
    questions = (
        [
            OpenQuestion(
                id="Q-01",
                question="¿Qué contador de uso aplica para este perfil de negocio?",
                severity=QuestionSeverity.CRITICAL,
                story_ids=["US-001"],
            )
        ]
        if with_blocking_question
        else []
    )
    return IntakeBrief(
        title="Recordatorios de servicio y atención de consultas",
        problem_statement="Los clientes olvidan su servicio periódico y las consultas se pierden.",
        module="CRM",
        business_profile="taller-automotriz",
        scope_in=["recordatorios por tiempo o uso", "registro de consultas"],
        draft_stories=[reminder_story(status=StoryStatus.DRAFT)],
        open_questions=questions,
    )


def refinement_v1() -> StoryRefinementOutput:
    return StoryRefinementOutput(
        stories=[reminder_story(revision=2), inquiry_story()],
        changes=[
            StoryChange(
                story_id="US-001",
                change_type=StoryChangeType.MODIFIED,
                rationale="criterios y reglas completados",
            ),
            StoryChange(
                story_id="US-002",
                change_type=StoryChangeType.ADDED,
                rationale="atención automatizada de consultas",
            ),
        ],
    )


def blueprint(version: int = 1, **overrides: Any) -> ArchitectureBlueprint:
    data: dict[str, Any] = {
        "blueprint_version": version,
        "summary": "Motor de políticas de recordatorio y registro de consultas por tenant.",
        "components": [
            Component(
                name="crm.reminder_policy_engine",
                kind=ComponentKind.DOMAIN_SERVICE,
                plane=Plane.DETERMINISTIC,
                responsibility="Evalúa vencimientos según la política configurada",
                implements=["US-001/AC-01", "US-001/AC-02", "US-001/RD-01", "US-001/RD-02"],
            ),
            Component(
                name="crm.inquiry_intake",
                kind=ComponentKind.AGENT_NODE,
                plane=Plane.AGENTIC,
                responsibility="Clasifica mensajes entrantes",
                implements=["US-002/AC-01"],
            ),
            Component(
                name="crm.handoff_rule",
                kind=ComponentKind.DOMAIN_SERVICE,
                plane=Plane.DETERMINISTIC,
                responsibility="Decide la derivación a un asesor",
                implements=["US-002/RD-01"],
            ),
        ],
        "test_plan": [
            PlannedTest(
                id="T-001",
                kind=TestKind.UNIT,
                description="vence por tiempo",
                covers=["US-001/AC-01", "US-001/RD-01"],
            ),
            PlannedTest(
                id="T-002",
                kind=TestKind.UNIT,
                description="vence por uso",
                covers=["US-001/AC-02", "US-001/RD-02"],
            ),
            PlannedTest(
                id="T-003",
                kind=TestKind.INTEGRATION,
                description="registra consulta",
                covers=["US-002/AC-01"],
            ),
        ],
    }
    data.update(overrides)
    return ArchitectureBlueprint(**data)


def dev_output(attempt: int = 1, *, version: int = 1, addressed: list[str] | None = None):
    from specforge.schemas.delivery import DevOutput, FileChange, FileChangeType

    return DevOutput(
        blueprint_version=version,
        attempt=attempt,
        summary="motor de políticas y registro de consultas",
        files=[
            FileChange(
                path="src/crm/reminders/policy_engine.py",
                change_type=FileChangeType.ADDED if attempt == 1 else FileChangeType.MODIFIED,
                summary="evaluación de políticas TIME/USAGE",
            ),
            FileChange(
                path="src/crm/inquiries/handoff.py",
                change_type=FileChangeType.ADDED,
                summary="regla de derivación",
            ),
        ],
        implemented=["US-001/AC-01", "US-001/AC-02", "US-002/AC-01"],
        addressed_issues=addressed or [],
    )


def qa_report(attempt: int = 1, *, version: int = 1, passed: bool = True):
    from specforge.schemas.delivery import (
        IssueSeverity,
        QAIssue,
        QAReport,
        TestResult,
        TestStatus,
        Verdict,
    )

    failing = TestStatus.PASSED if passed else TestStatus.FAILED
    return QAReport(
        blueprint_version=version,
        attempt=attempt,
        verdict=Verdict.PASS if passed else Verdict.FAIL,
        results=[
            TestResult(test_id="T-001", status=TestStatus.PASSED),
            TestResult(
                test_id="T-002",
                status=failing,
                detail=None if passed else "no dispara al alcanzar exactamente el umbral",
            ),
            TestResult(test_id="T-003", status=TestStatus.PASSED),
        ],
        issues=[]
        if passed
        else [
            QAIssue(
                id="QA-01",
                severity=IssueSeverity.CRITICAL,
                ref="US-001/AC-02",
                description="comparación estricta en RD-02",
                suggested_fix="usar >= al comparar contra el umbral",
            )
        ],
    )
