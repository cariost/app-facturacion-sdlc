"""API FastAPI sobre el orquestador real (PostgreSQL + LangGraph) con LLM guionado."""

from __future__ import annotations

import uuid

import httpx
import pytest

from specforge.api import create_app
from tests.factories import blueprint, dev_output, intake_brief, qa_report, refinement_v1
from tests.fakes import reply

BODY = {
    "module": "CRM",
    "tenant_id": "acme",
    "title": "Recordatorios de servicio y consultas",
    "request": "Recordar el servicio periódico por tiempo o por uso y atender consultas.",
    "business_profile": "taller-automotriz",
}


@pytest.fixture
async def client(orchestrator):
    transport = httpx.ASGITransport(app=create_app(orchestrator=orchestrator))
    async with httpx.AsyncClient(transport=transport, base_url="http://specforge") as http:
        yield http


async def test_create_run_approve_and_inspect(client, llm) -> None:
    llm.add("intake_po", reply(intake_brief()))
    llm.add("story_refinement", reply(refinement_v1()))
    llm.add("blueprint_design", reply(blueprint(1)))

    created = await client.post("/features", json=BODY)
    assert created.status_code == 201
    fid = created.json()["feature_id"]
    assert created.json()["phase"] == "INTAKE_PO"

    # La ejecución corre en segundo plano y se detiene en HUMAN_GATE.
    view = (await client.get(f"/features/{fid}")).json()
    assert (view["phase"], view["status"]) == ("HUMAN_GATE", "PAUSED")
    assert view["pending_interrupt"]["gate"] == "BLUEPRINT_APPROVAL"
    assert view["thread_id"] == f"sf:{fid}:v1:01"
    assert {u["model_tier"] for u in view["usage"]} == {"FAST", "REASONING"}

    bad = await client.post(
        f"/features/{fid}/approval",
        json={"decision": "CHANGES_REQUESTED", "reviewer": "ana"},
    )
    assert bad.status_code == 422  # pedir cambios exige detalle

    llm.add("implementation", reply(dev_output()))
    llm.add("qa_verification", reply(qa_report()))
    accepted = await client.post(
        f"/features/{fid}/approval", json={"decision": "APPROVED", "reviewer": "ana"}
    )
    assert accepted.status_code == 202
    assert accepted.json() == {
        "feature_id": fid,
        "action": "APPROVED",
        "thread_id": f"sf:{fid}:v1:01",
    }

    view = (await client.get(f"/features/{fid}")).json()
    assert (view["phase"], view["status"]) == ("COMPLETED", "COMPLETED")
    assert view["pending_interrupt"] is None
    assert [a["decision"] for a in view["approvals"]] == ["APPROVED"]

    artifacts = (await client.get(f"/features/{fid}/artifacts")).json()
    assert [a["artifact_type"] for a in artifacts] == [
        "INTAKE_BRIEF",
        "STORY_REFINEMENT",
        "ARCHITECTURE_BLUEPRINT",
        "DEV_OUTPUT",
        "QA_REPORT",
    ]
    only_qa = await client.get(f"/features/{fid}/artifacts", params={"artifact_type": "QA_REPORT"})
    assert len(only_qa.json()) == 1
    detail = (await client.get(f"/features/{fid}/artifacts/{artifacts[2]['artifact_id']}")).json()
    assert detail["content"]["blueprint_version"] == 1

    history = (await client.get(f"/features/{fid}/history")).json()
    assert [h["to_phase"] for h in history][-2:] == ["QA_VERIFICATION", "COMPLETED"]

    again = await client.post(
        f"/features/{fid}/approval", json={"decision": "APPROVED", "reviewer": "ana"}
    )
    assert again.status_code == 409
    resume = await client.post(
        f"/features/{fid}/resume", json={"reviewer": "ops", "reason": "volver a correr"}
    )
    assert resume.status_code == 409


async def test_clarification_endpoint_validates_questions(client, llm) -> None:
    llm.add("intake_po", reply(intake_brief(with_blocking_question=True)))
    fid = (await client.post("/features", json=BODY)).json()["feature_id"]
    view = (await client.get(f"/features/{fid}")).json()
    assert view["pending_interrupt"]["gate"] == "INTAKE_CLARIFICATION"

    unknown = await client.post(
        f"/features/{fid}/clarification", json={"reviewer": "ana", "answers": {"Q-09": "x"}}
    )
    assert unknown.status_code == 422
    assert unknown.json()["violations"] == ["pregunta desconocida: Q-09"]

    gate = await client.post(
        f"/features/{fid}/approval", json={"decision": "APPROVED", "reviewer": "ana"}
    )
    assert gate.status_code == 409

    rejected = await client.post(
        f"/features/{fid}/clarification", json={"reviewer": "ana", "reject": True}
    )
    assert rejected.status_code == 202
    assert (await client.get(f"/features/{fid}")).json()["phase"] == "FAILED"


async def test_not_found_and_validation_errors(client) -> None:
    assert (await client.get("/features/CRM-999")).status_code == 404
    missing = await client.get(f"/features/CRM-999/artifacts/{uuid.uuid4()}")
    assert missing.status_code == 404
    invalid = await client.post("/features", json={**BODY, "module": "crm"})
    assert invalid.status_code == 422
    unknown_module = await client.post("/features", json={**BODY, "module": "HR", "start": False})
    assert unknown_module.status_code == 404
