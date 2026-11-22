"""Carga de Context Packs desde Markdown con front matter TOML.

Formato de cada archivo en `context_packs/data/`::

    +++
    pack_code = "CRM.reminders"
    module = "CRM"
    title = "..."
    industry = "automotriz"   # opcional: overlay de giro; sin él, el pack es agnóstico
    +++
    Introducción...

    ## Sección
    ...

Cada sección `## ` es un chunk de la memoria semántica; la introducción es el chunk 0.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources

from specforge.context.tokens import estimate_tokens
from specforge.db.repositories._common import text_hash

PACK_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}(\.[a-z0-9_]+)*$")
INDUSTRY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_FRONT_MATTER = re.compile(r"\A\+\+\+\n(?P<meta>.*?)\n\+\+\+\n(?P<body>.*)\Z", re.DOTALL)
_DATA_PACKAGE = "specforge.context_packs.data"


class PackFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PackChunk:
    index: int
    heading: str
    content: str

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)


@dataclass(frozen=True, slots=True)
class PackDefinition:
    pack_code: str
    module: str
    title: str
    content: str
    industry: str | None = None
    source: str = ""

    @property
    def content_hash(self) -> str:
        return text_hash(self.content)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)

    @property
    def is_base(self) -> bool:
        """El pack raíz del módulo (`CRM`, `SUNAT`...) se incluye completo en el contexto."""
        return self.pack_code == self.module

    def chunks(self) -> list[PackChunk]:
        parts = re.split(r"(?m)^## ", self.content)
        chunks: list[PackChunk] = []
        intro = parts[0].strip()
        if intro:
            chunks.append(PackChunk(0, self.title, f"# {self.title}\n{intro}"))
        for part in parts[1:]:
            heading, _, body = part.partition("\n")
            text = f"# {self.title}\n## {heading.strip()}\n{body.strip()}"
            chunks.append(PackChunk(len(chunks), heading.strip(), text))
        return chunks


def parse_pack(text: str, *, source: str = "") -> PackDefinition:
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if match is None:
        raise PackFormatError(f"{source}: falta el front matter TOML (+++ ... +++)")
    try:
        meta = tomllib.loads(match["meta"])
    except tomllib.TOMLDecodeError as exc:
        raise PackFormatError(f"{source}: front matter inválido: {exc}") from exc
    unknown = set(meta) - {"pack_code", "module", "title", "industry"}
    if unknown:
        raise PackFormatError(f"{source}: claves desconocidas {sorted(unknown)}")
    try:
        pack_code, module, title = str(meta["pack_code"]), str(meta["module"]), str(meta["title"])
    except KeyError as exc:
        raise PackFormatError(f"{source}: falta {exc.args[0]}") from exc
    industry = meta.get("industry")
    if not PACK_CODE_RE.match(pack_code):
        raise PackFormatError(f"{source}: pack_code inválido {pack_code!r}")
    if pack_code.split(".", 1)[0] != module:
        raise PackFormatError(f"{source}: el prefijo de {pack_code} debe ser el módulo {module}")
    if industry is not None and not INDUSTRY_RE.match(str(industry)):
        raise PackFormatError(f"{source}: industry inválida {industry!r}")
    body = match["body"].strip()
    if not body:
        raise PackFormatError(f"{source}: contenido vacío")
    return PackDefinition(
        pack_code=pack_code,
        module=module,
        title=title,
        content=body + "\n",
        industry=str(industry) if industry is not None else None,
        source=source,
    )


def load_seed_packs() -> list[PackDefinition]:
    """Packs iniciales empaquetados con SpecForge, ordenados por `pack_code`."""
    root = resources.files(_DATA_PACKAGE)
    packs = [
        parse_pack(entry.read_text(encoding="utf-8"), source=entry.name)
        for entry in root.iterdir()
        if entry.name.endswith(".md")
    ]
    codes = [p.pack_code for p in packs]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    if duplicated:
        raise PackFormatError(f"pack_code duplicados: {duplicated}")
    return sorted(packs, key=lambda p: p.pack_code)
