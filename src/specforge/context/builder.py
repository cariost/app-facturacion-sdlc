"""`ContextBuilder`: arma el contexto mínimo de cada nodo a partir de la fuente de verdad.

Principios:
- Solo entra lo que declara el `NodeContextSpec` del nodo; el resto queda en PostgreSQL.
- Los artefactos se resuelven por puntero (`state.artifact_ids`) y, si no hay, por la
  última versión persistida. Nunca viajan en el State.
- Los packs secundarios del módulo se eligen por relevancia (pgvector) en lugar de
  incluirlos completos.
- El resultado se divide en un prefijo estable (packs + perfil, reutilizable por el cache de
  prompts del proveedor) y una parte volátil (artefactos, feedback, pedido, estado).
- Presupuesto de tokens por nodo: lo obligatorio debe entrar; lo opcional entra por
  prioridad y lo que no cabe queda registrado en `dropped`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from specforge.context.specs import NODE_SPECS, ArtifactNeed, NodeContextSpec
from specforge.context.summary import compact_summary, summary_header
from specforge.context.tokens import estimate_tokens
from specforge.core.enums import ApprovalDecision, ChunkSourceKind
from specforge.core.errors import ContextAssemblyError, ContextBudgetExceededError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from specforge.context.embeddings import Embedder
    from specforge.core.enums import AgentRole, Phase
    from specforge.db.models import (
        BusinessProfile,
        ContextPack,
        Feature,
        FeatureApproval,
        FeatureArtifact,
    )
    from specforge.db.repositories import ChunkHit
    from specforge.db.uow import UnitOfWork
    from specforge.schemas.state import FeatureDeliveryState

CORE_PACK = "CORE"
MAX_FEEDBACK_ITEMS = 3
_FEEDBACK_DECISIONS = frozenset(
    {ApprovalDecision.CHANGES_REQUESTED, ApprovalDecision.CLARIFIED, ApprovalDecision.REJECTED}
)


class SegmentKind(StrEnum):
    PACK = "PACK"
    PROFILE = "PROFILE"
    ARTIFACT = "ARTIFACT"
    FEEDBACK = "FEEDBACK"
    REQUEST = "REQUEST"
    STATE = "STATE"


_STABLE_KINDS = frozenset({SegmentKind.PACK, SegmentKind.PROFILE})


@dataclass(frozen=True, slots=True)
class ContextSegment:
    key: str
    kind: SegmentKind
    title: str
    content: str
    required: bool
    tokens: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", estimate_tokens(self.render()))

    @property
    def stable(self) -> bool:
        return self.kind in _STABLE_KINDS

    def render(self) -> str:
        return f'<context key="{self.key}">\n## {self.title}\n{self.content.strip()}\n</context>'


@dataclass(frozen=True, slots=True)
class ContextBundle:
    node: str
    role: AgentRole
    phase: Phase
    budget_tokens: int
    segments: tuple[ContextSegment, ...]
    pack_refs: tuple[str, ...]
    input_artifact_ids: tuple[uuid.UUID, ...]
    dropped: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.segments)

    def segment_keys(self) -> list[str]:
        return [s.key for s in self.segments]

    def segment(self, key: str) -> ContextSegment | None:
        return next((s for s in self.segments if s.key == key), None)

    def stable_text(self) -> str:
        return "\n\n".join(s.render() for s in self.segments if s.stable)

    def volatile_text(self) -> str:
        return "\n\n".join(s.render() for s in self.segments if not s.stable)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pack_ref(pack: ContextPack) -> str:
    return f"{pack.pack_code}@{pack.version}"


class ContextBuilder:
    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        specs: Mapping[str, NodeContextSpec] = NODE_SPECS,
    ) -> None:
        self._embedder = embedder
        self._specs = specs

    def spec_for(self, node: str) -> NodeContextSpec:
        try:
            return self._specs[node]
        except KeyError:
            raise ContextAssemblyError(f"nodo sin especificación de contexto: {node}") from None

    async def build(self, uow: UnitOfWork, state: FeatureDeliveryState, node: str) -> ContextBundle:
        spec = self.spec_for(node)
        if state.phase is not spec.phase:
            raise ContextAssemblyError(
                f"{node} corresponde a {spec.phase.value}; la feature está en {state.phase.value}"
            )
        feature = await uow.features.get(state.feature_id)
        profile = (
            await uow.profiles.get(feature.tenant_id, feature.business_profile)
            if feature.business_profile
            else None
        )
        industry = profile.industry if profile is not None else state.industry

        segments: list[ContextSegment] = []
        missing: list[str] = []
        pack_refs: list[str] = []

        pack_segments = await self._pack_segments(uow, spec, feature, industry, missing)
        for segment, refs in pack_segments:
            segments.append(segment)
            pack_refs.extend(refs)
        if spec.include_profile and profile is not None:
            segments.append(self._profile_segment(profile))

        artifact_ids: list[uuid.UUID] = []
        for need in spec.artifacts:
            if need.only_on_retry and state.retry_count == 0:
                continue
            artifact = await self._resolve_artifact(uow, state, need)
            if artifact is None:
                if need.required:
                    raise ContextAssemblyError(
                        f"{node}: falta el artefacto requerido {need.artifact_type.value}"
                    )
                continue
            segments.append(self._artifact_segment(artifact, required=need.required))
            artifact_ids.append(artifact.artifact_id)

        if spec.include_human_feedback:
            feedback = await self._feedback_segment(uow, state)
            if feedback is not None:
                segments.append(feedback)
        if spec.include_request:
            segments.append(
                ContextSegment(
                    key="request",
                    kind=SegmentKind.REQUEST,
                    title=f"Pedido original: {feature.title}",
                    content=feature.request,
                    required=True,
                )
            )
        segments.append(self._state_segment(state, include_qa_errors=spec.include_qa_errors))

        selected, dropped = self._apply_budget(spec, segments)
        selected_keys = {s.key for s in selected}
        used_refs = tuple(
            ref for (segment, refs) in pack_segments if segment.key in selected_keys for ref in refs
        )
        return ContextBundle(
            node=node,
            role=spec.role,
            phase=spec.phase,
            budget_tokens=spec.budget_tokens,
            segments=tuple(selected),
            pack_refs=tuple(dict.fromkeys(used_refs)),
            input_artifact_ids=tuple(artifact_ids),
            dropped=tuple(dropped),
            missing=tuple(missing),
        )

    async def record_progress(
        self,
        uow: UnitOfWork,
        state: FeatureDeliveryState,
        entries: Sequence[str],
        *,
        active_constraints: Sequence[str] | None = None,
        header_state: FeatureDeliveryState | None = None,
    ) -> FeatureDeliveryState:
        """Compacta el resumen ejecutivo con nuevos eventos y lo persiste en `features`.

        `header_state` permite escribir la cabecera de la fase destino antes de aplicar una
        transición (una feature cerrada ya no admite cambios).
        """
        header = summary_header(header_state or state)
        summary = compact_summary(state.executive_summary, entries, header=header)
        feature = await uow.features.update_working_memory(
            state.feature_id,
            expected_row_version=state.row_version,
            executive_summary=summary,
            active_constraints=active_constraints,
        )
        return state.model_copy(
            update={
                "executive_summary": feature.executive_summary,
                "active_constraints": list(feature.active_constraints),
                "row_version": feature.row_version,
            }
        )

    # ------------------------------------------------------------------ packs

    async def _pack_segments(
        self,
        uow: UnitOfWork,
        spec: NodeContextSpec,
        feature: Feature,
        industry: str | None,
        missing: list[str],
    ) -> list[tuple[ContextSegment, list[str]]]:
        result: list[tuple[ContextSegment, list[str]]] = []
        whole: list[str] = []
        if spec.include_core_pack and feature.module != CORE_PACK:
            whole.append(CORE_PACK)
        if spec.include_module_pack:
            whole.append(feature.module)
        for code in whole:
            pack = await uow.context.active_pack(code)
            if pack is None:
                missing.append(f"pack:{code}")
                continue
            result.append((self._whole_pack_segment(pack, required=False), [_pack_ref(pack)]))

        if not spec.retrieve_packs:
            return result
        candidates = [
            p
            for p in await uow.context.active_packs_for(feature.module, industry=industry)
            if p.pack_code not in whole
        ]
        overlays = [p for p in candidates if p.industry is not None]
        for pack in overlays:
            result.append((self._whole_pack_segment(pack, required=False), [_pack_ref(pack)]))
        generic = [p for p in candidates if p.industry is None]
        if self._embedder is None:
            # Sin embeddings no hay ranking: los packs entran completos según presupuesto.
            for pack in generic:
                result.append((self._whole_pack_segment(pack, required=False), [_pack_ref(pack)]))
            return result

        query = f"{feature.title}\n{feature.request}"
        [vector] = await self._embedder.embed([query])
        hits = await uow.context.search(
            vector,
            module=feature.module,
            tenant_id=feature.tenant_id,
            industry=industry,
            source_kinds=spec.retrieval_kinds,
            k=spec.retrieval_k,
        )
        excluded = set(whole) | {p.pack_code for p in overlays}
        result.extend(self._retrieved_segments(hits, excluded))
        return result

    @staticmethod
    def _whole_pack_segment(pack: ContextPack, *, required: bool) -> ContextSegment:
        return ContextSegment(
            key=f"pack:{_pack_ref(pack)}",
            kind=SegmentKind.PACK,
            title=pack.title,
            content=pack.content,
            required=required,
        )

    @staticmethod
    def _retrieved_segments(
        hits: Sequence[ChunkHit], excluded: set[str]
    ) -> list[tuple[ContextSegment, list[str]]]:
        grouped: dict[str, list[ChunkHit]] = {}
        for hit in hits:
            chunk = hit.chunk
            if chunk.pack_code is not None and chunk.pack_code in excluded:
                continue
            source = (
                f"{chunk.pack_code}@{chunk.pack_version}"
                if chunk.pack_code is not None
                else f"{chunk.source_kind}:{chunk.artifact_id or chunk.feature_id}"
            )
            grouped.setdefault(source, []).append(hit)
        segments: list[tuple[ContextSegment, list[str]]] = []
        # Orden por fuente e índice (no por distancia) para que el prefijo sea estable.
        for source in sorted(grouped):
            chunk_hits = sorted(grouped[source], key=lambda h: h.chunk.chunk_index)
            indexes = ",".join(str(h.chunk.chunk_index) for h in chunk_hits)
            first = chunk_hits[0].chunk
            is_pack = first.source_kind == ChunkSourceKind.CONTEXT_PACK.value
            segment = ContextSegment(
                key=f"{'pack' if is_pack else 'retrieved'}:{source}#{indexes}",
                kind=SegmentKind.PACK if is_pack else SegmentKind.ARTIFACT,
                title=f"Fragmentos relevantes de {source}",
                content="\n\n".join(h.chunk.content for h in chunk_hits),
                required=False,
            )
            segments.append((segment, [source] if is_pack else []))
        return segments

    # ------------------------------------------------------- otros segmentos

    @staticmethod
    def _profile_segment(profile: BusinessProfile) -> ContextSegment:
        content = (
            f"tenant: {profile.tenant_id} · perfil: {profile.code} · giro: {profile.industry}"
            f" · config_version: {profile.config_version}\n"
            f"config: {_canonical_json(profile.config)}"
        )
        return ContextSegment(
            key=f"profile:{profile.tenant_id}/{profile.code}@{profile.config_version}",
            kind=SegmentKind.PROFILE,
            title=f"Perfil de negocio {profile.display_name}",
            content=content,
            required=True,
        )

    @staticmethod
    async def _resolve_artifact(
        uow: UnitOfWork, state: FeatureDeliveryState, need: ArtifactNeed
    ) -> FeatureArtifact | None:
        version = state.blueprint_version if need.current_blueprint else None
        pointer = state.artifact_id(need.artifact_type)
        if pointer is not None:
            artifact = await uow.artifacts.get(pointer)
            if (
                artifact.feature_id == state.feature_id
                and artifact.artifact_type == need.artifact_type.value
                and (version is None or artifact.blueprint_version == version)
            ):
                return artifact
        return await uow.artifacts.latest(
            state.feature_id, need.artifact_type, blueprint_version=version
        )

    @staticmethod
    def _artifact_segment(artifact: FeatureArtifact, *, required: bool) -> ContextSegment:
        return ContextSegment(
            key=f"artifact:{artifact.artifact_type}@v{artifact.version}",
            kind=SegmentKind.ARTIFACT,
            title=(
                f"{artifact.artifact_type} v{artifact.version} (blueprint v"
                f"{artifact.blueprint_version}, {artifact.produced_by}, {artifact.schema_name})"
            ),
            content=_canonical_json(artifact.content),
            required=required,
        )

    @staticmethod
    async def _feedback_segment(
        uow: UnitOfWork, state: FeatureDeliveryState
    ) -> ContextSegment | None:
        approvals: list[FeatureApproval] = [
            a
            for a in await uow.approvals.list_for_feature(state.feature_id)
            if ApprovalDecision(a.decision) in _FEEDBACK_DECISIONS
            and a.blueprint_version >= state.blueprint_version - 1
        ]
        if not approvals:
            return None
        recent = approvals[-MAX_FEEDBACK_ITEMS:]
        lines = []
        for a in recent:
            detail = f" · datos: {_canonical_json(a.payload)}" if a.payload else ""
            lines.append(
                f"- [{a.gate} {a.decision} · bp v{a.blueprint_version} · {a.reviewer}] "
                f"{a.comments}{detail}"
            )
        return ContextSegment(
            key=f"feedback:{recent[-1].approval_id}",
            kind=SegmentKind.FEEDBACK,
            title="Decisiones humanas vigentes (obligatorias)",
            content="\n".join(lines),
            required=True,
        )

    @staticmethod
    def _state_segment(state: FeatureDeliveryState, *, include_qa_errors: bool) -> ContextSegment:
        lines = [
            f"feature: {state.feature_id} · fase: {state.phase.value}"
            f" · blueprint v{state.blueprint_version} · thread: {state.thread_id}"
            f" · intento {state.retry_count + 1}/{state.max_retries + 1}"
        ]
        if state.active_constraints:
            lines.append("restricciones activas:")
            lines.extend(f"- {c}" for c in state.active_constraints)
        if state.executive_summary:
            lines.append("resumen ejecutivo:")
            lines.append(state.executive_summary)
        if include_qa_errors and state.qa_errors:
            lines.append("errores de QA a corregir:")
            lines.extend(f"- {e}" for e in state.qa_errors)
        return ContextSegment(
            key="state",
            kind=SegmentKind.STATE,
            title="Estado de la feature",
            content="\n".join(lines),
            required=True,
        )

    # ------------------------------------------------------------ presupuesto

    @staticmethod
    def _apply_budget(
        spec: NodeContextSpec, segments: list[ContextSegment]
    ) -> tuple[list[ContextSegment], list[str]]:
        required_tokens = sum(s.tokens for s in segments if s.required)
        if required_tokens > spec.budget_tokens:
            raise ContextBudgetExceededError(
                f"{spec.node}: lo obligatorio suma ~{required_tokens} tokens y el presupuesto"
                f" es {spec.budget_tokens}"
            )
        remaining = spec.budget_tokens - required_tokens
        # Prioridad de lo opcional: artefactos previos, packs del módulo, overlays, fragmentos.
        priority = {SegmentKind.ARTIFACT: 0, SegmentKind.PACK: 1}
        optional = sorted(
            (i for i, s in enumerate(segments) if not s.required),
            key=lambda i: (priority.get(segments[i].kind, 2), i),
        )
        keep: set[int] = {i for i, s in enumerate(segments) if s.required}
        dropped: list[str] = []
        for i in optional:
            if segments[i].tokens <= remaining:
                keep.add(i)
                remaining -= segments[i].tokens
            else:
                dropped.append(segments[i].key)
        stable = [segments[i] for i in sorted(keep) if segments[i].stable]
        volatile = [segments[i] for i in sorted(keep) if not segments[i].stable]
        return stable + volatile, dropped
