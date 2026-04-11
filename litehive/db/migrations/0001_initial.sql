CREATE TABLE IF NOT EXISTS pool_state (
    workspace_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    workspace_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_journal (
    task_id TEXT NOT NULL,
    entry_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (task_id, entry_index)
);

CREATE TABLE IF NOT EXISTS stage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    hook_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subagent_sessions (
    task_id TEXT NOT NULL,
    subagent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (task_id, subagent_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    created_at TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_monitoring (
    engine_name TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    task_id TEXT PRIMARY KEY,
    worktree_path TEXT,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
