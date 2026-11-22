"""ARCHITECT_TECH_LEAD: diseña el `ArchitectureBlueprint` para las historias READY."""

from __future__ import annotations

from typing import TYPE_CHECKING

from specforge.agents.base import Agent, AgentOutcome, PreparedTask, load_latest, require
from specforge.core.enums import AgentRole, ArtifactType, Phase
from specforge.core.transition_guard import TransitionEvent
from specforge.schemas.blueprint import ArchitectureBlueprint
from specforge.schemas.stories import StoryRefinementOutput

if TYPE_CHECKING:
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState


class ArchitectAgent(Agent[ArchitectureBlueprint]):
    role = AgentRole.ARCHITECT_TECH_LEAD
    node = "blueprint_design"
    phase = Phase.BLUEPRINT_DESIGN
    output_model = ArchitectureBlueprint

    async def prepare(
        self, uow: UnitOfWork, state: FeatureDeliveryState
    ) -> PreparedTask[ArchitectureBlueprint]:
        stories, _ = await load_latest(
            uow, state, ArtifactType.STORY_REFINEMENT, StoryRefinementOutput
        )
        refs = ", ".join(sorted(stories.acceptance_refs()))
        task = (
            f"Diseña el ArchitectureBlueprint con blueprint_version = {state.blueprint_version}.\n"
            f"Criterios de aceptación a cubrir (componente + test): {refs}.\n"
            "Las reglas RD se implementan en componentes DETERMINISTIC."
        )

        def validator(blueprint: ArchitectureBlueprint) -> None:
            violations = []
            if blueprint.blueprint_version != state.blueprint_version:
                violations.append(f"blueprint_version debe ser {state.blueprint_version}")
            if not stories.ready_for_blueprint:
                violations.append("las historias no están READY")
            require(violations)
            blueprint.check_against(stories)

        return PreparedTask(task, validator)

    def outcome(self, output: ArchitectureBlueprint, state: FeatureDeliveryState) -> AgentOutcome:
        return AgentOutcome(
            summary=(
                f"Arquitecto: blueprint v{output.blueprint_version} con {len(output.components)}"
                f" componentes y {len(output.test_plan)} tests; espera aprobación humana"
            ),
            event=TransitionEvent.BLUEPRINT_READY,
        )
