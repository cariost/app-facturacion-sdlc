"""Paso 6 — end-to-end del caso CRM parametrizable (recordatorio por tiempo/uso + consultas).

Recorre el ciclo completo por la API HTTP sobre el orquestador real (PostgreSQL, LangGraph
con `AsyncPostgresSaver`, ContextBuilder con pgvector, ModelRouter y agentes reales). Lo
único guionado es la respuesta del proveedor LLM (`NodeScriptedLLMClient`): cada respuesta
pasa por la validación Pydantic, los validadores deterministas de cada agente y el
`TransitionGuard`, igual que en producción.

Escenario principal:
1. Refinamiento progresivo en dos rondas (US-001 rev 1 → 2 → 3, US-002 agregada).
2. HUMAN_GATE v1 pausa; el humano pide cambios a las HISTORIAS → blueprint v2 en un hilo
   nuevo (`v2:01`), con la decisión humana inyectada como feedback obligatorio.
3. HUMAN_GATE v2 pausa y se aprueba.
4. QA falla (QA-01) → reintento Dev con la memoria episódica; la primera respuesta del Dev
   omite QA-01, el validador la rechaza y el loop de reparación la corrige → QA PASS.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from specforge.api import create_app
from specforge.context.embeddings import HashingEmbedder
from specforge.core.enums import ApprovalDecision, ArtifactType, ChunkSourceKind, Phase
from specforge.graph import GateResume, RunStatus
from specforge.llm.types import LLMRequest
from specforge.schemas import StoryException, StoryRefinementOutput
from specforge.schemas.stories import StoryChange, StoryChangeType, StoryStatus
from tests.factories import (
    blueprint,
    dev_output,
    inquiry_story,
    intake_brief,
    qa_report,
    reminder_story,
)
from tests.fakes import reply
from tests.integration.conftest import PROFILE

pytestmark = pytest.mark.e2e

BODY = {
    "module": "CRM",
    "tenant_id": "acme",
    "title": "Recordatorios de servicio y consultas",
    "request": (
        "Recordar al cliente su servicio periódico por tiempo o por contador de uso, y atender "
        "consultas y cotizaciones por un canal automatizado con derivación a un asesor."
    ),
    "business_profile": "taller-automotriz",
}
GATE_COMMENT = "Excluir activos dados de baja: no deben recibir recordatorios"
OVERLAY = "CRM.examples.automotive"


# ----------------------------------------------------------- guion del caso CRM


def _modified(story_id: str, rationale: str) -> StoryChange:
    return StoryChange(story_id=story_id, change_type=StoryChangeType.MODIFIED, rationale=rationale)


def refinement_round_1() -> StoryRefinementOutput:
    """US-001 sale de borrador (rev 2, REFINED) y aparece US-002: aún no hay blueprint."""
    return StoryRefinementOutput(
        stories=[reminder_story(status=StoryStatus.REFINED, revision=2), inquiry_story()],
        changes=[
            _modified("US-001", "criterios y reglas por tiempo/uso completados"),
            StoryChange(
                story_id="US-002",
                change_type=StoryChangeType.ADDED,
                rationale="atención automatizada de consultas",
            ),
        ],
    )


def refinement_round_2() -> StoryRefinementOutput:
    """US-001 queda READY (rev 3); US-002 se copia sin cambios."""
    return StoryRefinementOutput(
        stories=[reminder_story(revision=3), inquiry_story()],
        changes=[_modified("US-001", "validada con el responsable de postventa")],
    )


def refinement_after_gate() -> StoryRefinementOutput:
    """Respuesta al cambio pedido en HUMAN_GATE: US-001 rev 4 con una excepción nueva."""
    base = reminder_story(revision=4)
    retired = StoryException(
        id="EX-02",
        condition="el activo fue dado de baja",
        expected_behavior="no se generan recordatorios para ese activo",
        rule_ids=["RD-01", "RD-02"],
    )
    return StoryRefinementOutput(
        stories=[
            base.model_copy(update={"exceptions": [*base.exceptions, retired]}),
            inquiry_story(),
        ],
        changes=[_modified("US-001", "excepción EX-02 solicitada en HUMAN_GATE")],
    )


def _text(request: LLMRequest) -> str:
    return "\n".join(block.text for block in (*request.system, *request.user))


async def _view(client: httpx.AsyncClient, fid: str) -> dict[str, Any]:
    response = await client.get(f"/features/{fid}")
    assert response.status_code == 200
    return response.json()


@pytest.fixture
async def client(orchestrator):
    transport = httpx.ASGITransport(app=create_app(orchestrator=orchestrator))
    async with httpx.AsyncClient(transport=transport, base_url="http://specforge") as http:
        yield http


# ------------------------------------------------------------- escenario principal


async def test_crm_feature_full_lifecycle(client, llm, tx) -> None:
    llm.add("intake_po", reply(intake_brief()))
    llm.add("story_refinement", reply(refinement_round_1()), reply(refinement_round_2()))
    llm.add("blueprint_design", reply(blueprint(1)))

    # 1) Alta + ejecución en segundo plano hasta la primera pausa humana.
    created = await client.post("/features", json=BODY)
    assert created.status_code == 201
    fid = created.json()["feature_id"]
    assert fid.startswith("CRM-")

    view = await _view(client, fid)
    assert (view["phase"], view["status"]) == ("HUMAN_GATE", "PAUSED")
    assert view["thread_id"] == f"sf:{fid}:v1:01"
    gate = view["pending_interrupt"]
    assert (gate["gate"], gate["blueprint_version"]) == ("BLUEPRINT_APPROVAL", 1)

    # Refinamiento progresivo: dos rondas en bp v1; la segunda ve la primera.
    refinements = await tx(
        lambda uow: uow.artifacts.list_for_feature(fid, artifact_type=ArtifactType.STORY_REFINEMENT)
    )
    assert [(a.blueprint_version, a.version) for a in refinements] == [(1, 1), (1, 2)]
    second_round = _text(llm.requests_for("story_refinement")[1])
    assert "US-001 revision 2 (REFINED)" in second_round
    assert "US-002 revision 1 (READY)" in second_round

    # 2) El humano pide cambios a las HISTORIAS: nueva versión de blueprint en otro hilo.
    llm.add("story_refinement", reply(refinement_after_gate()))
    llm.add("blueprint_design", reply(blueprint(2, summary="Políticas por tenant; excluye bajas")))
    changes = await client.post(
        f"/features/{fid}/approval",
        json={
            "decision": "CHANGES_REQUESTED",
            "reviewer": "ana",
            "comments": GATE_COMMENT,
            "target": "STORIES",
        },
    )
    assert changes.status_code == 202
    assert changes.json()["thread_id"] == f"sf:{fid}:v1:01"  # la decisión cierra el hilo v1

    view = await _view(client, fid)
    assert (view["phase"], view["status"]) == ("HUMAN_GATE", "PAUSED")
    assert (view["blueprint_version"], view["thread_id"]) == (2, f"sf:{fid}:v2:01")
    assert view["pending_interrupt"]["blueprint_version"] == 2
    assert view["pending_interrupt"]["summary"] == "Políticas por tenant; excluye bajas"

    # El Experto de negocio v2 parte de las historias vigentes y recibe la decisión humana.
    v2_refinement = _text(llm.requests_for("story_refinement")[2])
    assert "US-001 revision 3 (READY)" in v2_refinement
    assert "BLUEPRINT_APPROVAL CHANGES_REQUESTED · bp v1 · ana" in v2_refinement
    assert GATE_COMMENT in v2_refinement
    assert "blueprint v2 · thread: " + f"sf:{fid}:v2:01" in v2_refinement

    # 3) Aprobación del blueprint v2; QA falla una vez y el Dev corrige con reparación.
    llm.add("implementation", reply(dev_output(1, version=2)))
    llm.add("qa_verification", reply(qa_report(1, version=2, passed=False)))
    llm.add(
        "implementation",
        reply(dev_output(2, version=2, addressed=["QA-02"])),  # omite QA-01 → validador
        reply(dev_output(2, version=2, addressed=["QA-01"])),
    )
    llm.add("qa_verification", reply(qa_report(2, version=2)))
    approved = await client.post(
        f"/features/{fid}/approval", json={"decision": "APPROVED", "reviewer": "luis"}
    )
    assert approved.status_code == 202
    assert approved.json()["thread_id"] == f"sf:{fid}:v2:01"

    view = await _view(client, fid)
    assert (view["phase"], view["status"]) == ("COMPLETED", "COMPLETED")
    assert (view["retry_count"], view["max_retries"]) == (1, 3)
    assert view["pending_interrupt"] is None
    assert view["closed_at"] is not None
    assert [(a["decision"], a["blueprint_version"], a["reviewer"]) for a in view["approvals"]] == [
        ("CHANGES_REQUESTED", 1, "ana"),
        ("APPROVED", 2, "luis"),
    ]

    # Reintento Dev: ve los errores de QA (memoria episódica) y la entrega previa.
    dev_calls = llm.requests_for("implementation")
    assert len(dev_calls) == 3
    retry_prompt = _text(dev_calls[1])
    assert "errores de QA a corregir:" in retry_prompt
    assert "QA-01 [CRITICAL] US-001/AC-02: comparación estricta en RD-02" in retry_prompt
    assert "resuelve y declara en addressed_issues ['QA-01']" in retry_prompt
    repair_prompt = _text(dev_calls[2])
    assert "<repair>" in repair_prompt
    assert "faltan issues bloqueantes en addressed_issues: ['QA-01']" in repair_prompt

    # Bitácora de ejecución: una llamada por intento de modelo, con su outcome.
    logs = await tx(lambda uow: uow.logs.list_for_feature(fid))
    implementation = [(lg.attempt, lg.outcome) for lg in logs if lg.node_name == "implementation"]
    assert implementation == [(1, "SUCCESS"), (2, "VALIDATION_ERROR"), (2, "SUCCESS")]
    # Ruteo: PO/QA van al modelo barato; QA escala a REASONING en el reintento.
    tiers = [(lg.node_name, lg.attempt, lg.model_tier) for lg in logs]
    assert ("intake_po", 1, "FAST") in tiers
    qa_tiers = [(a, t) for n, a, t in tiers if n == "qa_verification"]
    assert qa_tiers == [(1, "FAST"), (2, "REASONING")]
    assert {t for n, _, t in tiers if n in {"blueprint_design", "implementation"}} == {"REASONING"}
    assert all(lg.outcome != "RUNNING" for lg in logs)

    # La memoria episódica queda recuperable por similitud para futuras features del giro.
    embedder = HashingEmbedder()
    [query] = await embedder.embed(["QA-01 comparación estricta umbral RD-02"])
    hits = await tx(
        lambda uow: uow.context.search(
            query,
            module="CRM",
            tenant_id="acme",
            industry="automotriz",
            source_kinds=[ChunkSourceKind.EPISODIC],
        )
    )
    assert [h.chunk.feature_id for h in hits] == [fid]
    assert "QA-01 [CRITICAL]" in hits[0].chunk.content

    # Artefactos versionados por tipo y versión de blueprint.
    artifacts = (await client.get(f"/features/{fid}/artifacts")).json()
    by_type: dict[str, list[int]] = {}
    for artifact in artifacts:
        by_type.setdefault(artifact["artifact_type"], []).append(artifact["blueprint_version"])
    assert by_type == {
        "INTAKE_BRIEF": [1],
        "STORY_REFINEMENT": [1, 1, 2],
        "ARCHITECTURE_BLUEPRINT": [1, 2],
        "DEV_OUTPUT": [2, 2],
        "QA_REPORT": [2, 2],
    }
    final_stories = [a for a in artifacts if a["artifact_type"] == "STORY_REFINEMENT"][-1]
    detail = (await client.get(f"/features/{fid}/artifacts/{final_stories['artifact_id']}")).json()
    us_001 = next(s for s in detail["content"]["stories"] if s["story_id"] == "US-001")
    assert us_001["revision"] == 4
    assert [e["id"] for e in us_001["exceptions"]] == ["EX-01", "EX-02"]

    # Historial de fases: una sola línea de tiempo repartida en dos hilos.
    history = (await client.get(f"/features/{fid}/history")).json()
    assert [h["to_phase"] for h in history] == [
        "INTAKE_PO",
        "STORY_REFINEMENT",
        "BLUEPRINT_DESIGN",
        "HUMAN_GATE",
        "STORY_REFINEMENT",
        "BLUEPRINT_DESIGN",
        "HUMAN_GATE",
        "IMPLEMENTATION",
        "QA_VERIFICATION",
        "IMPLEMENTATION",
        "QA_VERIFICATION",
        "COMPLETED",
    ]
    assert {h["thread_id"] for h in history} == {f"sf:{fid}:v1:01", f"sf:{fid}:v2:01"}

    # Resumen ejecutivo compacto y acotado (memoria de trabajo).
    summary = view["executive_summary"].splitlines()
    assert summary[0] == f"■ {fid} · COMPLETED · bp v2 · run 01 · reintentos 1/3"
    assert summary[-1] == "QA: PASS en intento 2"
    assert any(line.startswith("Gate blueprint v1: CHANGES_REQUESTED (ana)") for line in summary)
    assert len(summary) <= 15
    assert len(view["executive_summary"]) <= 2000

    # Feature cerrada: no acepta más decisiones ni vuelve a invocar modelos.
    calls = len(llm.calls)
    late = await client.post(
        f"/features/{fid}/approval", json={"decision": "APPROVED", "reviewer": "ana"}
    )
    assert late.status_code == 409
    assert len(llm.calls) == calls


# ------------------------------------------------------------- escenarios de borde


async def test_qa_retry_budget_exhausted_fails_the_feature(orchestrator, llm, tx) -> None:
    llm.add("intake_po", reply(intake_brief()))
    llm.add("story_refinement", reply(refinement_round_1()), reply(refinement_round_2()))
    llm.add("blueprint_design", reply(blueprint(1)))
    feature = await orchestrator.create_feature(
        module="CRM",
        tenant_id="acme",
        title=BODY["title"],
        request=BODY["request"],
        business_profile="taller-automotriz",
        max_retries=2,
    )
    fid = feature.feature_id
    assert (await orchestrator.run(fid)).status is RunStatus.PAUSED

    for attempt in (1, 2, 3):
        addressed = ["QA-01"] if attempt > 1 else None
        llm.add("implementation", reply(dev_output(attempt, addressed=addressed)))
        llm.add("qa_verification", reply(qa_report(attempt, passed=False)))
    result = await orchestrator.decide_gate(
        fid, GateResume(decision=ApprovalDecision.APPROVED, reviewer="ana")
    )
    assert (result.status, result.phase) == (RunStatus.FAILED, Phase.FAILED)

    closed = await tx(lambda uow: uow.features.get(fid))
    assert closed.retry_count == 2
    assert "presupuesto de 2 reintentos agotado" in (closed.last_transition_reason or "")
    reports = await tx(
        lambda uow: uow.artifacts.list_for_feature(fid, artifact_type=ArtifactType.QA_REPORT)
    )
    assert len(reports) == 3
    assert len(llm.requests_for("implementation")) == 3  # sin un cuarto intento


async def test_same_engine_other_industry_uses_only_its_configuration(
    orchestrator, llm, tx
) -> None:
    """Agnosticismo de rubro: otro giro reutiliza el flujo sin el overlay automotriz."""
    printing: dict[str, Any] = {
        "tenant_id": "grafix",
        "code": "imprenta-digital",
        "display_name": "Grafix",
        "industry": "imprenta",
        "config": {
            "reminders": {
                "time": {"interval": "P3M"},
                "usage": {"counter": "copias", "threshold": 120000},
            },
            "inquiries": {"handoff_after_turns": 2},
        },
    }
    await tx(lambda uow: uow.profiles.upsert(**printing))

    async def run_until_gate(tenant: str, profile: str, brief_profile: str) -> str:
        brief = intake_brief().model_copy(update={"business_profile": brief_profile})
        llm.add("intake_po", reply(brief))
        llm.add("story_refinement", reply(refinement_round_1()), reply(refinement_round_2()))
        llm.add("blueprint_design", reply(blueprint(1)))
        feature = await orchestrator.create_feature(
            module="CRM",
            tenant_id=tenant,
            title="Recordatorio de mantenimiento preventivo",
            request="Avisar el mantenimiento por tiempo o por contador de uso del equipo.",
            business_profile=profile,
        )
        paused = await orchestrator.run(feature.feature_id)
        assert (paused.status, paused.phase) == (RunStatus.PAUSED, Phase.HUMAN_GATE)
        return feature.feature_id

    grafix = await run_until_gate("grafix", "imprenta-digital", "imprenta-digital")
    acme = await run_until_gate("acme", PROFILE["code"], PROFILE["code"])

    def packs(fid: str) -> Callable[[Any], Any]:
        async def load(uow: Any) -> set[str]:
            logs = await uow.logs.list_for_feature(fid)
            return {ref.split("@")[0] for lg in logs for ref in lg.context_pack_refs}

        return load

    assert OVERLAY not in await tx(packs(grafix))
    assert OVERLAY in await tx(packs(acme))

    grafix_intake, acme_intake = (_text(r) for r in llm.requests_for("intake_po"))
    assert '"counter":"copias"' in grafix_intake
    assert "giro: imprenta" in grafix_intake
    assert "odometro_km" not in grafix_intake
    assert '"counter":"odometro_km"' in acme_intake
