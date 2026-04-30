CREATE TABLE pipeline_sessions_new (
    task_id            TEXT NOT NULL,
    node_name          TEXT NOT NULL,
    engine_name        TEXT NOT NULL,
    engine_session_id  TEXT,
    conversation_id    TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (task_id, node_name, engine_name)
);

INSERT INTO pipeline_sessions_new (
    task_id, node_name, engine_name,
    engine_session_id, conversation_id, metadata, updated_at
)
SELECT
    task_id, node_name, engine_name,
    engine_session_id, conversation_id, metadata, updated_at
FROM pipeline_sessions;

DROP TABLE pipeline_sessions;

ALTER TABLE pipeline_sessions_new RENAME TO pipeline_sessions;
