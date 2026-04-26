ALTER TABLE task_intent ADD COLUMN slug TEXT NOT NULL DEFAULT '';
ALTER TABLE task_intent ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE task_intent ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
ALTER TABLE task_intent ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium';
ALTER TABLE task_intent ADD COLUMN goal TEXT NOT NULL DEFAULT '';
ALTER TABLE task_intent ADD COLUMN acceptance_criteria_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task_intent ADD COLUMN constraints_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task_intent ADD COLUMN plan_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task_intent ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task_intent ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE task_intent ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE task_intent ADD COLUMN pipeline_status TEXT NOT NULL DEFAULT 'backlog';

CREATE INDEX IF NOT EXISTS idx_task_intent_lifecycle_status
    ON task_intent (lifecycle_status);

CREATE TABLE IF NOT EXISTS runtime_process_state (
    process_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    pid INTEGER,
    workspace TEXT,
    command TEXT,
    active_task_id TEXT,
    log_dir TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
