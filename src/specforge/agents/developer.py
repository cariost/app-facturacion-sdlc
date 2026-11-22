"""DEV: implementa el blueprint aprobado y, en un reintento, los issues de QA."""

from __future__ import annotations

from typing import TYPE_CHECKING

from specforge.agents.base import (
    Agent,
    AgentOutcome,
    PreparedTask,
    find_latest,
    load_latest,
    require,
)
from specforge.core.enums import AgentRole, ArtifactType, Phase
from specforge.core.transition_guard import TransitionEvent
from specforge.schemas.blueprint import ArchitectureBlueprint
from specforge.schemas.delivery import DevOutput, QAReport

if TYPE_CHECKING:
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState


class DeveloperAgent(Agent[DevOutput]):
    role = AgentRole.DEV
    node = "implementation"
    phase = Phase.IMPLEMENTATION
    output_model = DevOutput

    async def prepare(
        self, uow: UnitOfWork, state: FeatureDeliveryState
    ) -> PreparedTask[DevOutput]:
        blueprint, _ = await load_latest(
            uow,
            state,
            ArtifactType.ARCHITECTURE_BLUEPRINT,
            ArchitectureBlueprint,
            current_blueprint=True,
        )
        attempt = state.retry_count + 1
        must_address: list[str] = []
        if state.retry_count > 0:
            report = await find_latest(
                uow, state, ArtifactType.QA_REPORT, QAReport, current_blueprint=True
            )
            if report is not None:
                must_address = [i.id for i in report[0].issues if i.is_release_blocking]
        task = (
            f"Implementa el blueprint v{blueprint.blueprint_version}: DevOutput con "
            f"blueprint_version = {blueprint.blueprint_version} y attempt = {attempt}."
        )
        if must_address:
            task += f"\nEs un reintento: resuelve y declara en addressed_issues {must_address}."

        def validator(output: DevOutput) -> None:
            violations = []
            if output.attempt != attempt:
                violations.append(f"attempt debe ser {attempt}")
            missing = sorted(set(must_address) - set(output.addressed_issues))
            if missing:
                violations.append(f"faltan issues bloqueantes en addressed_issues: {missing}")
            require(violations)
            output.check_against(blueprint)

        return PreparedTask(task, validator)

    def outcome(self, output: DevOutput, state: FeatureDeliveryState) -> AgentOutcome:
        fixes = (
            f"; resuelve {', '.join(output.addressed_issues)}" if output.addressed_issues else ""
        )
        return AgentOutcome(
            summary=f"Dev: intento {output.attempt}, {len(output.files)} archivos{fixes}",
            event=TransitionEvent.IMPLEMENTATION_DONE,
        )
