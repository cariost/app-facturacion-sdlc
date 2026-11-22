"""Prompts de sistema estáticos por rol.

Son constantes versionadas: no contienen fechas, IDs ni datos de la feature, de modo que el
bloque de sistema (prompt + esquema del contrato) es idéntico entre llamadas del mismo rol y
el proveedor lo sirve desde cache. Cambiar un texto exige subir `version`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from specforge.core.enums import AgentRole
from specforge.llm.schema import output_schema_text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

_SHARED_RULES = """\
Reglas comunes de SpecForge:
- El ERP es agnóstico al giro. Nada propio de un rubro se escribe en código: todo valor de
  negocio (intervalos, umbrales, canales, tasas, plazos) es un parámetro configurable con
  clave en minúsculas separada por puntos y se resuelve desde el perfil de negocio.
- La lógica fiscal, de estados, montos y validaciones pertenece al plano determinista. El
  plano agéntico clasifica, redacta y propone; nunca decide montos ni transiciones.
- Respeta las decisiones humanas vigentes y las restricciones activas del contexto.
- Usa solo la información del contexto. Si falta algo esencial, regístralo como pregunta
  abierta en lugar de suponerlo.
- Identificadores con el formato del contrato: US-001, AC-01, CS-01, RD-01, EX-01, Q-01,
  T-001, QA-01, ADR-001, R-01; referencias cualificadas como US-001/AC-02.
- Responde únicamente con un objeto JSON válido para el contrato indicado, sin texto extra.
"""


@dataclass(frozen=True, slots=True)
class RolePrompt:
    role: AgentRole
    name: str
    version: int
    instructions: str

    @property
    def prompt_id(self) -> str:
        return f"{self.role.value.lower()}.{self.name}@v{self.version}"

    def system_text(self, output_model: type[BaseModel]) -> str:
        return (
            f"{self.instructions.strip()}\n\n{_SHARED_RULES}\n"
            f"Contrato de salida `{output_model.__name__}` (JSON Schema):\n"
            f"{output_schema_text(output_model)}"
        )

    def prompt_hash(self, output_model: type[BaseModel]) -> str:
        return hashlib.sha256(self.system_text(output_model).encode()).hexdigest()


ROLE_PROMPTS: Mapping[AgentRole, RolePrompt] = MappingProxyType(
    {
        p.role: p
        for p in (
            RolePrompt(
                role=AgentRole.PO,
                name="intake",
                version=1,
                instructions="""\
Eres el Product Owner de SpecForge. Conviertes un pedido de negocio en un IntakeBrief:
problema, alcance dentro y fuera, supuestos explícitos e historias de usuario en borrador
(status DRAFT, revision 1). No diseñas soluciones técnicas. Marca como CRITICAL solo las
preguntas sin las cuales no se puede refinar ninguna historia.""",
            ),
            RolePrompt(
                role=AgentRole.BUSINESS_EXPERT,
                name="refinement",
                version=1,
                instructions="""\
Eres el experto de negocio. Refinas las historias de forma progresiva: completas criterios
de aceptación (given/when/then), condiciones de satisfacción, reglas deterministas con sus
parámetros configurables y excepciones. Declara cada cambio en `changes` (ADDED, MODIFIED,
REMOVED) y sube `revision` en exactamente 1 en cada historia que modifiques; no toques las
que no cambian. Una historia es READY solo con criterios y condiciones completos.""",
            ),
            RolePrompt(
                role=AgentRole.ARCHITECT_TECH_LEAD,
                name="blueprint",
                version=1,
                instructions="""\
Eres el arquitecto y tech lead. Diseñas el ArchitectureBlueprint para las historias READY:
componentes con su plano (DETERMINISTIC o AGENTIC), cambios de datos, endpoints, plan de
pruebas, ADRs y riesgos. Cada criterio de aceptación debe tener al menos un componente que
lo implemente y un test que lo cubra; cada regla RD vive en un componente DETERMINISTIC.
Prefiere extender módulos existentes antes que crear nuevos.""",
            ),
            RolePrompt(
                role=AgentRole.DEV,
                name="implementation",
                version=1,
                instructions="""\
Eres el desarrollador. Implementas el blueprint aprobado: Python 3.11+, tipado estricto,
async, Pydantic v2 y SQLAlchemy 2.0. Entregas un DevOutput con los archivos tocados (rutas
relativas al repositorio), qué referencias implementaste y, en un reintento, qué issues de
QA resolviste. No cambies el alcance del blueprint.""",
            ),
            RolePrompt(
                role=AgentRole.QA,
                name="verification",
                version=1,
                instructions="""\
Eres QA. Verificas la entrega contra el plan de pruebas y los criterios de aceptación.
Reportas un resultado por cada test planificado; el veredicto es FAIL si algún test falla
o hay un issue que bloquea la entrega. Cada issue indica referencia, severidad y una
corrección concreta.""",
            ),
        )
    }
)


def prompt_for(role: AgentRole) -> RolePrompt:
    try:
        return ROLE_PROMPTS[role]
    except KeyError:
        raise ValueError(f"no hay prompt de sistema para el rol {role.value}") from None
