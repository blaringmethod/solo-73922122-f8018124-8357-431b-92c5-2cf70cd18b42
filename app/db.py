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
    outlinks    TEXT NOT NULL DEFAULT '[]',-- base_id 为 NULL 时：完整出链 JSON；差量行置空
    base_id     INTEGER,                   -- NULL=完整快照；否则为差量基抓存 id
    delta_add   TEXT,                      -- 相对基新增的出链 JSON（差量行）
    delta_remove TEXT,                     -- 相对基移除的出链 JSON（差量行）
    depth       INTEGER NOT NULL DEFAULT 1,-- 还原需读取的载荷条数（链长，<=8）
    full_len    INTEGER NOT NULL DEFAULT 0,-- 该版若存完整 JSON 的字节数（统计用）
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


# 旧库迁移：captures 表补上差量存储所需的列。
# 旧行 base_id 为 NULL，按完整 JSON 直接读取，无需改写任何既有数据。
_MIGRATION_STMTS = (
    "ALTER TABLE captures ADD COLUMN base_id INTEGER",
    "ALTER TABLE captures ADD COLUMN delta_add TEXT",
    "ALTER TABLE captures ADD COLUMN delta_remove TEXT",
    "ALTER TABLE captures ADD COLUMN depth INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE captures ADD COLUMN full_len INTEGER NOT NULL DEFAULT 0",
    # 旧行都是完整快照，其"逐版完整 JSON 字节数"即 outlinks 长度
    "UPDATE captures SET full_len = LENGTH(outlinks)",
)


def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(captures)")}
    if "base_id" in cols:
        return
    for stmt in _MIGRATION_STMTS:
        conn.execute(stmt)


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        _migrate(conn)
