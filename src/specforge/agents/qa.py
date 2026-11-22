"""QA: verifica la entrega contra el plan de pruebas y produce la memoria episódica."""

from __future__ import annotations

from typing import TYPE_CHECKING

from specforge.agents.base import Agent, AgentOutcome, PreparedTask, load_latest, require
from specforge.core.enums import AgentRole, ArtifactType, Phase
from specforge.schemas.blueprint import ArchitectureBlueprint
from specforge.schemas.delivery import DevOutput, QAReport, TestStatus, Verdict

if TYPE_CHECKING:
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState


class QAAgent(Agent[QAReport]):
    role = AgentRole.QA
    node = "qa_verification"
    phase = Phase.QA_VERIFICATION
    output_model = QAReport

    async def prepare(self, uow: UnitOfWork, state: FeatureDeliveryState) -> PreparedTask[QAReport]:
        blueprint, _ = await load_latest(
            uow,
            state,
            ArtifactType.ARCHITECTURE_BLUEPRINT,
            ArchitectureBlueprint,
            current_blueprint=True,
        )
        _, dev_artifact = await load_latest(
            uow, state, ArtifactType.DEV_OUTPUT, DevOutput, current_blueprint=True
        )
        attempt = state.retry_count + 1
        tests = ", ".join(t.id for t in blueprint.test_plan)
        task = (
            f"Verifica la entrega del intento {attempt} (DevOutput v{dev_artifact.version}). "
            f"QAReport con blueprint_version = {blueprint.blueprint_version}, attempt = {attempt}"
            f" y un resultado por cada test planificado: {tests}."
        )

        def validator(report: QAReport) -> None:
            require([f"attempt debe ser {attempt}"] if report.attempt != attempt else [])
            report.check_against(blueprint)

        return PreparedTask(task, validator)

    def outcome(self, output: QAReport, state: FeatureDeliveryState) -> AgentOutcome:
        failed = [r.test_id for r in output.results if r.status is TestStatus.FAILED]
        summary = f"QA: {output.verdict.value} en intento {output.attempt}"
        if output.verdict is Verdict.FAIL:
            summary += f" (fallan {', '.join(failed) or 'issues bloqueantes'})"
        return AgentOutcome(
            summary=summary,
            event=output.transition_event,
            qa_errors=output.to_qa_errors() if output.verdict is Verdict.FAIL else [],
        )
