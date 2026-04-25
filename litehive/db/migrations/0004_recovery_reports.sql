CREATE TABLE IF NOT EXISTS recovery_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    origin_stage TEXT,
    trigger_event_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recovery_reports_task_id
    ON recovery_reports (task_id, id);
