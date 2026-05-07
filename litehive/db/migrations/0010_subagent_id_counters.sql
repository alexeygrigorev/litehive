CREATE TABLE IF NOT EXISTS subagent_id_counters (
    task_id TEXT PRIMARY KEY,
    next_number INTEGER NOT NULL CHECK (next_number >= 1),
    updated_at TEXT NOT NULL
);
