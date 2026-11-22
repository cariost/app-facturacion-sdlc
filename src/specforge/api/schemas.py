"""DTOs HTTP de la API de SpecForge."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from specforge.graph.state import InterruptPayload


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateFeatureRequest(_Dto):
    module: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}$")
    tenant_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=3, max_length=300)
    request: str = Field(min_length=10, max_length=8000)
    business_profile: str | None = Field(default=None, max_length=120)
    max_retries: int = Field(default=3, ge=0, le=3)
    active_constraints: list[str] = Field(default_factory=list, max_length=20)
    start: bool = True


class ResumeRequest(_Dto):
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=300)


class ArtifactSummary(_Dto):
    artifact_id: uuid.UUID
    artifact_type: str
    version: int
    blueprint_version: int
    phase: str
    produced_by: str
    schema_name: str
    summary: str
    supersedes_artifact_id: uuid.UUID | None
    execution_log_id: uuid.UUID | None
    thread_id: str
    created_at: datetime


class ArtifactDetail(ArtifactSummary):
    content: dict[str, Any]
    content_hash: str


class ApprovalView(_Dto):
    approval_id: uuid.UUID
    gate: str
    decision: str
    blueprint_version: int
    reviewer: str
    comments: str
    payload: dict[str, Any]
    thread_id: str
    decided_at: datetime


class UsageView(_Dto):
    model_tier: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int


class PhaseHistoryItem(_Dto):
    from_phase: str | None
    to_phase: str
    blueprint_version: int
    retry_count: int
    thread_id: str
    reason: str | None
    changed_at: datetime


class FeatureView(_Dto):
    feature_id: str
    tenant_id: str
    module: str
    business_profile: str | None
    title: str
    phase: str
    status: str
    blocked_from_phase: str | None
    blocked_reason: str | None
    last_transition_reason: str | None
    blueprint_version: int
    run_seq: int
    thread_id: str
    retry_count: int
    max_retries: int
    executive_summary: str
    active_constraints: list[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    pending_interrupt: InterruptPayload | None
    artifacts: list[ArtifactSummary]
    approvals: list[ApprovalView]
    usage: list[UsageView]


class ActionAccepted(_Dto):
    feature_id: str
    action: str
    thread_id: str
