CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    old_value_json TEXT,
    new_value_json TEXT,
    context_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runtime_settings_audit_key_id
    ON runtime_settings_audit_log (key, id DESC);

CREATE INDEX IF NOT EXISTS idx_runtime_settings_audit_created_at
    ON runtime_settings_audit_log (created_at DESC);
