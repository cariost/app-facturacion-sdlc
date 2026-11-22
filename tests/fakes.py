"""Dobles de prueba explícitos (solo para tests): cliente LLM con respuestas guionadas y reloj."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from specforge.llm.types import LLMRequest, LLMResponse, LLMUsage


@dataclass
class Scripted:
    text: str = ""
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(1200, 300, 0, 0))
    error: Exception | None = None


class ScriptedLLMClient:
    """Devuelve las respuestas en orden y registra cada request recibido."""

    def __init__(self, *responses: Scripted) -> None:
        self._responses = deque(responses)
        self.requests: list[LLMRequest] = []

    @property
    def provider(self) -> str:
        return "scripted"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("ScriptedLLMClient sin respuestas pendientes")
        item = self._responses.popleft()
        if item.error is not None:
            raise item.error
        return LLMResponse(
            text=item.text,
            usage=item.usage,
            model=request.model,
            stop_reason="end_turn",
            request_id=f"req_{len(self.requests)}",
        )


class StepClock:
    """Reloj monotónico que avanza `step` segundos en cada lectura."""

    def __init__(self, step: float = 0.25) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class NodeScriptedLLMClient:
    """Respuestas guionadas por nodo del grafo (`LLMRequest.metadata["node"]`)."""

    def __init__(self, script: dict[str, list[Scripted]] | None = None) -> None:
        self._script = {node: deque(items) for node, items in (script or {}).items()}
        self.calls: list[tuple[str, LLMRequest]] = []

    def add(self, node: str, *items: Scripted) -> None:
        self._script.setdefault(node, deque()).extend(items)

    @property
    def provider(self) -> str:
        return "scripted"

    def requests_for(self, node: str) -> list[LLMRequest]:
        return [r for n, r in self.calls if n == node]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        node = request.metadata["node"]
        self.calls.append((node, request))
        queue = self._script.get(node)
        if not queue:
            raise AssertionError(f"sin respuesta guionada para el nodo {node}")
        item = queue.popleft()
        if item.error is not None:
            raise item.error
        return LLMResponse(
            text=item.text,
            usage=item.usage,
            model=request.model,
            stop_reason="end_turn",
            request_id=f"req_{len(self.calls)}",
        )


def reply(contract) -> Scripted:
    return Scripted(contract.model_dump_json())
