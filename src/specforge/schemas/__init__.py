"""Contratos Pydantic v2 de los agentes y `FeatureDeliveryState`."""

from specforge.schemas.blueprint import ArchitectureBlueprint, Component, Plane, PlannedTest
from specforge.schemas.delivery import DevOutput, FileChange, QAIssue, QAReport, Verdict
from specforge.schemas.registry import ARTIFACT_CONTRACTS, deserialize, load_as, serialize
from specforge.schemas.state import FeatureDeliveryState, PendingInterrupt
from specforge.schemas.stories import (
    AcceptanceCriterion,
    ConfigurableParameter,
    DeterministicRule,
    IntakeBrief,
    OpenQuestion,
    SatisfactionCondition,
    StoryChange,
    StoryException,
    StoryRefinementOutput,
    UserStoryWithConditions,
    validate_progression,
)

__all__ = [
    "ARTIFACT_CONTRACTS",
    "AcceptanceCriterion",
    "ArchitectureBlueprint",
    "Component",
    "ConfigurableParameter",
    "DeterministicRule",
    "DevOutput",
    "FeatureDeliveryState",
    "FileChange",
    "IntakeBrief",
    "OpenQuestion",
    "PendingInterrupt",
    "Plane",
    "PlannedTest",
    "QAIssue",
    "QAReport",
    "SatisfactionCondition",
    "StoryChange",
    "StoryException",
    "StoryRefinementOutput",
    "UserStoryWithConditions",
    "Verdict",
    "deserialize",
    "load_as",
    "serialize",
    "validate_progression",
]
