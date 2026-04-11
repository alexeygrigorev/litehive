-- v2 pipeline task state. One row per task. The ``payload`` blob carries
-- everything that isn't indexed (counters, failure_context, last_rejection,
-- last_report, failed_reason, failed_message).
CREATE TABLE IF NOT EXISTS pipeline_task_state (
    task_id       TEXT PRIMARY KEY,
    stage         TEXT NOT NULL,
    pipeline_mode TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pipeline_task_state_stage
    ON pipeline_task_state (stage);

-- v2 per-(task, node, engine) session handles. Used by ``SessionStore`` so
-- tier-1 retries reuse the same session (enabling --continue) and engine
-- switches start fresh.
CREATE TABLE IF NOT EXISTS pipeline_sessions (
    task_id            TEXT NOT NULL,
    node_name          TEXT NOT NULL,
    engine_name        TEXT NOT NULL,
    engine_session_id  TEXT,
    conversation_id    TEXT,
    turn_count         INTEGER NOT NULL DEFAULT 0,
    metadata           TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (task_id, node_name, engine_name)
);
