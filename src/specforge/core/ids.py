"""Convenciones de identidad de SpecForge.

- feature_id: ``<MODULO>-<NNN>``                 p. ej. ``CRM-001``, ``SUNAT-014``
- thread_id:  ``sf:<feature_id>:v<bp>:<run>``    p. ej. ``sf:CRM-001:v1:01``

La BD calcula `features.current_thread_id` como columna generada con la misma fórmula.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

MODULE_CODE_PATTERN = r"[A-Z][A-Z0-9]{1,15}"
_MODULE_RE = re.compile(rf"^{MODULE_CODE_PATTERN}$")
_FEATURE_RE = re.compile(rf"^(?P<module>{MODULE_CODE_PATTERN})-(?P<seq>[0-9]{{3,}})$")
_THREAD_RE = re.compile(
    rf"^sf:(?P<feature>{MODULE_CODE_PATTERN}-[0-9]{{3,}}):v(?P<bp>[1-9][0-9]*):(?P<run>[0-9]{{2,}})$"
)


class InvalidIdentifierError(ValueError):
    """Identificador que no cumple las convenciones de SpecForge."""


def validate_module_code(code: str) -> str:
    if not _MODULE_RE.fullmatch(code):
        raise InvalidIdentifierError(f"código de módulo inválido: {code!r}")
    return code


@dataclass(frozen=True, slots=True, order=True)
class FeatureId:
    module: str
    seq: int

    def __post_init__(self) -> None:
        validate_module_code(self.module)
        if self.seq < 1:
            raise InvalidIdentifierError(f"la secuencia debe ser >= 1: {self.seq}")

    def __str__(self) -> str:
        return f"{self.module}-{self.seq:03d}"

    @classmethod
    def parse(cls, value: str) -> Self:
        match = _FEATURE_RE.fullmatch(value)
        if match is None:
            raise InvalidIdentifierError(f"feature_id inválido: {value!r}")
        seq_text = match["seq"]
        if len(seq_text) > 3 and seq_text.startswith("0"):
            raise InvalidIdentifierError(f"feature_id con relleno no canónico: {value!r}")
        return cls(module=match["module"], seq=int(seq_text))


@dataclass(frozen=True, slots=True, order=True)
class ThreadId:
    feature_id: FeatureId
    blueprint_version: int
    run_seq: int

    def __post_init__(self) -> None:
        if self.blueprint_version < 1:
            raise InvalidIdentifierError("blueprint_version debe ser >= 1")
        if self.run_seq < 1:
            raise InvalidIdentifierError("run_seq debe ser >= 1")

    def __str__(self) -> str:
        return f"sf:{self.feature_id}:v{self.blueprint_version}:{self.run_seq:02d}"

    @classmethod
    def parse(cls, value: str) -> Self:
        match = _THREAD_RE.fullmatch(value)
        if match is None:
            raise InvalidIdentifierError(f"thread_id inválido: {value!r}")
        run_text = match["run"]
        if len(run_text) > 2 and run_text.startswith("0"):
            raise InvalidIdentifierError(f"thread_id con relleno no canónico: {value!r}")
        return cls(
            feature_id=FeatureId.parse(match["feature"]),
            blueprint_version=int(match["bp"]),
            run_seq=int(run_text),
        )

    def next_run(self) -> ThreadId:
        """Nueva ejecución sobre la misma versión del blueprint."""
        return ThreadId(self.feature_id, self.blueprint_version, self.run_seq + 1)

    def next_blueprint(self) -> ThreadId:
        """Primera ejecución de la siguiente versión del blueprint."""
        return ThreadId(self.feature_id, self.blueprint_version + 1, 1)
