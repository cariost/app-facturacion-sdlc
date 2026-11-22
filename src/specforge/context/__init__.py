"""Ensamblado de contexto mínimo por nodo, compactación del resumen y embeddings."""

from specforge.context.builder import ContextBuilder, ContextBundle, ContextSegment, SegmentKind
from specforge.context.embeddings import Embedder, HashingEmbedder
from specforge.context.specs import NODE_SPECS, ArtifactNeed, NodeContextSpec
from specforge.context.summary import compact_summary, summary_header
from specforge.context.tokens import estimate_tokens

__all__ = [
    "NODE_SPECS",
    "ArtifactNeed",
    "ContextBuilder",
    "ContextBundle",
    "ContextSegment",
    "Embedder",
    "HashingEmbedder",
    "NodeContextSpec",
    "SegmentKind",
    "compact_summary",
    "estimate_tokens",
    "summary_header",
]
