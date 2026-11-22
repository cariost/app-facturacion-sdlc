"""Embeddings para la memoria semántica (pgvector, `vector(1536)`).

`Embedder` es el puerto: cualquier proveedor de embeddings se conecta implementándolo.
`HashingEmbedder` es una implementación léxica local y determinista (feature hashing de
unigramas y bigramas normalizados). No requiere red ni credenciales, por lo que sirve como
línea base reproducible en desarrollo y CI; en producción se reemplaza por un modelo de
embeddings semántico con la misma dimensión.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import unicodedata
from typing import TYPE_CHECKING, Protocol

from specforge.core.config import SCHEMA_EMBEDDING_DIM

if TYPE_CHECKING:
    from collections.abc import Sequence

_WORD = re.compile(r"[a-z0-9_.]+")
# Palabras vacías frecuentes en español que solo agregan ruido al hashing léxico.
_STOPWORDS = frozenset(
    [
        "a",
        "al",
        "como",
        "con",
        "de",
        "del",
        "el",
        "en",
        "es",
        "la",
        "las",
        "lo",
        "los",
        "o",
        "para",
        "por",
        "que",
        "se",
        "sin",
        "su",
        "un",
        "una",
        "y",
    ]
)


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def normalize_terms(text: str) -> list[str]:
    """Minúsculas, sin tildes, sin palabras vacías."""
    folded = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return [w.strip(".") for w in _WORD.findall(ascii_text) if w.strip(".") not in _STOPWORDS]


class HashingEmbedder:
    def __init__(self, dimension: int = SCHEMA_EMBEDDING_DIM) -> None:
        if dimension != SCHEMA_EMBEDDING_DIM:
            raise ValueError(f"la dimensión debe ser {SCHEMA_EMBEDDING_DIM} (esquema pgvector)")
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return "specforge-hashing-v1"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_one(self, text: str) -> list[float]:
        terms = normalize_terms(text)
        features = terms + [f"{a} {b}" for a, b in itertools.pairwise(terms)]
        vector = [0.0] * self._dimension
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
