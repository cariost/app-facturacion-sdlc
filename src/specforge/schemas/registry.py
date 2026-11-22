"""Mapeo `ArtifactType` ↔ contrato Pydantic para persistir y rehidratar artefactos."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from specforge.core.enums import ArtifactType
from specforge.schemas.blueprint import ArchitectureBlueprint
from specforge.schemas.common import SCHEMA_VERSION, Contract
from specforge.schemas.delivery import DevOutput, QAReport
from specforge.schemas.stories import IntakeBrief, StoryRefinementOutput

if TYPE_CHECKING:
    from collections.abc import Mapping

C = TypeVar("C", bound=Contract)

ARTIFACT_CONTRACTS: Mapping[ArtifactType, type[Contract]] = MappingProxyType(
    {
        ArtifactType.INTAKE_BRIEF: IntakeBrief,
        ArtifactType.STORY_REFINEMENT: StoryRefinementOutput,
        ArtifactType.ARCHITECTURE_BLUEPRINT: ArchitectureBlueprint,
        ArtifactType.DEV_OUTPUT: DevOutput,
        ArtifactType.QA_REPORT: QAReport,
    }
)


@dataclass(frozen=True, slots=True)
class SerializedArtifact:
    artifact_type: ArtifactType
    schema_name: str
    schema_version: str
    content: dict[str, Any]


def artifact_type_for(contract: Contract | type[Contract]) -> ArtifactType:
    model = contract if isinstance(contract, type) else type(contract)
    for artifact_type, cls in ARTIFACT_CONTRACTS.items():
        if model is cls:
            return artifact_type
    raise TypeError(f"{model.__name__} no está registrado como artefacto")


def serialize(contract: Contract) -> SerializedArtifact:
    return SerializedArtifact(
        artifact_type=artifact_type_for(contract),
        schema_name=type(contract).__name__,
        schema_version=SCHEMA_VERSION,
        content=contract.model_dump(mode="json"),
    )


def deserialize(
    artifact_type: ArtifactType, schema_name: str, content: Mapping[str, Any]
) -> Contract:
    cls = ARTIFACT_CONTRACTS.get(artifact_type)
    if cls is None or cls.__name__ != schema_name:
        raise TypeError(f"no hay contrato para {artifact_type}/{schema_name}")
    return cls.model_validate(content)


def load_as(
    cls: type[C], artifact_type: ArtifactType, schema_name: str, content: Mapping[str, Any]
) -> C:
    contract = deserialize(artifact_type, schema_name, content)
    if not isinstance(contract, cls):
        raise TypeError(f"{schema_name} no es {cls.__name__}")
    return contract
