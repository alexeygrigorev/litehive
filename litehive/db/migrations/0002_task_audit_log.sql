CREATE TABLE IF NOT EXISTS task_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    task_status_before TEXT,
    task_status_after TEXT,
    pipeline_status_before TEXT,
    pipeline_status_after TEXT,
    queue_position_before INTEGER,
    queue_position_after INTEGER,
    context_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_task_audit_log_task_id_id
    ON task_audit_log (task_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_task_audit_log_action_id
    ON task_audit_log (action, id DESC);
