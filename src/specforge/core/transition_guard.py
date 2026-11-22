"""`TransitionGuard`: decide de forma determinista la siguiente fase a partir de un evento.

Los nodos agénticos NO eligen fases: emiten un `TransitionEvent` (p. ej. `QA_FAILED`) y el
guard calcula la `TransitionDecision` aplicando las reglas del ciclo de vida. La decisión
es exactamente la que acepta el trigger de PostgreSQL (misma arista, mismo incremento de
`retry_count` / `blueprint_version`), de modo que BD y orquestador nunca divergen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn, Protocol

from specforge.core.enums import TERMINAL_PHASES, ApprovalDecision, Phase
from specforge.core.errors import InvalidPhaseTransitionError
from specforge.core.phases import is_allowed

if TYPE_CHECKING:
    from collections.abc import Mapping


class TransitionEvent(StrEnum):
    INTAKE_COMPLETED = "INTAKE_COMPLETED"
    STORIES_READY = "STORIES_READY"
    BLUEPRINT_READY = "BLUEPRINT_READY"
    BLUEPRINT_APPROVED = "BLUEPRINT_APPROVED"
    BLUEPRINT_CHANGES_REQUESTED = "BLUEPRINT_CHANGES_REQUESTED"
    STORY_CHANGES_REQUESTED = "STORY_CHANGES_REQUESTED"
    BLUEPRINT_REJECTED = "BLUEPRINT_REJECTED"
    IMPLEMENTATION_DONE = "IMPLEMENTATION_DONE"
    QA_PASSED = "QA_PASSED"
    QA_FAILED = "QA_FAILED"
    BLOCK = "BLOCK"
    RESUME = "RESUME"
    FAIL = "FAIL"


class ChangeTarget(StrEnum):
    """Qué artefacto debe rehacerse cuando el humano solicita cambios en HUMAN_GATE."""

    BLUEPRINT = "BLUEPRINT"
    STORIES = "STORIES"


class GuardSnapshot(Protocol):
    """Lo mínimo que el guard necesita (lo cumplen `Feature` y `FeatureDeliveryState`)."""

    @property
    def phase(self) -> str: ...
    @property
    def retry_count(self) -> int: ...
    @property
    def max_retries(self) -> int: ...
    @property
    def blocked_from_phase(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    event: TransitionEvent
    from_phase: Phase
    to_phase: Phase
    reason: str
    count_retry: bool = False
    new_blueprint_version: bool = False
    blocked_reason: str | None = None


_P, _E = Phase, TransitionEvent

# Eventos de avance "simples": (fase actual, evento) -> fase destino.
_SIMPLE: Mapping[tuple[Phase, TransitionEvent], Phase] = MappingProxyType(
    {
        (_P.INTAKE_PO, _E.INTAKE_COMPLETED): _P.STORY_REFINEMENT,
        (_P.STORY_REFINEMENT, _E.STORIES_READY): _P.BLUEPRINT_DESIGN,
        (_P.BLUEPRINT_DESIGN, _E.BLUEPRINT_READY): _P.HUMAN_GATE,
        (_P.HUMAN_GATE, _E.BLUEPRINT_APPROVED): _P.IMPLEMENTATION,
        (_P.HUMAN_GATE, _E.BLUEPRINT_REJECTED): _P.FAILED,
        (_P.IMPLEMENTATION, _E.IMPLEMENTATION_DONE): _P.QA_VERIFICATION,
        (_P.QA_VERIFICATION, _E.QA_PASSED): _P.COMPLETED,
    }
)

# Cambios solicitados en HUMAN_GATE: nueva versión de blueprint.
_REVISIONS: Mapping[TransitionEvent, Phase] = MappingProxyType(
    {
        _E.BLUEPRINT_CHANGES_REQUESTED: _P.BLUEPRINT_DESIGN,
        _E.STORY_CHANGES_REQUESTED: _P.STORY_REFINEMENT,
    }
)

_DEFAULT_REASONS: Mapping[TransitionEvent, str] = MappingProxyType(
    {
        _E.INTAKE_COMPLETED: "intake del PO completado",
        _E.STORIES_READY: "historias listas para blueprint",
        _E.BLUEPRINT_READY: "blueprint listo para revisión humana",
        _E.BLUEPRINT_APPROVED: "blueprint aprobado en HUMAN_GATE",
        _E.BLUEPRINT_CHANGES_REQUESTED: "cambios solicitados al blueprint",
        _E.STORY_CHANGES_REQUESTED: "cambios solicitados a las historias",
        _E.BLUEPRINT_REJECTED: "blueprint rechazado en HUMAN_GATE",
        _E.IMPLEMENTATION_DONE: "implementación entregada a QA",
        _E.QA_PASSED: "QA aprobado",
        _E.QA_FAILED: "QA rechazó la implementación",
        _E.BLOCK: "bloqueo",
        _E.RESUME: "reanudación tras bloqueo",
        _E.FAIL: "falla irrecuperable",
    }
)


class TransitionGuard:
    """Reglas puras (sin I/O) del ciclo de vida de una feature."""

    def decide(
        self,
        snapshot: GuardSnapshot,
        event: TransitionEvent,
        *,
        reason: str | None = None,
    ) -> TransitionDecision:
        current = Phase(snapshot.phase)
        why = reason or _DEFAULT_REASONS[event]
        if current in TERMINAL_PHASES:
            self._reject(current, event, "la feature está cerrada")

        to_phase: Phase
        count_retry = False
        new_blueprint = False
        blocked_reason: str | None = None

        if (current, event) in _SIMPLE:
            to_phase = _SIMPLE[(current, event)]
        elif event in _REVISIONS and current is Phase.HUMAN_GATE:
            to_phase = _REVISIONS[event]
            new_blueprint = True
        elif event is TransitionEvent.QA_FAILED and current is Phase.QA_VERIFICATION:
            if snapshot.retry_count < snapshot.max_retries:
                to_phase, count_retry = Phase.IMPLEMENTATION, True
                why = f"{why} (reintento {snapshot.retry_count + 1}/{snapshot.max_retries})"
            else:
                to_phase = Phase.FAILED
                why = f"{why}: presupuesto de {snapshot.max_retries} reintentos agotado"
        elif event is TransitionEvent.BLOCK and current is not Phase.BLOCKED:
            if reason is None:
                raise ValueError("BLOCK exige un motivo (reason)")
            to_phase, blocked_reason = Phase.BLOCKED, reason
        elif event is TransitionEvent.RESUME and current is Phase.BLOCKED:
            if snapshot.blocked_from_phase is None:
                self._reject(current, event, "no se conoce la fase previa al bloqueo")
            to_phase = Phase(snapshot.blocked_from_phase)
        elif event is TransitionEvent.FAIL:
            to_phase = Phase.FAILED
        else:
            self._reject(current, event, "evento no aplicable en esta fase")

        if not is_allowed(current, to_phase):  # defensa en profundidad: nunca debería ocurrir
            self._reject(current, event, f"arista {current} -> {to_phase} no permitida")
        return TransitionDecision(
            event=event,
            from_phase=current,
            to_phase=to_phase,
            reason=why,
            count_retry=count_retry,
            new_blueprint_version=new_blueprint,
            blocked_reason=blocked_reason,
        )

    def allowed_events(self, snapshot: GuardSnapshot) -> frozenset[TransitionEvent]:
        events: set[TransitionEvent] = set()
        for event in TransitionEvent:
            try:
                self.decide(snapshot, event, reason="probe")
            except InvalidPhaseTransitionError:
                continue
            events.add(event)
        return frozenset(events)

    @staticmethod
    def _reject(current: Phase, event: TransitionEvent, detail: str) -> NoReturn:
        raise InvalidPhaseTransitionError(f"evento {event} inválido en {current}: {detail}")


def event_for_gate_decision(
    decision: ApprovalDecision, target: ChangeTarget = ChangeTarget.BLUEPRINT
) -> TransitionEvent:
    """Traduce la decisión humana de HUMAN_GATE a un evento del guard."""
    if decision is ApprovalDecision.APPROVED:
        return TransitionEvent.BLUEPRINT_APPROVED
    if decision is ApprovalDecision.REJECTED:
        return TransitionEvent.BLUEPRINT_REJECTED
    if decision is ApprovalDecision.CHANGES_REQUESTED:
        return (
            TransitionEvent.STORY_CHANGES_REQUESTED
            if target is ChangeTarget.STORIES
            else TransitionEvent.BLUEPRINT_CHANGES_REQUESTED
        )
    raise ValueError(f"{decision} no es una decisión válida para BLUEPRINT_APPROVAL")
