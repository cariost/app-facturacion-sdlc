# app-facturacion-sdlc · SpecForge

**SpecForge** (*Stateful Spec-Driven Agentic SDLC Engine*) es un orquestador multiagente
respaldado por PostgreSQL que funciona como una fábrica de software dirigida por estados
para construir un **ERP modular agnóstico al giro de negocio**, con Facturación Electrónica
(**SUNAT + SIRE**) y un **CRM configurable por rubro y tenant**.

## Principios

- **Plano determinista vs. plano agéntico.** El grafo de fases, el tope de reintentos, el
  versionado de blueprints, la idempotencia y la inmutabilidad de artefactos viven en
  PostgreSQL (CHECKs y triggers) y en código Python tipado, nunca en el LLM.
- **Agnóstico al giro.** Las políticas operativas (recordatorios por tiempo, uso o ciclo,
  citas, renovaciones, atención automatizada) son configuración en
  `business_profiles.config` por `tenant_id`. El mantenimiento periódico de un taller es
  solo un ejemplo de configuración.
- **Refinamiento progresivo.** El dominio se construye con historias de usuario, criterios
  de aceptación y condiciones de satisfacción versionadas como artefactos, sin requerir
  una especificación monolítica previa.
- **Memoria en 4 capas.** Estado del sistema (PostgreSQL), memoria de trabajo (State
  liviano de LangGraph con punteros), memoria episódica (resúmenes) y memoria semántica
  (Context Packs con pgvector).

## Estado de implementación

| Paso | Contenido | Estado |
|------|-----------|--------|
| 1 | Estructura, configuración y tooling | ✅ |
| 2 | Esquema SQL, capa de BD async y checkpointer de LangGraph | ✅ |
| 3 | Contratos Pydantic, `FeatureDeliveryState` y `TransitionGuard` | ✅ |
| 4 | `ContextBuilder`, `ModelRouter` y Context Packs iniciales | ✅ |
| 5 | Subagentes, `SpecForgeOrchestrator` y API FastAPI | ✅ |
| 6 | Pruebas end-to-end del caso CRM | ✅ |

## Estructura

```
src/specforge/
├── core/            config, enums, ids (feature_id / thread_id), grafo de fases, errores
├── db/
│   ├── migrations/  SQL versionado (fuente del DDL)
│   ├── models.py    ORM SQLAlchemy 2.0
│   ├── engine.py    Database async + traducción de errores SQLSTATE → dominio
│   ├── repositories/ features, artefactos, aprobaciones, logs, perfiles, contexto
│   ├── uow.py       UnitOfWork
│   ├── checkpointer.py  AsyncPostgresSaver
│   └── migrate.py   runner + CLI `specforge-db`
├── schemas/         contratos Pydantic, FeatureDeliveryState, registro de artefactos
├── context_packs/   packs Markdown versionados (CORE, CRM, SUNAT, SIRE + overlays de giro)
├── context/         ContextBuilder, specs por nodo, compactación del resumen, embeddings
├── llm/             ModelRouter, RoutingPolicy, prompts por rol, adaptador Anthropic, OTel
├── agents/          PO, BUSINESS_EXPERT, ARCHITECT_TECH_LEAD, DEV, QA
├── graph/           StateGraph, nodos idempotentes, HITL y SpecForgeOrchestrator
├── api/             FastAPI (crear, consultar, aprobar/pedir cambios, aclarar, reanudar)
tests/
├── unit/            sin dependencias externas
└── integration/     PostgreSQL real con una BD efímera por módulo de tests
```

## Convenciones

- `feature_id`: `<MODULO>-<NNN>` (`CRM-001`, `SUNAT-014`). La BD lo asigna de forma atómica
  por módulo (`modules.last_feature_seq`).
- `thread_id`: `sf:<feature_id>:v<blueprint_version>:<run_seq>` (`sf:CRM-001:v1:01`).
  Es una columna generada en `features.current_thread_id`.
- Fases: `INTAKE_PO → STORY_REFINEMENT → BLUEPRINT_DESIGN → HUMAN_GATE → IMPLEMENTATION →
  QA_VERIFICATION → COMPLETED`, más `BLOCKED` y `FAILED`.

### Reglas que hace cumplir la base de datos

| Regla | Mecanismo | Error de dominio |
|-------|-----------|------------------|
| Solo transiciones de `phase_transitions` | trigger `SF001` | `InvalidPhaseTransitionError` |
| `BLOCKED` solo vuelve a la fase previa o a `FAILED` | trigger `SF001` | `InvalidPhaseTransitionError` |
| QA → IMPLEMENTATION incrementa `retry_count` en 1 | trigger `SF002` | `RetryPolicyViolationError` |
| Máximo 3 ciclos Dev ↔ QA | CHECK `features_retry_cap` | `RetryBudgetExceededError` |
| `blueprint_version` +1 solo al pedir cambios en `HUMAN_GATE` | trigger `SF003` | `BlueprintVersionViolationError` |
| Features cerradas, artefactos y aprobaciones son inmutables | triggers `SF004` | `ImmutableRecordError` |
| `run_seq` solo avanza de uno en uno | trigger `SF005` | `RunSequenceViolationError` |
| `executive_summary` ≤ 15 líneas | CHECK `features_summary_compact` | `IntegrityViolationError` |
| Concurrencia | lock optimista `row_version` | `ConcurrencyConflictError` |

## Puesta en marcha

Requisitos: Python 3.11+ y Docker.

```bash
python3.11 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

```bash
docker compose up -d
```

```bash
.venv/bin/specforge-db migrate --with-checkpointer
```

```bash
.venv/bin/specforge-db status
```

Publicar los Context Packs y sus embeddings (idempotente: solo crea versión si el contenido
cambió):

```bash
.venv/bin/specforge-db seed-packs
```

Para llamadas reales a modelos (Anthropic) instala el extra `llm` y define
`ANTHROPIC_API_KEY`. Los modelos por tier se configuran con `SPECFORGE_MODEL_FAST`
(por defecto `claude-haiku-5-5`) y `SPECFORGE_MODEL_REASONING` (por defecto
`claude-opus-5-5`).

```bash
.venv/bin/pip install -e ".[dev,llm]"
```

## Orquestación y API

Arranque (requiere `migrate --with-checkpointer`, `seed-packs` y `ANTHROPIC_API_KEY`):

```bash
.venv/bin/uvicorn specforge.api.app:create_app --factory --reload
```

| Método | Ruta | Uso |
|--------|------|-----|
| POST | `/features` | crea la feature y la ejecuta en segundo plano (`start=false` para no iniciarla) |
| GET | `/features/{id}` | fase, estado (`PAUSED`, `BLOCKED`…), pausa pendiente, artefactos, aprobaciones, consumo por tier |
| GET | `/features/{id}/artifacts[/{artifact_id}]` | artefactos versionados (filtro `artifact_type`) y su contenido |
| GET | `/features/{id}/history` | transiciones con su `thread_id` |
| POST | `/features/{id}/approval` | HUMAN_GATE: `APPROVED`, `CHANGES_REQUESTED` (`target`: `BLUEPRINT` o `STORIES`) o `REJECTED` |
| POST | `/features/{id}/clarification` | respuestas a preguntas críticas (`answers`) o `reject` |
| POST | `/features/{id}/resume` | reanuda una feature `BLOCKED` (nuevo `run_seq` → nuevo hilo) |

- Cada nodo del grafo relee la feature de PostgreSQL y no hace nada si la fase ya avanzó,
  así que reanudar o reintentar tras una caída no duplica efectos.
- Artefacto + resumen ejecutivo + transición se escriben en una sola transacción; la
  transición la decide el `TransitionGuard` y la valida el trigger.
- Pausas: `interrupt` en HUMAN_GATE y en aclaraciones (preguntas `CRITICAL` del PO o del
  experto); se reanudan con `Command(resume=...)` y la decisión queda en
  `feature_approvals` (clave de idempotencia = `interrupt_id`).
- Pedir cambios en HUMAN_GATE sube `blueprint_version`: el hilo actual termina y el
  orquestador abre `sf:<id>:v<bp+1>:01`. Reanudar desde `BLOCKED` abre `…:<run+1>`.
- Una falla del modelo o de contexto bloquea la feature con el motivo; los errores de QA se
  guardan como memoria episódica (pgvector) y en `qa_errors` para el reintento de DEV.

## Contexto y ruteo de modelos

- **Contexto mínimo por nodo** (`context/specs.py`): cada nodo declara qué artefactos,
  packs y presupuesto de tokens usa. Lo obligatorio debe entrar; lo opcional entra por
  prioridad y lo descartado queda en `ContextBundle.dropped`.
- **Packs**: el pack raíz del módulo y `CORE` entran completos; los secundarios
  (`CRM.reminders`, `CRM.inquiries`…) se eligen por relevancia con pgvector; los overlays
  de giro (`industry`) solo se cargan para perfiles de ese giro.
- **Cache de prompts**: sistema (prompt del rol + contrato) → contexto estable (packs,
  perfil) → contexto volátil (artefactos, feedback humano, pedido, estado) → tarea.
- **Ruteo**: PO y QA usan FAST; BUSINESS_EXPERT, ARCHITECT_TECH_LEAD y DEV usan REASONING.
  Lo FAST se escala a REASONING ante una reparación de salida inválida, contexto grande o
  reintento Dev ↔ QA. Cada llamada registra modelo, motivo de ruteo, tokens de entrada,
  salida y cache, latencia y outcome en `agent_execution_logs`.
- **`executive_summary`**: cabecera regenerada + eventos recientes, ≤ 15 líneas; los
  eventos antiguos se pliegan en un marcador (el detalle queda en la BD).

## Calidad y pruebas

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

Tests unitarios (sin BD):

```bash
.venv/bin/pytest tests/unit
```

Tests de integración: crean una BD `specforge_test_<random>`, aplican migraciones y la
eliminan al terminar.

```bash
SPECFORGE_TEST_DATABASE_URL=postgresql://specforge:specforge@localhost:5433/postgres .venv/bin/pytest
```

### End-to-end del caso CRM

`tests/integration/test_e2e_crm.py` (marcador `e2e`) recorre el ciclo completo por la API
HTTP sobre PostgreSQL, LangGraph (`AsyncPostgresSaver`), pgvector, `ModelRouter` y los
agentes reales. Solo el proveedor LLM está guionado (`tests/fakes.py`), y cada respuesta
pasa por Pydantic, los validadores de cada agente y el `TransitionGuard`.

- **Ciclo completo:**
  - Refinamiento progresivo en dos rondas (US-001 rev 1 → 2 → 3, US-002 agregada).
  - HUMAN_GATE v1 pide cambios a las historias, lo que abre blueprint v2 en el hilo `v2:01` con el feedback humano en el contexto.
  - HUMAN_GATE v2 se aprueba.
  - QA falla una vez (QA-01). En el reintento el Dev recibe los errores de la memoria episódica, una respuesta inválida pasa por el loop de reparación y el segundo QA da PASS (COMPLETED).
  - Se verifican artefactos versionados, approvals, historial de fases, bitácora y ruteo FAST/REASONING, memoria episódica recuperable y el resumen ejecutivo acotado.
- **Presupuesto de reintentos agotado:** pasa a FAILED sin un intento extra.
- **Agnosticismo de rubro:** un tenant `imprenta` usa su propia configuración (contador `copias`) y no recibe el overlay `CRM.examples.automotive`.

```bash
SPECFORGE_TEST_DATABASE_URL=postgresql://specforge:specforge@localhost:5433/postgres .venv/bin/pytest -m e2e -v
```

## Uso de la capa de datos

```python
from specforge.core.config import get_settings
from specforge.core.enums import Phase
from specforge.db import Database, unit_of_work

db = Database(get_settings())

async with unit_of_work(db) as uow:
    feature = await uow.features.create(
        module="CRM",
        tenant_id="acme",
        business_profile="taller-automotriz",
        title="Aviso de mantenimiento periódico",
        request="...",
    )

async with unit_of_work(db) as uow:
    feature = await uow.features.transition(
        feature.feature_id,
        to_phase=Phase.STORY_REFINEMENT,
        expected_row_version=feature.row_version,
        reason="intake completo",
    )
```
