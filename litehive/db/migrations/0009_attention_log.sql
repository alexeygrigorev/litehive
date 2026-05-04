CREATE TABLE IF NOT EXISTS attention_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attention_log_created_at
    ON attention_log (created_at DESC);
