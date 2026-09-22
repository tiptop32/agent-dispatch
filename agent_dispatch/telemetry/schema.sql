CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    escalated_from TEXT,
    root_agent TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    hop INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    executor TEXT,
    model TEXT,
    request_json TEXT NOT NULL,
    decision_json TEXT,
    result_json TEXT,
    log_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    router TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    choice TEXT NOT NULL,
    confidence REAL NOT NULL,
    scores_json TEXT NOT NULL,
    judgments_json TEXT NOT NULL,
    guard_reason TEXT,
    latency_ms INTEGER NOT NULL,
    cost_usd REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent_task_id ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_task_id ON routing_decisions(task_id);
