CREATE TABLE IF NOT EXISTS task_intent (
    task_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
