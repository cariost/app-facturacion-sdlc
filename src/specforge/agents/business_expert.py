"""BUSINESS_EXPERT: refinamiento progresivo de historias, reglas y excepciones."""

from __future__ import annotations

from typing import TYPE_CHECKING

from specforge.agents.base import (
    Agent,
    AgentOutcome,
    Clarification,
    PreparedTask,
    find_latest,
    load_latest,
)
from specforge.core.enums import AgentRole, ApprovalGate, ArtifactType, Phase
from specforge.core.transition_guard import TransitionEvent
from specforge.schemas.stories import (
    IntakeBrief,
    StoryRefinementOutput,
    StoryStatus,
    UserStoryWithConditions,
    validate_progression,
)

if TYPE_CHECKING:
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState


async def current_stories(
    uow: UnitOfWork, state: FeatureDeliveryState
) -> list[UserStoryWithConditions]:
    """Historias vigentes: el último refinamiento o, si no hay, el borrador del intake."""
    refinement = await find_latest(uow, state, ArtifactType.STORY_REFINEMENT, StoryRefinementOutput)
    if refinement is not None:
        return list(refinement[0].stories)
    brief, _ = await load_latest(uow, state, ArtifactType.INTAKE_BRIEF, IntakeBrief)
    return list(brief.draft_stories)


class BusinessExpertAgent(Agent[StoryRefinementOutput]):
    role = AgentRole.BUSINESS_EXPERT
    node = "story_refinement"
    phase = Phase.STORY_REFINEMENT
    output_model = StoryRefinementOutput

    async def prepare(
        self, uow: UnitOfWork, state: FeatureDeliveryState
    ) -> PreparedTask[StoryRefinementOutput]:
        previous = await current_stories(uow, state)
        inventory = "\n".join(
            f"- {s.story_id} revision {s.revision} ({s.status.value}): {s.title}" for s in previous
        )
        task = (
            "Refina el conjunto de historias de forma progresiva.\n"
            f"Historias vigentes:\n{inventory}\n"
            "- Copia sin cambios (misma revision) las historias que no modifiques.\n"
            "- Por cada historia nueva, modificada o eliminada agrega su entrada en `changes`;"
            " una modificada sube su revision en 1 y una nueva empieza en revision 1.\n"
            "- Todo valor de negocio va como parámetro configurable referenciado por las reglas."
        )

        def validator(output: StoryRefinementOutput) -> None:
            validate_progression(previous, output)

        return PreparedTask(task, validator)

    def outcome(self, output: StoryRefinementOutput, state: FeatureDeliveryState) -> AgentOutcome:
        ready = sum(s.status is StoryStatus.READY for s in output.stories)
        changes = ", ".join(f"{c.story_id} {c.change_type.value}" for c in output.changes)
        summary = f"Experto: {len(output.stories)} historias ({ready} READY)" + (
            f"; cambios {changes}" if changes else "; sin cambios"
        )
        if output.ready_for_blueprint:
            return AgentOutcome(summary=summary, event=TransitionEvent.STORIES_READY)
        if output.blocking_questions:
            ids = tuple(q.id for q in output.blocking_questions)
            return AgentOutcome(
                summary=f"{summary}; aclaración pendiente {', '.join(ids)}",
                clarification=Clarification(
                    ApprovalGate.STORY_CLARIFICATION, ids, "preguntas críticas del refinamiento"
                ),
            )
        return AgentOutcome(summary=f"{summary}; requiere otra ronda de refinamiento")
