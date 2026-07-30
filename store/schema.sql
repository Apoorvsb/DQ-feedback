-- Source of truth for feedback. Chroma is a derived index over this.

CREATE TABLE IF NOT EXISTS feedback_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    signature            TEXT    NOT NULL,   -- canonical, see feedback/signature.py
    entity_type          TEXT    NOT NULL CHECK (entity_type IN ('rule','selfheal','anomaly')),
    table_name           TEXT    NOT NULL,
    columns_json         TEXT    NOT NULL,
    rule_type            TEXT    NOT NULL,

    -- 'add' means "you failed to emit this rule at all". It is the only action
    -- whose signature is expected to be ABSENT from the run under review.
    action               TEXT    NOT NULL CHECK (action IN ('reject','correct','confirm','add')),
    raw_comment          TEXT    NOT NULL,   -- exactly what the user typed; never injected
    normalized_directive TEXT,               -- imperative form; THIS is what gets injected/embedded
    corrected_expression TEXT,

    validation_status    TEXT    NOT NULL CHECK (validation_status IN
                                  ('valid','needs_clarification','rejected')),
    validation_reason    TEXT,
    validation_detail    TEXT,               -- JSON: per-gate scores, for the demo's --explain
    confidence           REAL,

    status               TEXT    NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active','superseded')),
    superseded_by        INTEGER REFERENCES feedback_log(id),

    -- Anomaly-only. Rules are time-invariant; anomalies are not, so dismissing
    -- one must not blind you to the same anomaly at a worse magnitude later.
    -- suppression_bound is a PERCENT (0-100): "do not alert below this".
    suppression_bound    REAL,
    -- Anomaly feedback decays because distributions drift. NULL = never expires
    -- (rules and self-heal). See config.ANOMALY_FEEDBACK_TTL_DAYS.
    expires_at           TEXT,
    -- How many times this same anomaly has been dismissed. Repeated dismissal
    -- is itself a signal that the detector is miscalibrated.
    dismiss_count        INTEGER NOT NULL DEFAULT 1,

    iteration            INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Only valid+active rows are ever injected or embedded.
CREATE INDEX IF NOT EXISTS idx_feedback_live
    ON feedback_log (signature, validation_status, status);
CREATE INDEX IF NOT EXISTS idx_feedback_entity
    ON feedback_log (entity_type, table_name);

-- Every submission that never made it into feedback_log, with the gate that killed it.
-- This table is the demo evidence that "hygyt" is caught.
CREATE TABLE IF NOT EXISTS validation_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signature     TEXT,
    entity_type   TEXT,
    table_name    TEXT,
    raw_comment   TEXT NOT NULL,
    gate_failed   TEXT NOT NULL,   -- G0_COHERENCE | G1_STRUCTURAL | G2_SEMANTIC | G3_CONSISTENCY
    reason        TEXT NOT NULL,
    detail        TEXT,            -- JSON
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full payload per generation, so runs are replayable and diffable offline.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_label     TEXT NOT NULL,   -- v1 | v2 | noise_1 ... | replay_1 ...
    entity_type   TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    model         TEXT NOT NULL,
    feedback_ids  TEXT,            -- JSON array of feedback_log.id actually injected
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runs_lookup ON runs (table_name, entity_type, run_label);

-- Audit trail when new feedback contradicts existing feedback on the same signature.
CREATE TABLE IF NOT EXISTS feedback_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signature     TEXT    NOT NULL,
    old_id        INTEGER NOT NULL REFERENCES feedback_log(id),
    new_id        INTEGER NOT NULL REFERENCES feedback_log(id),
    kind          TEXT    NOT NULL,   -- action_contradiction | expression_change
    detail        TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
