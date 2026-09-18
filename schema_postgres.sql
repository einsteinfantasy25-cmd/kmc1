-- Optional reference schema. The bot creates/migrates these tables automatically.
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    student_name TEXT,
    calculations INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'unknown',
    last_status_check TEXT,
    banned INTEGER DEFAULT 0,
    ban_reason TEXT,
    banned_at TEXT,
    current_stage INTEGER
);

CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT,
    student_name TEXT,
    username TEXT,
    filename TEXT,
    path TEXT,
    stage INTEGER,
    mode TEXT,
    summary TEXT,
    html_content TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS stage_results (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    stage INTEGER NOT NULL,
    mode TEXT NOT NULL,
    avg TEXT,
    min_avg TEXT,
    mid_avg TEXT,
    max_avg TEXT,
    contribution TEXT,
    min_contribution TEXT,
    mid_contribution TEXT,
    max_contribution TEXT,
    answers_json TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stage_results_user_stage ON stage_results(telegram_id, stage);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id BIGSERIAL PRIMARY KEY,
    admin_id BIGINT,
    target TEXT,
    source_chat_id BIGINT,
    source_message_id BIGINT,
    total INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    status TEXT,
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS cumulative_history (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT,
    target_stage INTEGER,
    result_type TEXT,
    current_min TEXT,
    current_max TEXT,
    final_contribution_min TEXT,
    final_contribution_max TEXT,
    values_json TEXT,
    created_at TEXT
);
