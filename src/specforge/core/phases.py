"""Tabla determinista de transiciones de fase.

Es la fuente Python del grafo de fases; la migración 0001 siembra exactamente las mismas
aristas en `phase_transitions` y un trigger las hace cumplir en PostgreSQL. Un test de
integración verifica la paridad entre ambas.

Reglas adicionales (también forzadas por trigger):
- QA_VERIFICATION -> IMPLEMENTATION es un reintento: `retry_count` sube exactamente 1 y
  nunca supera `max_retries` (<= 3).
- HUMAN_GATE -> BLUEPRINT_DESIGN | STORY_REFINEMENT (cambios solicitados) exige
  `blueprint_version + 1`; al subir versión se reinician `retry_count` y `run_seq`.
- Salir de BLOCKED solo puede volver a la fase previa (`blocked_from_phase`) o a FAILED.
- COMPLETED y FAILED son terminales e inmutables.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from specforge.core.enums import Phase

if TYPE_CHECKING:
    from collections.abc import Mapping

_P = Phase

ALLOWED_TRANSITIONS: Mapping[Phase, frozenset[Phase]] = MappingProxyType(
    {
        _P.INTAKE_PO: frozenset({_P.STORY_REFINEMENT, _P.BLOCKED, _P.FAILED}),
        _P.STORY_REFINEMENT: frozenset({_P.BLUEPRINT_DESIGN, _P.BLOCKED, _P.FAILED}),
        _P.BLUEPRINT_DESIGN: frozenset({_P.HUMAN_GATE, _P.BLOCKED, _P.FAILED}),
        _P.HUMAN_GATE: frozenset(
            {_P.IMPLEMENTATION, _P.BLUEPRINT_DESIGN, _P.STORY_REFINEMENT, _P.BLOCKED, _P.FAILED}
        ),
        _P.IMPLEMENTATION: frozenset({_P.QA_VERIFICATION, _P.BLOCKED, _P.FAILED}),
        _P.QA_VERIFICATION: frozenset({_P.COMPLETED, _P.IMPLEMENTATION, _P.BLOCKED, _P.FAILED}),
        _P.BLOCKED: frozenset(
            {
                _P.INTAKE_PO,
                _P.STORY_REFINEMENT,
                _P.BLUEPRINT_DESIGN,
                _P.HUMAN_GATE,
                _P.IMPLEMENTATION,
                _P.QA_VERIFICATION,
                _P.FAILED,
            }
        ),
        _P.COMPLETED: frozenset(),
        _P.FAILED: frozenset(),
    }
)

# Transiciones HUMAN_GATE -> fase previa que crean una nueva versión del blueprint.
BLUEPRINT_REVISION_TRANSITIONS: frozenset[tuple[Phase, Phase]] = frozenset(
    {(_P.HUMAN_GATE, _P.BLUEPRINT_DESIGN), (_P.HUMAN_GATE, _P.STORY_REFINEMENT)}
)

RETRY_TRANSITION: tuple[Phase, Phase] = (_P.QA_VERIFICATION, _P.IMPLEMENTATION)


def is_allowed(from_phase: Phase, to_phase: Phase) -> bool:
    return to_phase in ALLOWED_TRANSITIONS[from_phase]


def transition_edges() -> frozenset[tuple[Phase, Phase]]:
    return frozenset((src, dst) for src, dsts in ALLOWED_TRANSITIONS.items() for dst in dsts)
