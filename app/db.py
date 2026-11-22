"""SQLite 连接管理与建表。

并发模型：
- WAL 模式：长事务的只读核查不阻塞补传写入，读写可并行。
- 所有写操作使用 BEGIN IMMEDIATE 短事务，配合 busy_timeout 串行化。
"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at INTEGER NOT NULL,          -- UTC 微秒
    filename    TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL DEFAULT 0,
    applied     INTEGER NOT NULL DEFAULT 1, -- 1=已生效 0=已整批退回
    error       TEXT,
    detail      TEXT                        -- 冲突明细 JSON
);

CREATE TABLE IF NOT EXISTS captures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    url         TEXT NOT NULL,
    captured_at INTEGER NOT NULL,          -- UTC 微秒
    outlinks    TEXT NOT NULL DEFAULT '[]',-- JSON 数组，排序去重后的同站出链
    UNIQUE (url, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_captures_batch ON captures(batch_id);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,        -- UTC 微秒
    entry_url     TEXT NOT NULL,
    lookback      INTEGER NOT NULL,        -- UTC 微秒，回看时刻
    base_batch    INTEGER NOT NULL,        -- 核查快照可见的最大已生效批次号
    status        TEXT NOT NULL CHECK (status IN ('valid', 'needs_recheck')),
    visited_count INTEGER NOT NULL,
    missing_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_pages (
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    url         TEXT NOT NULL,
    captured_at INTEGER,                   -- 核查所选抓存；NULL 表示缺档
    capture_id  INTEGER,
    path        TEXT,                      -- 缺档时：从入口到该 URL 的路径 JSON
    PRIMARY KEY (report_id, url)
);
"""


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"], timeout=30, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        get_db().executescript(SCHEMA)
