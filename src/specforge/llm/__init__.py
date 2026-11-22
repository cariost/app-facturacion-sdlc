"""Capa de modelos: ruteo FAST/REASONING, prompts estáticos por rol y adaptadores."""

from specforge.llm.observability import InvocationHook, NoopHook, OpenTelemetryHook
from specforge.llm.prompts import ROLE_PROMPTS, RolePrompt, prompt_for
from specforge.llm.router import CallRecord, ModelRouter, RoutedResult
from specforge.llm.routing import ModelSpec, RouteDecision, RouteRequest, RoutingPolicy
from specforge.llm.schema import output_schema
from specforge.llm.types import LLMClient, LLMRequest, LLMResponse, LLMUsage, TextBlock

__all__ = [
    "ROLE_PROMPTS",
    "CallRecord",
    "InvocationHook",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "ModelRouter",
    "ModelSpec",
    "NoopHook",
    "OpenTelemetryHook",
    "RolePrompt",
    "RouteDecision",
    "RouteRequest",
    "RoutedResult",
    "RoutingPolicy",
    "TextBlock",
    "output_schema",
    "prompt_for",
]
