"""Utilidades compartidas por los repositorios."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_SUMMARY_LINES = 15


def content_hash(content: Mapping[str, Any]) -> str:
    """SHA-256 de la serialización JSON canónica (claves ordenadas, sin espacios)."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
