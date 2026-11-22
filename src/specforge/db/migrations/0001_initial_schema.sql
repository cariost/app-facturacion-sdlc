-- =============================================================================
-- SpecForge · 0001_initial_schema
-- Fuente de verdad del ciclo de vida de features. Las reglas deterministas
-- (formato de IDs, grafo de fases, tope de reintentos, versionado de blueprint,
-- inmutabilidad de artefactos) viven aquí y NO dependen del LLM.
--
-- SQLSTATE propios (mapeados en specforge.core.errors):
--   SF001 transición de fase inválida
--   SF002 violación de política de reintentos
--   SF003 violación de versionado de blueprint
--   SF004 registro inmutable / feature cerrada
--   SF005 run_seq no monotónico
-- Requiere PostgreSQL >= 14 con la extensión pgvector.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- Utilidades
-- -----------------------------------------------------------------------------
CREATE FUNCTION sf_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- Bloquea UPDATE y DELETE directos; permite DELETE en cascada desde `features`
-- (en cascada el trigger corre anidado en el trigger RI, depth > 1).
CREATE FUNCTION sf_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% es append-only: % no permitido', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'SF004';
END
$$;

-- -----------------------------------------------------------------------------
-- Catálogos
-- -----------------------------------------------------------------------------
CREATE TABLE modules (
    code              text PRIMARY KEY CONSTRAINT modules_code_format
                          CHECK (code ~ '^[A-Z][A-Z0-9]{1,15}$'),
    name              text NOT NULL,
    description       text NOT NULL DEFAULT '',
    last_feature_seq  integer NOT NULL DEFAULT 0 CHECK (last_feature_seq >= 0),
    created_at        timestamptz NOT NULL DEFAULT now()
);

INSERT INTO modules (code, name, description) VALUES
    ('CORE',  'Núcleo ERP',             'Maestros, multi-tenant, seguridad y servicios transversales agnósticos al giro.'),
    ('CRM',   'CRM configurable',       'Clientes, activos, recordatorios por tiempo/uso/ciclo y atención automatizada parametrizable por giro y tenant.'),
    ('SUNAT', 'Facturación Electrónica','Emisión de comprobantes electrónicos (UBL 2.1), CDR y resúmenes según normativa SUNAT.'),
    ('SIRE',  'SIRE',                   'Sistema Integrado de Registros Electrónicos: registros de ventas y compras.');

CREATE TABLE phases (
    code         text PRIMARY KEY,
    ordinal      smallint NOT NULL UNIQUE,
    is_terminal  boolean NOT NULL DEFAULT false
);

INSERT INTO phases (code, ordinal, is_terminal) VALUES
    ('INTAKE_PO',         10, false),
    ('STORY_REFINEMENT',  20, false),
    ('BLUEPRINT_DESIGN',  30, false),
    ('HUMAN_GATE',        40, false),
    ('IMPLEMENTATION',    50, false),
    ('QA_VERIFICATION',   60, false),
    ('COMPLETED',         70, true),
    ('BLOCKED',           80, false),
    ('FAILED',            90, true);

-- Grafo de transiciones permitido (espejo de specforge.core.phases.ALLOWED_TRANSITIONS).
CREATE TABLE phase_transitions (
    from_phase  text NOT NULL REFERENCES phases (code),
    to_phase    text NOT NULL REFERENCES phases (code),
    PRIMARY KEY (from_phase, to_phase),
    CHECK (from_phase <> to_phase)
);

INSERT INTO phase_transitions (from_phase, to_phase) VALUES
    ('INTAKE_PO',        'STORY_REFINEMENT'),
    ('INTAKE_PO',        'BLOCKED'),
    ('INTAKE_PO',        'FAILED'),
    ('STORY_REFINEMENT', 'BLUEPRINT_DESIGN'),
    ('STORY_REFINEMENT', 'BLOCKED'),
    ('STORY_REFINEMENT', 'FAILED'),
    ('BLUEPRINT_DESIGN', 'HUMAN_GATE'),
    ('BLUEPRINT_DESIGN', 'BLOCKED'),
    ('BLUEPRINT_DESIGN', 'FAILED'),
    ('HUMAN_GATE',       'IMPLEMENTATION'),
    ('HUMAN_GATE',       'BLUEPRINT_DESIGN'),
    ('HUMAN_GATE',       'STORY_REFINEMENT'),
    ('HUMAN_GATE',       'BLOCKED'),
    ('HUMAN_GATE',       'FAILED'),
    ('IMPLEMENTATION',   'QA_VERIFICATION'),
    ('IMPLEMENTATION',   'BLOCKED'),
    ('IMPLEMENTATION',   'FAILED'),
    ('QA_VERIFICATION',  'COMPLETED'),
    ('QA_VERIFICATION',  'IMPLEMENTATION'),
    ('QA_VERIFICATION',  'BLOCKED'),
    ('QA_VERIFICATION',  'FAILED'),
    ('BLOCKED',          'INTAKE_PO'),
    ('BLOCKED',          'STORY_REFINEMENT'),
    ('BLOCKED',          'BLUEPRINT_DESIGN'),
    ('BLOCKED',          'HUMAN_GATE'),
    ('BLOCKED',          'IMPLEMENTATION'),
    ('BLOCKED',          'QA_VERIFICATION'),
    ('BLOCKED',          'FAILED');

-- -----------------------------------------------------------------------------
-- Perfiles de negocio (giro) por tenant
-- Las políticas operativas (recordatorios por tiempo/uso, citas, renovaciones,
-- atención automatizada) se parametrizan en `config`, nunca en el código.
-- -----------------------------------------------------------------------------
CREATE TABLE business_profiles (
    tenant_id       text NOT NULL CONSTRAINT business_profiles_tenant_format
                        CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'),
    code            text NOT NULL CONSTRAINT business_profiles_code_format
                        CHECK (code ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    display_name    text NOT NULL CHECK (length(btrim(display_name)) > 0),
    industry        text NOT NULL CONSTRAINT business_profiles_industry_format
                        CHECK (industry ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    config          jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(config) = 'object'),
    config_version  integer NOT NULL DEFAULT 1 CHECK (config_version >= 1),
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, code)
);

CREATE INDEX business_profiles_industry_idx ON business_profiles (industry);

CREATE TRIGGER business_profiles_touch
    BEFORE UPDATE ON business_profiles
    FOR EACH ROW EXECUTE FUNCTION sf_touch_updated_at();

-- -----------------------------------------------------------------------------
-- features: estado del sistema por feature
-- -----------------------------------------------------------------------------
CREATE TABLE features (
    feature_id              text PRIMARY KEY,
    module                  text NOT NULL REFERENCES modules (code),
    tenant_id               text NOT NULL,
    business_profile        text,
    title                   text NOT NULL CHECK (length(btrim(title)) > 0),
    request                 text NOT NULL CHECK (length(btrim(request)) > 0),

    phase                   text NOT NULL DEFAULT 'INTAKE_PO' REFERENCES phases (code),
    phase_entered_at        timestamptz NOT NULL DEFAULT now(),
    last_transition_reason  text,
    blocked_from_phase      text REFERENCES phases (code),
    blocked_reason          text,

    blueprint_version       integer NOT NULL DEFAULT 1 CHECK (blueprint_version >= 1),
    run_seq                 integer NOT NULL DEFAULT 1 CHECK (run_seq >= 1),
    current_thread_id       text GENERATED ALWAYS AS (
                                'sf:' || feature_id
                                || ':v' || blueprint_version::text
                                || ':' || CASE WHEN run_seq < 10 THEN '0' || run_seq::text
                                               ELSE run_seq::text END
                            ) STORED,
    retry_count             integer NOT NULL DEFAULT 0,
    max_retries             integer NOT NULL DEFAULT 3 CHECK (max_retries BETWEEN 0 AND 3),

    -- Memoria de trabajo compacta (no historiales crudos).
    executive_summary       text NOT NULL DEFAULT '',
    active_constraints      jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,

    row_version             integer NOT NULL DEFAULT 1,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,

    CONSTRAINT features_id_format
        CHECK (feature_id ~ '^[A-Z][A-Z0-9]{1,15}-[0-9]{3,}$'),
    CONSTRAINT features_id_module
        CHECK (split_part(feature_id, '-', 1) = module),
    CONSTRAINT features_retry_cap
        CHECK (retry_count BETWEEN 0 AND max_retries),
    CONSTRAINT features_blocked_consistency
        CHECK ((phase = 'BLOCKED') = (blocked_from_phase IS NOT NULL)
               AND (phase <> 'BLOCKED' OR blocked_reason IS NOT NULL)
               AND (blocked_from_phase IS NULL
                    OR blocked_from_phase NOT IN ('BLOCKED', 'COMPLETED', 'FAILED'))),
    CONSTRAINT features_closed_consistency
        CHECK ((phase IN ('COMPLETED', 'FAILED')) = (closed_at IS NOT NULL)),
    CONSTRAINT features_summary_compact
        CHECK (length(executive_summary) <= 4000
               AND coalesce(array_length(string_to_array(executive_summary, E'\n'), 1), 0) <= 15),
    CONSTRAINT features_constraints_array
        CHECK (jsonb_typeof(active_constraints) = 'array'),
    CONSTRAINT features_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT features_business_profile_fk
        FOREIGN KEY (tenant_id, business_profile)
        REFERENCES business_profiles (tenant_id, code)
);

CREATE UNIQUE INDEX features_current_thread_uq ON features (current_thread_id);
CREATE INDEX features_tenant_phase_idx ON features (tenant_id, phase);
CREATE INDEX features_module_phase_idx ON features (module, phase);
CREATE INDEX features_open_idx ON features (updated_at DESC) WHERE closed_at IS NULL;

CREATE TABLE feature_phase_history (
    history_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feature_id         text NOT NULL REFERENCES features (feature_id) ON DELETE CASCADE,
    from_phase         text REFERENCES phases (code),
    to_phase           text NOT NULL REFERENCES phases (code),
    blueprint_version  integer NOT NULL,
    retry_count        integer NOT NULL,
    thread_id          text NOT NULL,
    reason             text,
    changed_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX feature_phase_history_feature_idx
    ON feature_phase_history (feature_id, history_id);

-- Toda feature nace en INTAKE_PO, v1, run 01, sin reintentos.
CREATE FUNCTION sf_guard_feature_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.phase <> 'INTAKE_PO' OR NEW.blueprint_version <> 1 OR NEW.run_seq <> 1
       OR NEW.retry_count <> 0 OR NEW.row_version <> 1 THEN
        RAISE EXCEPTION 'feature % debe crearse en INTAKE_PO v1 run 01 sin reintentos',
            NEW.feature_id USING ERRCODE = 'SF001';
    END IF;
    NEW.blocked_from_phase := NULL;
    NEW.blocked_reason := NULL;
    NEW.closed_at := NULL;
    NEW.phase_entered_at := now();
    RETURN NEW;
END
$$;

CREATE FUNCTION sf_guard_feature_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.phase IN ('COMPLETED', 'FAILED') THEN
        RAISE EXCEPTION 'feature % está cerrada (%)', OLD.feature_id, OLD.phase
            USING ERRCODE = 'SF004';
    END IF;

    IF NEW.feature_id <> OLD.feature_id OR NEW.module <> OLD.module
       OR NEW.tenant_id <> OLD.tenant_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'la identidad de la feature % es inmutable', OLD.feature_id
            USING ERRCODE = 'SF004';
    END IF;

    -- Política de reintentos Dev <-> QA.
    IF OLD.phase = 'QA_VERIFICATION' AND NEW.phase = 'IMPLEMENTATION' THEN
        IF NEW.retry_count <> OLD.retry_count + 1 THEN
            RAISE EXCEPTION 'QA -> IMPLEMENTATION exige retry_count = % (recibido %)',
                OLD.retry_count + 1, NEW.retry_count USING ERRCODE = 'SF002';
        END IF;
    ELSIF NEW.retry_count <> OLD.retry_count
          AND NEW.blueprint_version = OLD.blueprint_version THEN
        RAISE EXCEPTION 'retry_count solo cambia en QA_VERIFICATION -> IMPLEMENTATION'
            USING ERRCODE = 'SF002';
    END IF;

    -- Versionado del blueprint: solo +1 y solo al pedir cambios en HUMAN_GATE.
    IF NEW.blueprint_version <> OLD.blueprint_version THEN
        IF NEW.blueprint_version <> OLD.blueprint_version + 1 THEN
            RAISE EXCEPTION 'blueprint_version debe incrementarse de uno en uno (% -> %)',
                OLD.blueprint_version, NEW.blueprint_version USING ERRCODE = 'SF003';
        END IF;
        IF NOT (OLD.phase = 'HUMAN_GATE'
                AND NEW.phase IN ('BLUEPRINT_DESIGN', 'STORY_REFINEMENT')) THEN
            RAISE EXCEPTION 'blueprint_version solo cambia al solicitar cambios en HUMAN_GATE'
                USING ERRCODE = 'SF003';
        END IF;
        NEW.retry_count := 0;
        NEW.run_seq := 1;
    ELSIF OLD.phase = 'HUMAN_GATE' AND NEW.phase IN ('BLUEPRINT_DESIGN', 'STORY_REFINEMENT') THEN
        RAISE EXCEPTION 'HUMAN_GATE -> % exige una nueva blueprint_version', NEW.phase
            USING ERRCODE = 'SF003';
    ELSIF NEW.run_seq NOT IN (OLD.run_seq, OLD.run_seq + 1) THEN
        RAISE EXCEPTION 'run_seq solo puede avanzar de uno en uno (% -> %)',
            OLD.run_seq, NEW.run_seq USING ERRCODE = 'SF005';
    END IF;

    -- Grafo de fases.
    IF NEW.phase <> OLD.phase THEN
        IF NOT EXISTS (SELECT 1 FROM phase_transitions t
                       WHERE t.from_phase = OLD.phase AND t.to_phase = NEW.phase) THEN
            RAISE EXCEPTION 'transición inválida % -> % para %',
                OLD.phase, NEW.phase, OLD.feature_id USING ERRCODE = 'SF001';
        END IF;
        IF OLD.phase = 'BLOCKED' AND NEW.phase <> 'FAILED'
           AND NEW.phase <> OLD.blocked_from_phase THEN
            RAISE EXCEPTION 'feature % bloqueada solo puede reanudarse en % (o FAILED)',
                OLD.feature_id, OLD.blocked_from_phase USING ERRCODE = 'SF001';
        END IF;
        IF NEW.phase = 'BLOCKED' THEN
            NEW.blocked_from_phase := OLD.phase;
        ELSE
            NEW.blocked_from_phase := NULL;
            NEW.blocked_reason := NULL;
        END IF;
        NEW.closed_at := CASE WHEN NEW.phase IN ('COMPLETED', 'FAILED') THEN now() END;
        NEW.phase_entered_at := now();
    ELSE
        NEW.blocked_from_phase := OLD.blocked_from_phase;
        NEW.last_transition_reason := OLD.last_transition_reason;
    END IF;

    NEW.row_version := OLD.row_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

CREATE FUNCTION sf_record_phase_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.phase <> OLD.phase THEN
        INSERT INTO feature_phase_history
            (feature_id, from_phase, to_phase, blueprint_version, retry_count, thread_id, reason)
        VALUES
            (NEW.feature_id,
             CASE WHEN TG_OP = 'UPDATE' THEN OLD.phase END,
             NEW.phase, NEW.blueprint_version, NEW.retry_count,
             NEW.current_thread_id, NEW.last_transition_reason);
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER features_guard_insert
    BEFORE INSERT ON features
    FOR EACH ROW EXECUTE FUNCTION sf_guard_feature_insert();

CREATE TRIGGER features_guard_update
    BEFORE UPDATE ON features
    FOR EACH ROW EXECUTE FUNCTION sf_guard_feature_update();

CREATE TRIGGER features_phase_history
    AFTER INSERT OR UPDATE ON features
    FOR EACH ROW EXECUTE FUNCTION sf_record_phase_history();

-- -----------------------------------------------------------------------------
-- agent_execution_logs: trazabilidad, tokens y latencia por nodo/agente
-- -----------------------------------------------------------------------------
CREATE TABLE agent_execution_logs (
    log_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_id          text NOT NULL REFERENCES features (feature_id) ON DELETE CASCADE,
    tenant_id           text NOT NULL,
    thread_id           text NOT NULL,
    node_name           text NOT NULL,
    agent_role          text NOT NULL CHECK (agent_role IN
                            ('PO', 'BUSINESS_EXPERT', 'ARCHITECT_TECH_LEAD', 'DEV', 'QA',
                             'HUMAN', 'SYSTEM')),
    phase               text NOT NULL REFERENCES phases (code),
    attempt             integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),

    model_provider      text,
    model_name          text,
    model_tier          text CHECK (model_tier IN ('FAST', 'REASONING')),
    prompt_id           text,
    prompt_hash         text,
    context_pack_refs   text[] NOT NULL DEFAULT '{}',
    input_artifact_ids  uuid[] NOT NULL DEFAULT '{}',

    input_tokens        integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens       integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cached_tokens       integer NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    latency_ms          integer CHECK (latency_ms >= 0),

    outcome             text NOT NULL DEFAULT 'RUNNING' CHECK (outcome IN
                            ('RUNNING', 'SUCCESS', 'RETRY', 'VALIDATION_ERROR', 'LLM_ERROR',
                             'INTERRUPTED', 'SKIPPED')),
    error_type          text,
    error_message       text,

    trace_id            text,
    span_id             text,
    langsmith_run_id    text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(metadata) = 'object'),

    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,

    CONSTRAINT agent_logs_finish_consistency
        CHECK ((outcome = 'RUNNING') = (finished_at IS NULL)),
    CONSTRAINT agent_logs_finish_order
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT agent_logs_model_tier
        CHECK (model_name IS NULL OR model_tier IS NOT NULL)
);

CREATE INDEX agent_logs_feature_idx ON agent_execution_logs (feature_id, started_at DESC);
CREATE INDEX agent_logs_thread_idx ON agent_execution_logs (thread_id, started_at);
CREATE INDEX agent_logs_cost_idx ON agent_execution_logs (tenant_id, model_name, started_at);
CREATE INDEX agent_logs_running_idx ON agent_execution_logs (started_at) WHERE outcome = 'RUNNING';

-- Solo se puede cerrar un log en curso; los cerrados son inmutables.
CREATE FUNCTION sf_guard_log_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.outcome <> 'RUNNING' THEN
        RAISE EXCEPTION 'agent_execution_logs % ya fue cerrado', OLD.log_id
            USING ERRCODE = 'SF004';
    END IF;
    IF NEW.log_id <> OLD.log_id OR NEW.feature_id <> OLD.feature_id
       OR NEW.thread_id <> OLD.thread_id OR NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION 'identidad del log inmutable' USING ERRCODE = 'SF004';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agent_logs_guard_update
    BEFORE UPDATE ON agent_execution_logs
    FOR EACH ROW EXECUTE FUNCTION sf_guard_log_update();

-- -----------------------------------------------------------------------------
-- feature_artifacts: artefactos versionados y append-only (contratos Pydantic)
-- -----------------------------------------------------------------------------
CREATE TABLE feature_artifacts (
    artifact_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_id              text NOT NULL REFERENCES features (feature_id) ON DELETE CASCADE,
    artifact_type           text NOT NULL CHECK (artifact_type IN
                                ('INTAKE_BRIEF', 'USER_STORY_SET', 'STORY_REFINEMENT',
                                 'ARCHITECTURE_BLUEPRINT', 'DEV_OUTPUT', 'QA_REPORT',
                                 'EPISODIC_SUMMARY', 'ADR')),
    version                 integer NOT NULL CHECK (version >= 1),
    blueprint_version       integer NOT NULL CHECK (blueprint_version >= 1),
    phase                   text NOT NULL REFERENCES phases (code),
    produced_by             text NOT NULL CHECK (produced_by IN
                                ('PO', 'BUSINESS_EXPERT', 'ARCHITECT_TECH_LEAD', 'DEV', 'QA',
                                 'HUMAN', 'SYSTEM')),
    schema_name             text NOT NULL,
    schema_version          text NOT NULL DEFAULT '1',
    content                 jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    content_hash            text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    summary                 text NOT NULL DEFAULT '',
    supersedes_artifact_id  uuid REFERENCES feature_artifacts (artifact_id),
    thread_id               text NOT NULL,
    execution_log_id        uuid REFERENCES agent_execution_logs (log_id) ON DELETE SET NULL,
    idempotency_key         text UNIQUE,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_artifacts_version_uq UNIQUE (feature_id, artifact_type, version)
);

CREATE INDEX feature_artifacts_blueprint_idx
    ON feature_artifacts (feature_id, blueprint_version);

-- Asigna versión secuencial y `supersedes` bajo lock de la feature (sin carreras).
CREATE FUNCTION sf_prepare_artifact() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_phase        text;
    v_prev_id      uuid;
    v_prev_version integer;
BEGIN
    SELECT phase INTO v_phase FROM features WHERE feature_id = NEW.feature_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NEW;  -- la FK reportará el error
    END IF;
    IF v_phase IN ('COMPLETED', 'FAILED') THEN
        RAISE EXCEPTION 'feature % está cerrada; no admite artefactos', NEW.feature_id
            USING ERRCODE = 'SF004';
    END IF;

    SELECT artifact_id, version INTO v_prev_id, v_prev_version
    FROM feature_artifacts
    WHERE feature_id = NEW.feature_id AND artifact_type = NEW.artifact_type
    ORDER BY version DESC
    LIMIT 1;

    NEW.version := coalesce(v_prev_version, 0) + 1;
    IF NEW.supersedes_artifact_id IS NULL THEN
        NEW.supersedes_artifact_id := v_prev_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER feature_artifacts_prepare
    BEFORE INSERT ON feature_artifacts
    FOR EACH ROW EXECUTE FUNCTION sf_prepare_artifact();

CREATE TRIGGER feature_artifacts_append_only
    BEFORE UPDATE OR DELETE ON feature_artifacts
    FOR EACH ROW EXECUTE FUNCTION sf_forbid_mutation();

-- -----------------------------------------------------------------------------
-- feature_approvals: decisiones Human-in-the-Loop (append-only)
-- -----------------------------------------------------------------------------
CREATE TABLE feature_approvals (
    approval_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_id         text NOT NULL REFERENCES features (feature_id) ON DELETE CASCADE,
    gate               text NOT NULL CHECK (gate IN
                           ('INTAKE_CLARIFICATION', 'STORY_CLARIFICATION', 'BLUEPRINT_APPROVAL')),
    decision           text NOT NULL CHECK (decision IN
                           ('APPROVED', 'CHANGES_REQUESTED', 'REJECTED', 'CLARIFIED')),
    phase              text NOT NULL REFERENCES phases (code),
    blueprint_version  integer NOT NULL CHECK (blueprint_version >= 1),
    artifact_id        uuid REFERENCES feature_artifacts (artifact_id),
    reviewer           text NOT NULL CHECK (length(btrim(reviewer)) > 0),
    comments           text NOT NULL DEFAULT '',
    payload            jsonb NOT NULL DEFAULT '{}'::jsonb
                           CHECK (jsonb_typeof(payload) = 'object'),
    thread_id          text NOT NULL,
    interrupt_id       text,
    idempotency_key    text UNIQUE,
    decided_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT feature_approvals_blueprint_decision
        CHECK (gate <> 'BLUEPRINT_APPROVAL'
               OR (decision IN ('APPROVED', 'CHANGES_REQUESTED', 'REJECTED')
                   AND artifact_id IS NOT NULL)),
    CONSTRAINT feature_approvals_clarification_decision
        CHECK (gate = 'BLUEPRINT_APPROVAL' OR decision IN ('CLARIFIED', 'REJECTED')),
    CONSTRAINT feature_approvals_changes_need_detail
        CHECK (decision <> 'CHANGES_REQUESTED'
               OR length(btrim(comments)) > 0 OR payload <> '{}'::jsonb)
);

CREATE INDEX feature_approvals_feature_idx ON feature_approvals (feature_id, decided_at DESC);
CREATE UNIQUE INDEX feature_approvals_interrupt_uq
    ON feature_approvals (feature_id, interrupt_id) WHERE interrupt_id IS NOT NULL;

CREATE TRIGGER feature_approvals_append_only
    BEFORE UPDATE OR DELETE ON feature_approvals
    FOR EACH ROW EXECUTE FUNCTION sf_forbid_mutation();

-- -----------------------------------------------------------------------------
-- Memoria semántica/normativa: Context Packs versionados + chunks con pgvector
-- -----------------------------------------------------------------------------
CREATE TABLE context_packs (
    pack_code       text NOT NULL CONSTRAINT context_packs_code_format
                        CHECK (pack_code ~ '^[A-Z][A-Z0-9]{1,15}(\.[a-z0-9_]+)*$'),
    version         integer NOT NULL CHECK (version >= 1),
    module          text NOT NULL REFERENCES modules (code),
    industry        text CHECK (industry ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),  -- NULL = agnóstico
    title           text NOT NULL,
    content         text NOT NULL,
    content_hash    text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    token_estimate  integer NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
    is_active       boolean NOT NULL DEFAULT true,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pack_code, version),
    CONSTRAINT context_packs_code_module CHECK (split_part(pack_code, '.', 1) = module)
);

CREATE UNIQUE INDEX context_packs_one_active_uq ON context_packs (pack_code) WHERE is_active;
CREATE INDEX context_packs_module_idx ON context_packs (module, industry) WHERE is_active;

CREATE TABLE context_chunks (
    chunk_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind      text NOT NULL CHECK (source_kind IN
                         ('CONTEXT_PACK', 'ADR', 'BLUEPRINT', 'EPISODIC')),
    pack_code        text,
    pack_version     integer,
    artifact_id      uuid REFERENCES feature_artifacts (artifact_id) ON DELETE CASCADE,
    feature_id       text REFERENCES features (feature_id) ON DELETE CASCADE,
    module           text NOT NULL REFERENCES modules (code),
    tenant_id        text,      -- NULL = conocimiento compartido entre tenants
    industry         text,      -- NULL = agnóstico al giro
    chunk_index      integer NOT NULL CHECK (chunk_index >= 0),
    content          text NOT NULL CHECK (length(content) > 0),
    token_count      integer NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    embedding        vector(1536),
    embedding_model  text,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT context_chunks_pack_fk
        FOREIGN KEY (pack_code, pack_version)
        REFERENCES context_packs (pack_code, version) ON DELETE CASCADE,
    CONSTRAINT context_chunks_pack_source
        CHECK ((source_kind = 'CONTEXT_PACK')
               = (pack_code IS NOT NULL AND pack_version IS NOT NULL)),
    CONSTRAINT context_chunks_artifact_source
        CHECK (source_kind NOT IN ('ADR', 'BLUEPRINT') OR artifact_id IS NOT NULL),
    CONSTRAINT context_chunks_episodic_source
        CHECK (source_kind <> 'EPISODIC' OR feature_id IS NOT NULL),
    CONSTRAINT context_chunks_embedding_model
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);

CREATE UNIQUE INDEX context_chunks_pack_uq
    ON context_chunks (pack_code, pack_version, chunk_index) WHERE pack_code IS NOT NULL;
CREATE UNIQUE INDEX context_chunks_artifact_uq
    ON context_chunks (artifact_id, chunk_index) WHERE artifact_id IS NOT NULL;
CREATE INDEX context_chunks_scope_idx ON context_chunks (module, industry, tenant_id);
CREATE INDEX context_chunks_feature_idx ON context_chunks (feature_id) WHERE feature_id IS NOT NULL;
CREATE INDEX context_chunks_embedding_hnsw
    ON context_chunks USING hnsw (embedding vector_cosine_ops);
