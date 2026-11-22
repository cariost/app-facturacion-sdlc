"""JSON schema de salida apto para salidas estructuradas del proveedor.

El esquema de un contrato Pydantic se simplifica quitando restricciones que el proveedor no
admite (longitudes, rangos, patrones, cardinalidades). No se pierde validación: la salida
siempre se valida después contra el contrato completo con Pydantic.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


# Claves cuyo valor es un mapa de nombres (no de palabras clave del esquema).
_NAME_MAPS = frozenset({"properties", "$defs", "definitions"})


def _simplify(node: Any, *, names: bool = False) -> Any:
    if isinstance(node, dict):
        if names:
            return {k: _simplify(v) for k, v in node.items()}
        cleaned = {
            k: _simplify(v, names=k in _NAME_MAPS)
            for k, v in node.items()
            if k not in UNSUPPORTED_KEYWORDS
        }
        is_object = cleaned.get("type") == "object" or "properties" in cleaned
        if is_object and not isinstance(cleaned.get("additionalProperties"), dict):
            cleaned["additionalProperties"] = False
        return cleaned
    if isinstance(node, list):
        return [_simplify(v) for v in node]
    return node


@lru_cache(maxsize=64)
def _cached(model: type[BaseModel]) -> str:
    return json.dumps(_simplify(model.model_json_schema()), sort_keys=True, ensure_ascii=False)


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Esquema simplificado (copia nueva en cada llamada, serialización estable)."""
    loaded: dict[str, Any] = json.loads(_cached(model))
    return loaded


def output_schema_text(model: type[BaseModel]) -> str:
    """Esquema completo (con restricciones) en JSON canónico, para el prompt de sistema."""
    return json.dumps(model.model_json_schema(), sort_keys=True, ensure_ascii=False)
