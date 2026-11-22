"""Qué necesita ver cada nodo del grafo. Todo lo que no figura aquí no entra al contexto."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from specforge.core.enums import AgentRole, ArtifactType, ChunkSourceKind, Phase

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ArtifactNeed:
    artifact_type: ArtifactType
    required: bool = True
    # Debe pertenecer a la versión de blueprint vigente (no a una anterior).
    current_blueprint: bool = False
    # Solo se incluye en un reintento Dev ↔ QA (retry_count > 0).
    only_on_retry: bool = False


@dataclass(frozen=True, slots=True)
class NodeContextSpec:
    node: str
    role: AgentRole
    phase: Phase
    budget_tokens: int
    artifacts: tuple[ArtifactNeed, ...] = ()
    include_request: bool = True
    include_profile: bool = True
    include_core_pack: bool = True
    include_module_pack: bool = True
    # Packs secundarios del módulo (p. ej. CRM.reminders) elegidos por relevancia.
    retrieve_packs: bool = True
    retrieval_k: int = 4
    retrieval_kinds: tuple[ChunkSourceKind, ...] = field(default=(ChunkSourceKind.CONTEXT_PACK,))
    include_human_feedback: bool = True
    include_qa_errors: bool = False


NODE_SPECS: Mapping[str, NodeContextSpec] = MappingProxyType(
    {
        spec.node: spec
        for spec in (
            NodeContextSpec(
                node="intake_po",
                role=AgentRole.PO,
                phase=Phase.INTAKE_PO,
                budget_tokens=8000,
                retrieval_k=3,
            ),
            NodeContextSpec(
                node="story_refinement",
                role=AgentRole.BUSINESS_EXPERT,
                phase=Phase.STORY_REFINEMENT,
                budget_tokens=14000,
                artifacts=(
                    ArtifactNeed(ArtifactType.INTAKE_BRIEF),
                    ArtifactNeed(ArtifactType.STORY_REFINEMENT, required=False),
                ),
            ),
            NodeContextSpec(
                node="blueprint_design",
                role=AgentRole.ARCHITECT_TECH_LEAD,
                phase=Phase.BLUEPRINT_DESIGN,
                budget_tokens=18000,
                artifacts=(
                    ArtifactNeed(ArtifactType.STORY_REFINEMENT),
                    ArtifactNeed(ArtifactType.ARCHITECTURE_BLUEPRINT, required=False),
                ),
                retrieval_kinds=(ChunkSourceKind.CONTEXT_PACK, ChunkSourceKind.ADR),
            ),
            NodeContextSpec(
                node="implementation",
                role=AgentRole.DEV,
                phase=Phase.IMPLEMENTATION,
                budget_tokens=18000,
                include_request=False,
                artifacts=(
                    ArtifactNeed(ArtifactType.ARCHITECTURE_BLUEPRINT, current_blueprint=True),
                    ArtifactNeed(ArtifactType.STORY_REFINEMENT),
                    ArtifactNeed(
                        ArtifactType.DEV_OUTPUT,
                        required=False,
                        current_blueprint=True,
                        only_on_retry=True,
                    ),
                ),
                retrieval_k=3,
                include_human_feedback=False,
                include_qa_errors=True,
            ),
            NodeContextSpec(
                node="qa_verification",
                role=AgentRole.QA,
                phase=Phase.QA_VERIFICATION,
                budget_tokens=14000,
                include_request=False,
                include_profile=False,
                include_core_pack=False,
                retrieve_packs=False,
                artifacts=(
                    ArtifactNeed(ArtifactType.STORY_REFINEMENT),
                    ArtifactNeed(ArtifactType.ARCHITECTURE_BLUEPRINT, current_blueprint=True),
                    ArtifactNeed(ArtifactType.DEV_OUTPUT, current_blueprint=True),
                ),
                include_human_feedback=False,
                include_qa_errors=True,
            ),
        )
    }
)
