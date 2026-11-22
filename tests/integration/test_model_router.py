"""ModelRouter contra PostgreSQL: cada llamada queda en `agent_execution_logs`."""

from __future__ import annotations

import pytest

from specforge.context import ContextBuilder, HashingEmbedder
from specforge.context_packs import seed_context_packs
from specforge.core.config import Settings
from specforge.core.enums import AgentRole, ExecutionOutcome, ModelTier
from specforge.core.errors import (
    ContractViolationError,
    LLMInvocationError,
    LLMOutputValidationError,
)
from specforge.llm import ModelRouter, RoutingPolicy, prompt_for
from specforge.llm.types import LLMUsage
from specforge.schemas import FeatureDeliveryState, IntakeBrief
from tests.factories import intake_brief
from tests.fakes import Scripted, ScriptedLLMClient, StepClock

POLICY = RoutingPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
TASK = "Genera el IntakeBrief para el pedido."
VALID = intake_brief().model_dump_json()


@pytest.fixture
async def context(tx):
    embedder = HashingEmbedder()
    await tx(lambda uow: seed_context_packs(uow, embedder=embedder))
    feature = await tx(
        lambda uow: uow.features.create(
            module="CRM",
            tenant_id="acme",
            title="Recordatorios de servicio",
            request="Avisar por tiempo o por contador de uso.",
        )
    )
    state = FeatureDeliveryState.from_feature(feature)
    bundle = await tx(lambda uow: ContextBuilder(embedder=embedder).build(uow, state, "intake_po"))
    return state, bundle


def _router(db, client, **kw) -> ModelRouter:
    return ModelRouter(db=db, client=client, policy=POLICY, clock=StepClock(0.2), **kw)


async def _logs(tx, feature_id: str):
    return await tx(lambda uow: uow.logs.list_for_feature(feature_id))


async def test_success_is_logged_with_tokens_cache_and_latency(db, tx, context) -> None:
    state, bundle = context
    client = ScriptedLLMClient(Scripted(VALID, LLMUsage(900, 350, 4000, 0)))
    prompt = prompt_for(AgentRole.PO)

    result = await _router(db, client).invoke(
        state=state, prompt=prompt, bundle=bundle, task=TASK, output_model=IntakeBrief
    )
    assert result.output == intake_brief()
    assert result.model == "claude-haiku-5-5"

    [request] = client.requests
    assert [b.cache for b in request.system] == [True]
    assert [b.cache for b in request.user] == [True, False, False]
    assert request.effort == "low"
    assert request.output_schema is not None

    [log] = await _logs(tx, state.feature_id)
    assert log.log_id == result.log_id
    assert (log.outcome, log.model_tier, log.model_name) == ("SUCCESS", "FAST", "claude-haiku-5-5")
    assert (log.input_tokens, log.output_tokens, log.cached_tokens) == (4900, 350, 4000)
    assert log.latency_ms == 200
    assert log.thread_id == state.thread_id
    assert log.prompt_id == "po.intake@v1"
    assert log.prompt_hash == prompt.prompt_hash(IntakeBrief)
    assert list(log.context_pack_refs) == list(bundle.pack_refs)
    assert log.metadata_["routing_reason"] == "rol PO → FAST"
    assert log.metadata_["provider_request_id"] == "req_1"
    assert log.finished_at is not None


async def test_invalid_output_is_repaired_on_reasoning_tier(db, tx, context) -> None:
    state, bundle = context
    client = ScriptedLLMClient(Scripted('{"title": "incompleto"}'), Scripted(VALID))
    result = await _router(db, client).invoke(
        state=state,
        prompt=prompt_for(AgentRole.PO),
        bundle=bundle,
        task=TASK,
        output_model=IntakeBrief,
    )
    assert [c.outcome for c in result.calls] == [
        ExecutionOutcome.VALIDATION_ERROR,
        ExecutionOutcome.SUCCESS,
    ]
    assert [c.tier for c in result.calls] == [ModelTier.FAST, ModelTier.REASONING]
    repair_block = client.requests[1].user[-1].text
    assert "problem_statement" in repair_block

    first, second = await _logs(tx, state.feature_id)
    assert first.outcome == "VALIDATION_ERROR"
    assert first.error_type == "OutputValidation"
    assert "problem_statement" in first.metadata_["violations"][0]
    assert second.metadata_["routing_reason"] == "escalado: reparación de salida inválida"

    usage = {
        u.model_tier: u for u in await tx(lambda uow: uow.logs.usage_by_tier(state.feature_id))
    }
    assert (usage["FAST"].calls, usage["REASONING"].calls) == (1, 1)


async def test_deterministic_validator_violations_trigger_repair(db, tx, context) -> None:
    state, bundle = context

    def no_blocking_questions(brief: IntakeBrief) -> None:
        if brief.module != "CRM":
            raise ContractViolationError([f"module debe ser CRM (es {brief.module})"])

    wrong = intake_brief().model_copy(update={"module": "SUNAT"}).model_dump_json()
    client = ScriptedLLMClient(Scripted(wrong), Scripted(wrong))
    with pytest.raises(LLMOutputValidationError, match="module debe ser CRM"):
        await _router(db, client).invoke(
            state=state,
            prompt=prompt_for(AgentRole.PO),
            bundle=bundle,
            task=TASK,
            output_model=IntakeBrief,
            validator=no_blocking_questions,
        )
    logs = await _logs(tx, state.feature_id)
    assert [log.outcome for log in logs] == ["VALIDATION_ERROR", "VALIDATION_ERROR"]


async def test_provider_failure_is_logged_as_llm_error(db, tx, context) -> None:
    state, bundle = context
    client = ScriptedLLMClient(Scripted(error=TimeoutError("sin respuesta")))
    with pytest.raises(LLMInvocationError, match="TimeoutError"):
        await _router(db, client).invoke(
            state=state,
            prompt=prompt_for(AgentRole.PO),
            bundle=bundle,
            task=TASK,
            output_model=IntakeBrief,
        )
    [log] = await _logs(tx, state.feature_id)
    assert (log.outcome, log.error_type, log.input_tokens) == ("LLM_ERROR", "TimeoutError", 0)
    assert log.finished_at is not None


async def test_prompt_and_context_role_must_match(db, context) -> None:
    state, bundle = context
    with pytest.raises(ValueError, match="prompt de DEV"):
        await _router(db, ScriptedLLMClient()).invoke(
            state=state,
            prompt=prompt_for(AgentRole.DEV),
            bundle=bundle,
            task=TASK,
            output_model=IntakeBrief,
        )
