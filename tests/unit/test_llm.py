import json
import math

import pytest
from anthropic.types import Message

from specforge.context import HashingEmbedder
from specforge.context.embeddings import cosine_similarity, normalize_terms
from specforge.core.config import Settings
from specforge.core.enums import AgentRole, ModelTier
from specforge.core.errors import LLMInvocationError, LLMRefusalError
from specforge.llm import ROLE_PROMPTS, RouteRequest, RoutingPolicy, TextBlock, output_schema
from specforge.llm.anthropic_client import from_anthropic_message, to_anthropic_params
from specforge.llm.types import LLMRequest
from specforge.schemas import ArchitectureBlueprint, StoryRefinementOutput

POLICY = RoutingPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]


class TestEmbeddings:
    def test_vectors_are_deterministic_and_normalized(self) -> None:
        embedder = HashingEmbedder()
        a = embedder.embed_one("Recordatorio por contador de uso")
        assert a == embedder.embed_one("recordatorio  por CONTADOR de úso")
        assert len(a) == 1536
        assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0)

    def test_lexical_similarity_ranks_related_text_first(self) -> None:
        embedder = HashingEmbedder()
        query = embedder.embed_one("avisar cuando el contador de uso supera el umbral")
        related = embedder.embed_one("USAGE: vence cuando el contador supera el umbral de uso")
        unrelated = embedder.embed_one("XML UBL 2.1 firmado con certificado digital")
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)

    def test_normalization_drops_accents_and_stopwords(self) -> None:
        assert normalize_terms("La política de envío") == ["politica", "envio"]


class TestRouting:
    @pytest.mark.parametrize(
        ("role", "tier"),
        [
            (AgentRole.PO, ModelTier.FAST),
            (AgentRole.QA, ModelTier.FAST),
            (AgentRole.BUSINESS_EXPERT, ModelTier.REASONING),
            (AgentRole.ARCHITECT_TECH_LEAD, ModelTier.REASONING),
            (AgentRole.DEV, ModelTier.REASONING),
        ],
    )
    def test_base_tier_per_role(self, role: AgentRole, tier: ModelTier) -> None:
        decision = POLICY.route(RouteRequest(role=role, node="n", context_tokens=1000))
        assert decision.spec.tier is tier

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"repair": 1}, "reparación"),
            ({"context_tokens": 50_000}, "contexto"),
            ({"attempt": 2}, "intento 2"),
        ],
    )
    def test_fast_work_escalates_deterministically(self, kwargs: dict, reason: str) -> None:
        request = RouteRequest(role=AgentRole.QA, node="qa", **{"context_tokens": 1000, **kwargs})
        decision = POLICY.route(request)
        assert decision.spec.tier is ModelTier.REASONING
        assert reason in decision.reason

    def test_defaults_and_forced_tier(self) -> None:
        assert POLICY.model(ModelTier.FAST).name == "claude-haiku-5-5"
        assert POLICY.model(ModelTier.REASONING).name == "claude-opus-5-5"
        forced = POLICY.route(
            RouteRequest(
                role=AgentRole.DEV, node="dev", context_tokens=1, force_tier=ModelTier.FAST
            )
        )
        assert forced.spec.tier is ModelTier.FAST
        with pytest.raises(ValueError, match="no invoca"):
            POLICY.route(RouteRequest(role=AgentRole.HUMAN, node="x", context_tokens=1))


class TestSchemaAndPrompts:
    def test_output_schema_drops_unsupported_constraints(self) -> None:
        schema = output_schema(ArchitectureBlueprint)
        text = json.dumps(schema)
        for keyword in ("maxLength", "minLength", "pattern", "minimum", "maxItems"):
            assert f'"{keyword}"' not in text

        def objects(node: object) -> list[dict]:
            found = []
            if isinstance(node, dict):
                if node.get("type") == "object":
                    found.append(node)
                for value in node.values():
                    found.extend(objects(value))
            elif isinstance(node, list):
                for value in node:
                    found.extend(objects(value))
            return found

        assert all(o["additionalProperties"] is False for o in objects(schema))

    def test_system_prompt_is_static_and_hashable(self) -> None:
        prompt = ROLE_PROMPTS[AgentRole.BUSINESS_EXPERT]
        text = prompt.system_text(StoryRefinementOutput)
        assert prompt.prompt_id == "business_expert.refinement@v1"
        assert "StoryRefinementOutput" in text
        assert '"pattern"' in text  # el prompt conserva el esquema completo
        assert prompt.prompt_hash(StoryRefinementOutput) == prompt.prompt_hash(
            StoryRefinementOutput
        )
        assert set(ROLE_PROMPTS) == {
            AgentRole.PO,
            AgentRole.BUSINESS_EXPERT,
            AgentRole.ARCHITECT_TECH_LEAD,
            AgentRole.DEV,
            AgentRole.QA,
        }


class TestAnthropicAdapter:
    def test_request_marks_cache_breakpoints_and_output_config(self) -> None:
        request = LLMRequest(
            model="claude-opus-5-5",
            max_tokens=16000,
            system=(TextBlock("rol", cache=True),),
            user=(TextBlock("packs", cache=True), TextBlock("estado"), TextBlock("tarea")),
            effort="high",
            output_schema={"type": "object", "additionalProperties": False},
        )
        params = to_anthropic_params(request)
        assert params["system"] == [
            {"type": "text", "text": "rol", "cache_control": {"type": "ephemeral"}}
        ]
        content = params["messages"][0]["content"]
        assert [("cache_control" in b) for b in content] == [True, False, False]
        assert params["output_config"] == {
            "effort": "high",
            "format": {
                "type": "json_schema",
                "schema": {"type": "object", "additionalProperties": False},
            },
        }

    @staticmethod
    def _message(stop_reason: str, **extra: object) -> Message:
        return Message.model_validate(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 300,
                },
                **extra,
            }
        )

    def test_response_maps_usage_including_cache(self) -> None:
        response = from_anthropic_message(self._message("end_turn"))
        assert response.text == '{"ok": true}'
        assert (response.usage.total_input_tokens, response.usage.cache_read_tokens) == (2420, 2000)

    def test_refusal_and_truncation_raise(self) -> None:
        with pytest.raises(LLMRefusalError):
            from_anthropic_message(self._message("refusal"))
        with pytest.raises(LLMInvocationError, match="max_tokens"):
            from_anthropic_message(self._message("max_tokens"))
