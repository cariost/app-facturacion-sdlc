"""PO: convierte el pedido en un `IntakeBrief` con historias en borrador."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from specforge.agents.base import Agent, AgentOutcome, Clarification, PreparedTask, require
from specforge.core.enums import AgentRole, ApprovalGate, Phase
from specforge.core.transition_guard import TransitionEvent
from specforge.schemas.stories import IntakeBrief, StoryStatus

if TYPE_CHECKING:
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState


class ProductOwnerAgent(Agent[IntakeBrief]):
    role = AgentRole.PO
    node = "intake_po"
    phase = Phase.INTAKE_PO
    output_model = IntakeBrief

    async def prepare(
        self, uow: UnitOfWork, state: FeatureDeliveryState
    ) -> PreparedTask[IntakeBrief]:
        feature = await uow.features.get(state.feature_id)
        profile = feature.business_profile
        task = (
            f"Genera el IntakeBrief de la feature {state.feature_id}.\n"
            f'- module = "{state.module}"; business_profile = {json.dumps(profile)}.\n'
            "- Historias en borrador: status DRAFT, revision 1, IDs desde US-001.\n"
            "- Si hay respuestas de aclaración en las decisiones humanas, incorpóralas y "
            "completa `answer` en la pregunta correspondiente."
        )

        def validator(brief: IntakeBrief) -> None:
            violations = []
            if brief.module != state.module:
                violations.append(f"module debe ser {state.module} (es {brief.module})")
            if brief.business_profile != profile:
                violations.append(f"business_profile debe ser {profile!r}")
            violations += [
                f"{s.story_id} debe ser DRAFT revision 1"
                for s in brief.draft_stories
                if s.status is not StoryStatus.DRAFT or s.revision != 1
            ]
            require(violations)

        return PreparedTask(task, validator)

    def outcome(self, output: IntakeBrief, state: FeatureDeliveryState) -> AgentOutcome:
        stories = ", ".join(s.story_id for s in output.draft_stories)
        if output.requires_clarification:
            ids = tuple(q.id for q in output.blocking_questions)
            return AgentOutcome(
                summary=f"PO: borrador {stories}; aclaración pendiente {', '.join(ids)}",
                clarification=Clarification(
                    ApprovalGate.INTAKE_CLARIFICATION, ids, "preguntas críticas del intake"
                ),
            )
        return AgentOutcome(
            summary=f"PO: intake listo con {stories}", event=TransitionEvent.INTAKE_COMPLETED
        )
