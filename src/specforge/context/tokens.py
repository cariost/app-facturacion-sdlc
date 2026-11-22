"""Estimación de tokens para presupuestar contexto antes de llamar al modelo.

Es una cota heurística (≈ 4 caracteres por token, redondeo hacia arriba) usada solo para
decidir qué segmentos entran al contexto. Los conteos reales los informa el proveedor en
cada respuesta y son los que se registran en `agent_execution_logs`.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
