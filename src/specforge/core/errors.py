"""Errores de dominio. Las violaciones de reglas deterministas que detecta PostgreSQL
(SQLSTATE propios ``SF00x`` y constraints nombrados) se traducen a estas clases."""

from __future__ import annotations


class SpecForgeError(Exception):
    """Raíz de los errores de SpecForge."""


class NotFoundError(SpecForgeError):
    pass


class UnknownModuleError(NotFoundError):
    pass


class ConcurrencyConflictError(SpecForgeError):
    """El `row_version` esperado no coincide (otro proceso modificó la feature)."""


class IdempotencyConflictError(SpecForgeError):
    """Misma `idempotency_key` reutilizada con contenido distinto."""


class InvalidPhaseTransitionError(SpecForgeError):
    """SQLSTATE SF001: arista no permitida en el grafo de fases."""


class RetryPolicyViolationError(SpecForgeError):
    """SQLSTATE SF002: `retry_count` modificado fuera de QA -> IMPLEMENTATION."""


class RetryBudgetExceededError(RetryPolicyViolationError):
    """CHECK features_retry_cap: se superó `max_retries` (máx. 3 ciclos Dev <-> QA)."""


class BlueprintVersionViolationError(SpecForgeError):
    """SQLSTATE SF003: versión de blueprint no incremental o fuera de HUMAN_GATE."""


class ImmutableRecordError(SpecForgeError):
    """SQLSTATE SF004: modificación de identidad, artefacto/aprobación o feature cerrada."""


class RunSequenceViolationError(SpecForgeError):
    """SQLSTATE SF005: `run_seq` no monotónico."""


class IntegrityViolationError(SpecForgeError):
    """Otra violación de integridad no mapeada a una regla específica."""


class ContractViolationError(SpecForgeError):
    """Un artefacto válido en forma viola una regla determinista entre artefactos."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


class StoryProgressionError(ContractViolationError):
    """El refinamiento no respeta la evolución progresiva de las historias."""


class ContextAssemblyError(SpecForgeError):
    """No se pudo armar el contexto mínimo de un nodo (p. ej. falta un artefacto requerido)."""


class ContextBudgetExceededError(ContextAssemblyError):
    """Los segmentos obligatorios superan el presupuesto de tokens del nodo."""


class LLMError(SpecForgeError):
    """Raíz de las fallas en llamadas a modelos."""


class LLMInvocationError(LLMError):
    """El proveedor falló (red, cuota, error HTTP) o la respuesta quedó truncada."""


class LLMRefusalError(LLMInvocationError):
    """El modelo (y su cadena de fallback) rechazó la solicitud."""

    def __init__(self, message: str, *, category: str | None = None) -> None:
        self.category = category
        super().__init__(message)


class LLMOutputValidationError(LLMError):
    """La salida no cumplió el contrato Pydantic tras agotar las reparaciones."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


class GateNotPendingError(SpecForgeError):
    """La acción humana no corresponde a ninguna pausa pendiente de la feature."""


class MigrationError(SpecForgeError):
    """Migración aplicada cuyo contenido cambió (checksum distinto) o archivo inválido."""


SQLSTATE_ERRORS: dict[str, type[SpecForgeError]] = {
    "SF001": InvalidPhaseTransitionError,
    "SF002": RetryPolicyViolationError,
    "SF003": BlueprintVersionViolationError,
    "SF004": ImmutableRecordError,
    "SF005": RunSequenceViolationError,
}

CONSTRAINT_ERRORS: dict[str, type[SpecForgeError]] = {
    "features_retry_cap": RetryBudgetExceededError,
}
