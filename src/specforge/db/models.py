"""Modelos ORM (SQLAlchemy 2.0) que reflejan la migración 0001.

El DDL es propiedad de los archivos SQL de `migrations/`; estos modelos solo mapean
columnas para lectura/escritura tipada. Columnas calculadas por triggers o generadas
(`current_thread_id`, `row_version`, `version` de artefactos…) se marcan como tales.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Computed,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from specforge.core.config import SCHEMA_EMBEDDING_DIM

_THREAD_ID_EXPR = (
    "'sf:' || feature_id || ':v' || blueprint_version::text || ':' || "
    "CASE WHEN run_seq < 10 THEN '0' || run_seq::text ELSE run_seq::text END"
)


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        str: Text,
    }


class Module(Base):
    __tablename__ = "modules"

    code: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    description: Mapped[str] = mapped_column(server_default="")
    last_feature_seq: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BusinessProfile(Base):
    __tablename__ = "business_profiles"

    tenant_id: Mapped[str] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    industry: Mapped[str]
    config: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    config_version: Mapped[int] = mapped_column(Integer, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "business_profile"],
            ["business_profiles.tenant_id", "business_profiles.code"],
            name="features_business_profile_fk",
        ),
    )

    feature_id: Mapped[str] = mapped_column(primary_key=True)
    module: Mapped[str] = mapped_column(ForeignKey("modules.code"))
    tenant_id: Mapped[str]
    business_profile: Mapped[str | None]
    title: Mapped[str]
    request: Mapped[str]

    phase: Mapped[str] = mapped_column(server_default="INTAKE_PO")
    phase_entered_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_transition_reason: Mapped[str | None]
    blocked_from_phase: Mapped[str | None]
    blocked_reason: Mapped[str | None]

    blueprint_version: Mapped[int] = mapped_column(Integer, server_default="1")
    run_seq: Mapped[int] = mapped_column(Integer, server_default="1")
    current_thread_id: Mapped[str] = mapped_column(Computed(_THREAD_ID_EXPR, persisted=True))
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, server_default="3")

    executive_summary: Mapped[str] = mapped_column(server_default="")
    active_constraints: Mapped[list[Any]] = mapped_column(server_default="[]")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")

    row_version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_at: Mapped[datetime | None]


class FeaturePhaseHistory(Base):
    __tablename__ = "feature_phase_history"

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feature_id: Mapped[str] = mapped_column(ForeignKey("features.feature_id"))
    from_phase: Mapped[str | None]
    to_phase: Mapped[str]
    blueprint_version: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    thread_id: Mapped[str]
    reason: Mapped[str | None]
    changed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentExecutionLog(Base):
    __tablename__ = "agent_execution_logs"

    log_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    feature_id: Mapped[str] = mapped_column(ForeignKey("features.feature_id"))
    tenant_id: Mapped[str]
    thread_id: Mapped[str]
    node_name: Mapped[str]
    agent_role: Mapped[str]
    phase: Mapped[str]
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")

    model_provider: Mapped[str | None]
    model_name: Mapped[str | None]
    model_tier: Mapped[str | None]
    prompt_id: Mapped[str | None]
    prompt_hash: Mapped[str | None]
    context_pack_refs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    input_artifact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default="{}"
    )

    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    outcome: Mapped[str] = mapped_column(server_default="RUNNING")
    error_type: Mapped[str | None]
    error_message: Mapped[str | None]

    trace_id: Mapped[str | None]
    span_id: Mapped[str | None]
    langsmith_run_id: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")

    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


class FeatureArtifact(Base):
    __tablename__ = "feature_artifacts"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    feature_id: Mapped[str] = mapped_column(ForeignKey("features.feature_id"))
    artifact_type: Mapped[str]
    # Asignada por el trigger `sf_prepare_artifact` (secuencial por feature + tipo).
    version: Mapped[int] = mapped_column(Integer, server_default=FetchedValue())
    blueprint_version: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str]
    produced_by: Mapped[str]
    schema_name: Mapped[str]
    schema_version: Mapped[str] = mapped_column(server_default="1")
    content: Mapped[dict[str, Any]]
    content_hash: Mapped[str]
    summary: Mapped[str] = mapped_column(server_default="")
    supersedes_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("feature_artifacts.artifact_id")
    )
    thread_id: Mapped[str]
    execution_log_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_execution_logs.log_id")
    )
    idempotency_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FeatureApproval(Base):
    __tablename__ = "feature_approvals"

    approval_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    feature_id: Mapped[str] = mapped_column(ForeignKey("features.feature_id"))
    gate: Mapped[str]
    decision: Mapped[str]
    phase: Mapped[str]
    blueprint_version: Mapped[int] = mapped_column(Integer)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("feature_artifacts.artifact_id")
    )
    reviewer: Mapped[str]
    comments: Mapped[str] = mapped_column(server_default="")
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    thread_id: Mapped[str]
    interrupt_id: Mapped[str | None]
    idempotency_key: Mapped[str | None]
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContextPack(Base):
    __tablename__ = "context_packs"

    pack_code: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    module: Mapped[str] = mapped_column(ForeignKey("modules.code"))
    industry: Mapped[str | None]
    title: Mapped[str]
    content: Mapped[str]
    content_hash: Mapped[str]
    token_estimate: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContextChunk(Base):
    __tablename__ = "context_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["pack_code", "pack_version"],
            ["context_packs.pack_code", "context_packs.version"],
            name="context_chunks_pack_fk",
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    source_kind: Mapped[str]
    pack_code: Mapped[str | None]
    pack_version: Mapped[int | None] = mapped_column(Integer)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("feature_artifacts.artifact_id")
    )
    feature_id: Mapped[str | None] = mapped_column(ForeignKey("features.feature_id"))
    module: Mapped[str] = mapped_column(ForeignKey("modules.code"))
    tenant_id: Mapped[str | None]
    industry: Mapped[str | None]
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str]
    token_count: Mapped[int] = mapped_column(Integer, server_default="0")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(SCHEMA_EMBEDDING_DIM))
    embedding_model: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
