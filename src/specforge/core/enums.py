"""Catálogos cerrados del ciclo de vida. Cada valor tiene su CHECK equivalente en SQL."""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    INTAKE_PO = "INTAKE_PO"
    STORY_REFINEMENT = "STORY_REFINEMENT"
    BLUEPRINT_DESIGN = "BLUEPRINT_DESIGN"
    HUMAN_GATE = "HUMAN_GATE"
    IMPLEMENTATION = "IMPLEMENTATION"
    QA_VERIFICATION = "QA_VERIFICATION"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES: frozenset[Phase] = frozenset({Phase.COMPLETED, Phase.FAILED})


class AgentRole(StrEnum):
    PO = "PO"
    BUSINESS_EXPERT = "BUSINESS_EXPERT"
    ARCHITECT_TECH_LEAD = "ARCHITECT_TECH_LEAD"
    DEV = "DEV"
    QA = "QA"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class ArtifactType(StrEnum):
    INTAKE_BRIEF = "INTAKE_BRIEF"
    USER_STORY_SET = "USER_STORY_SET"
    STORY_REFINEMENT = "STORY_REFINEMENT"
    ARCHITECTURE_BLUEPRINT = "ARCHITECTURE_BLUEPRINT"
    DEV_OUTPUT = "DEV_OUTPUT"
    QA_REPORT = "QA_REPORT"
    EPISODIC_SUMMARY = "EPISODIC_SUMMARY"
    ADR = "ADR"


class ApprovalGate(StrEnum):
    INTAKE_CLARIFICATION = "INTAKE_CLARIFICATION"
    STORY_CLARIFICATION = "STORY_CLARIFICATION"
    BLUEPRINT_APPROVAL = "BLUEPRINT_APPROVAL"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"
    CLARIFIED = "CLARIFIED"


class ExecutionOutcome(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    LLM_ERROR = "LLM_ERROR"
    INTERRUPTED = "INTERRUPTED"
    SKIPPED = "SKIPPED"


class ModelTier(StrEnum):
    FAST = "FAST"
    REASONING = "REASONING"


class ChunkSourceKind(StrEnum):
    CONTEXT_PACK = "CONTEXT_PACK"
    ADR = "ADR"
    BLUEPRINT = "BLUEPRINT"
    EPISODIC = "EPISODIC"


# Módulos sembrados por la migración 0001. Nuevos módulos se agregan con INSERT en
# `modules` (catálogo extensible) sin cambiar código del motor.
SEED_MODULES: tuple[str, ...] = ("CORE", "CRM", "SUNAT", "SIRE")
