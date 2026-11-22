"""Compactación determinista del `executive_summary` (memoria de trabajo de la feature).

Estructura fija, máximo 15 líneas:

    línea 1   cabecera regenerada en cada escritura (feature · fase · blueprint · intentos)
    línea 2   marcador opcional "… +N eventos previos (ver feature_phase_history)"
    resto     eventos más recientes, uno por línea, en orden cronológico

Cuando no entra todo, los eventos más antiguos se pliegan en el marcador. El detalle nunca
se pierde: vive en `feature_phase_history`, `feature_artifacts` y `agent_execution_logs`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from specforge.schemas.state import EXECUTIVE_SUMMARY_MAX_CHARS, EXECUTIVE_SUMMARY_MAX_LINES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from specforge.schemas.state import FeatureDeliveryState

MAX_LINE_CHARS = 200
_MARKER = re.compile(r"^… \+(\d+) eventos previos")
_HEADER_PREFIX = "■ "


def summary_header(state: FeatureDeliveryState) -> str:
    return (
        f"{_HEADER_PREFIX}{state.feature_id} · {state.phase.value} · bp v{state.blueprint_version}"
        f" · run {state.run_seq:02d} · reintentos {state.retry_count}/{state.max_retries}"
    )


def _clip(line: str, limit: int) -> str:
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _marker(count: int) -> str:
    return f"… +{count} eventos previos (ver feature_phase_history)"


def parse_summary(summary: str) -> tuple[int, list[str]]:
    """Devuelve (eventos plegados, eventos visibles) de un resumen existente."""
    folded = 0
    events: list[str] = []
    for line in summary.splitlines():
        if not line.strip() or line.startswith(_HEADER_PREFIX):
            continue
        marker = _MARKER.match(line)
        if marker:
            folded += int(marker.group(1))
        else:
            events.append(line)
    return folded, events


def compact_summary(
    previous: str,
    entries: Sequence[str],
    *,
    header: str,
    max_lines: int = EXECUTIVE_SUMMARY_MAX_LINES,
    max_chars: int = EXECUTIVE_SUMMARY_MAX_CHARS,
    max_line_chars: int = MAX_LINE_CHARS,
) -> str:
    if max_lines < 3:
        raise ValueError("max_lines debe permitir cabecera, marcador y al menos un evento")
    folded, events = parse_summary(previous)
    for entry in entries:
        line = _clip(entry, max_line_chars)
        if line and (not events or events[-1] != line):
            events.append(line)

    head = _clip(header, max_line_chars)

    def render(folded_count: int, visible: list[str]) -> str:
        lines = [head]
        if folded_count:
            lines.append(_marker(folded_count))
        return "\n".join(lines + visible)

    while True:
        room = max_lines - 1 - (1 if folded else 0)
        if len(events) > room:
            overflow = len(events) - (max_lines - 2)
            folded += overflow
            events = events[overflow:]
            continue
        text = render(folded, events)
        if len(text) <= max_chars or not events:
            return text
        folded += 1
        events = events[1:]
