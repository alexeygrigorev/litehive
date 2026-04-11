-- Structured record of every state-machine transition, one row per hop.
CREATE TABLE IF NOT EXISTS pipeline_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_payload TEXT NOT NULL DEFAULT '{}',
    to_stage TEXT NOT NULL,
    rule_description TEXT NOT NULL DEFAULT '',
    delta TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pipeline_transitions_task_seq
    ON pipeline_transitions (task_id, seq);

CREATE INDEX IF NOT EXISTS idx_pipeline_transitions_event_type
    ON pipeline_transitions (event_type);

-- Runner lifecycle events: task_started, stop_requested, task_finished, etc.
-- Free-form payload so adding a new kind never needs a schema change.
CREATE TABLE IF NOT EXISTS pipeline_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pipeline_journal_task_seq
    ON pipeline_journal (task_id, seq);

CREATE INDEX IF NOT EXISTS idx_pipeline_journal_task_kind
    ON pipeline_journal (task_id, kind);
