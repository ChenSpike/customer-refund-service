-- Local audit store for Jenny's triage pipeline.
-- Applied to db/idox_triage_outputs_jenny_local.db.
--
-- Columns mirror the shared GCP tables in idox_appdata_derrick so these local
-- rows can be exported into the shared DB later without transformation.
-- Extra structured detail we produce (failed_check, offending_field, pii_type,
-- routing) is folded into flags_json — the shared table's JSON column for it.

-- Append-only event stream: one row per pipeline event.
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,   -- shared: audit_id (bigint)
    trace_id     TEXT NOT NULL,
    ticket_id    TEXT,
    event_type   TEXT NOT NULL,
    agent        TEXT NOT NULL,   -- 'triage_agent' | 'governance_interceptor' | 'system'
    payload_json TEXT,            -- details as JSON (user_id folded in here)
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id);

-- One row per interceptor verdict (mirrors shared governance_events).
CREATE TABLE IF NOT EXISTS governance_events (
    event_id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- shared: event_id (bigint)
    trace_id           TEXT NOT NULL,
    ticket_id          TEXT,
    agent              TEXT NOT NULL,     -- which agent step this verdict is about
    owasp_category     TEXT,              -- 'ASI01' (injection) | 'ASI07' (data leak) | ...
    trigger_score      REAL,              -- semantic-drift score (NULL: rule-based interceptor)
    interceptor_action TEXT NOT NULL,     -- 'allow' | 'quarantine' | 'block'
    flags_json         TEXT,              -- {failed_check, offending_field, pii_type, detail, next_agent}
    offending_content  TEXT,              -- raw value that triggered the verdict (see risk register)
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_gov_trace ON governance_events(trace_id);

-- Append-only enforcement: reject UPDATE and DELETE on both tables.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS governance_events_no_update BEFORE UPDATE ON governance_events
BEGIN SELECT RAISE(ABORT, 'governance_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS governance_events_no_delete BEFORE DELETE ON governance_events
BEGIN SELECT RAISE(ABORT, 'governance_events is append-only'); END;
