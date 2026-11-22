"""出链差量存储的专项测试：

- 120 次抓存基准：出链载荷合计 < 逐版完整 JSON 的一半；还原最多读 8 条载荷；
- 乱序补传旧时点：已存载荷原样保留；
- 重复/冲突按还原后的出链判定，冲突整批退回；
- 旧库迁移后可直接使用；
- 核查/批次详情走还原后的完整出链。
"""
import csv
import io
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402
from app import logic  # noqa: E402

BASE_TS = 1_704_067_200_000_000  # 2024-01-01T00:00:00Z，UTC 微秒
MINUTE = 60 * 1_000_000


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["url", "captured_at", "outlinks"])
    for url, ts, links in rows:
        w.writerow([url, ts, "|".join(links)])
    return buf.getvalue().encode()


def upload(client, rows, filename="catalog.csv"):
    return client.post(
        "/uploads",
        data={"file": (io.BytesIO(make_csv(rows)), filename)},
        content_type="multipart/form-data",
    )


def q(app, sql, args=()):
    with app.app_context():
        return get_db().execute(sql, args).fetchall()


def one(app, sql, args=()):
    rows = q(app, sql, args)
    assert len(rows) == 1
    return rows[0]


def restore(app, cap_id):
    """还原指定抓存，返回 (links, reads)。"""
    with app.app_context():
        conn = get_db()
        row = conn.execute(
            f"SELECT {logic.CAP_COLS} FROM captures WHERE id=?", (cap_id,)
        ).fetchone()
        return logic.load_outlinks(conn, row)


def bench_links(i, n=40):
    """第 i 版出链：40 条等长 URL 的滑动窗口，相邻两版恰好换一条。"""
    return [f"http://bench.site/l{k:04d}" for k in range(i, i + n)]


def bench_rows(versions=120):
    """同一 URL 的 versions 次抓存，每次 40 条等长出链，相邻只换一条。"""
    rows = []
    for i in range(versions):
        ts = logic.us_to_dt(BASE_TS + i * MINUTE).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(("http://bench.site/page", ts, bench_links(i)))
    return rows


def payload_total(app):
    return one(
        app,
        "SELECT COALESCE(SUM(LENGTH(outlinks)"
        " + LENGTH(COALESCE(delta_add, ''))"
        " + LENGTH(COALESCE(delta_remove, ''))), 0) AS p FROM captures",
    )["p"]


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"DATABASE": str(tmp_path / "test.db"), "TESTING": True, "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


# ------------------------------------------------------------ 120 次抓存基准


def test_delta_payload_under_half_and_restore_bound(app, client):
    rows = bench_rows(120)
    upload(client, rows, "bench.csv")

    # 出链载荷合计 < 逐版存完整 JSON 的一半
    payload = payload_total(app)
    baseline = sum(len(json.dumps(links, ensure_ascii=False)) for _, _, links in rows)
    assert payload < baseline / 2, f"载荷 {payload} 应小于基线 {baseline} 的一半"

    # 统计列 full_len 与基线一致（页面统计口径即"逐版完整 JSON"）
    assert one(app, "SELECT SUM(full_len) AS f FROM captures")["f"] == baseline

    # 确实用了差量，且发生了链满重基（完整快照不止一条）
    n_delta = one(app, "SELECT COUNT(*) AS c FROM captures WHERE base_id IS NOT NULL")["c"]
    n_full = one(app, "SELECT COUNT(*) AS c FROM captures WHERE base_id IS NULL")["c"]
    assert n_delta > 100 and n_full > 1

    # 链深上限 8：任意抓存还原最多读 8 条载荷
    assert one(app, "SELECT MAX(depth) AS d FROM captures")["d"] <= logic.MAX_CHAIN
    caps = q(app, "SELECT id, captured_at FROM captures ORDER BY captured_at")
    assert len(caps) == 120
    for i, c in enumerate(caps):
        links, reads = restore(app, c["id"])
        assert links == bench_links(i)  # 还原结果与原始出链一致
        assert reads <= logic.MAX_CHAIN


def test_delta_chain_never_exceeds_bound_on_long_import(app, client):
    # 更多版本（300 次）下链深仍然不超上限
    upload(client, bench_rows(300), "bench300.csv")
    assert one(app, "SELECT MAX(depth) AS d FROM captures")["d"] <= logic.MAX_CHAIN
    caps = q(app, "SELECT id FROM captures")
    for c in caps:
        _, reads = restore(app, c["id"])
        assert reads <= logic.MAX_CHAIN


# ------------------------------------------------------------ 乱序补传


def test_backfill_keeps_stored_payloads(app, client):
    # 先按时刻顺序导入 10 版（T0..T9）
    first = bench_rows(10)
    upload(client, first, "in_order.csv")
    before = q(app, "SELECT * FROM captures ORDER BY id")
    before_dump = [dict(r) for r in before]

    # 乱序补传旧时点：T4 与 T5 之间的 T4.5
    ts_mid = logic.us_to_dt(BASE_TS + 4 * MINUTE + 30_000_000).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    mid_links = bench_links(4)[:-1] + ["http://bench.site/l0044"]  # 与相邻版差一条
    upload(client, [("http://bench.site/page", ts_mid, mid_links)], "backfill.csv")

    after = q(app, "SELECT * FROM captures ORDER BY id")
    assert len(after) == len(before) + 1
    # 已存载荷原样保留：旧行逐字节不变
    for old, new in zip(before_dump, [dict(r) for r in after[:-1]]):
        assert old == new
    # 新行插在旧时点，且以差量存储（与相邻版只差一条）
    new_row = after[-1]
    assert new_row["base_id"] is not None
    links, reads = restore(app, new_row["id"])
    assert links == sorted(mid_links)
    assert reads <= logic.MAX_CHAIN
    # 既有抓存还原结果不变
    for i, r in enumerate(after[:-1]):
        links, _ = restore(app, r["id"])
        assert links == bench_links(i)


def test_backfill_before_all_existing(app, client):
    upload(client, bench_rows(5)[2:], "tail.csv")  # 先存 T2..T4
    upload(client, bench_rows(2), "older.csv")  # 补传更早的 T0..T1
    caps = q(app, "SELECT id FROM captures ORDER BY captured_at")
    assert len(caps) == 5
    for i, c in enumerate(caps):
        links, reads = restore(app, c["id"])
        assert links == bench_links(i)
        assert reads <= logic.MAX_CHAIN


# ------------------------------------------------------------ 重复/冲突按还原后出链判定


def test_duplicate_and_conflict_judged_by_restored_outlinks(app, client):
    rows = bench_rows(10)
    upload(client, rows, "bench.csv")
    # 第 6 版（链中间）必然是差量存储
    target = one(
        app, "SELECT * FROM captures ORDER BY captured_at LIMIT 1 OFFSET 5"
    )
    assert target["base_id"] is not None

    ts5 = logic.us_to_dt(BASE_TS + 5 * MINUTE).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 完全相同的记录（与还原后出链一致）→ 判重跳过，不新增
    upload(client, [("http://bench.site/page", ts5, bench_links(5))], "dup.csv")
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 10
    b = one(app, "SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    assert b["applied"] == 1

    # 同 URL 同时刻但出链不同 → 冲突，整批退回（同批新记录也不写入）
    changed = bench_links(5)[:-1] + ["http://bench.site/l9999"]
    upload(
        client,
        [("http://bench.site/page", ts5, changed), ("http://bench.site/new", ts5, [])],
        "conflict.csv",
    )
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 10
    b = one(app, "SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    assert b["applied"] == 0
    assert "冲突" in b["error"]
    # 冲突明细里是还原后的完整出链
    detail = json.loads(b["detail"])
    assert detail[0]["existing"] == bench_links(5)
    assert q(app, "SELECT * FROM captures WHERE url='http://bench.site/new'") == []


# ------------------------------------------------------------ 旧库迁移

OLD_SCHEMA = """
CREATE TABLE batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at INTEGER NOT NULL,
    filename    TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL DEFAULT 0,
    applied     INTEGER NOT NULL DEFAULT 1,
    error       TEXT,
    detail      TEXT
);
CREATE TABLE captures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    url         TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    outlinks    TEXT NOT NULL DEFAULT '[]',
    UNIQUE (url, captured_at)
);
CREATE INDEX idx_captures_batch ON captures(batch_id);
CREATE TABLE reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    entry_url     TEXT NOT NULL,
    lookback      INTEGER NOT NULL,
    base_batch    INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('valid', 'needs_recheck')),
    visited_count INTEGER NOT NULL,
    missing_count INTEGER NOT NULL
);
CREATE TABLE report_pages (
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    url         TEXT NOT NULL,
    captured_at INTEGER,
    capture_id  INTEGER,
    path        TEXT,
    PRIMARY KEY (report_id, url)
);
"""


def test_old_database_usable_directly(tmp_path):
    """旧库（无差量列）不做任何手工处理即可被新版本直接使用。"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    old_links = bench_links(0)
    conn.execute(
        "INSERT INTO batches(id, uploaded_at, filename, row_count, applied)"
        " VALUES (1, ?, 'old.csv', 1, 1)",
        (BASE_TS,),
    )
    conn.execute(
        "INSERT INTO captures(id, batch_id, url, captured_at, outlinks)"
        " VALUES (1, 1, 'http://bench.site/page', ?, ?)",
        (BASE_TS, json.dumps(old_links, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()

    app = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "t"})
    client = app.test_client()

    # 迁移补列，旧行按完整快照直接还原
    cols = {r["name"] for r in q(app, "PRAGMA table_info(captures)")}
    assert {"base_id", "delta_add", "delta_remove", "depth", "full_len"} <= cols
    links, reads = restore(app, 1)
    assert links == old_links and reads == 1
    row = one(app, "SELECT * FROM captures WHERE id=1")
    assert row["base_id"] is None and row["depth"] == 1
    assert row["full_len"] == len(json.dumps(old_links, ensure_ascii=False))

    # 新抓存可基于旧行存差量
    ts1 = logic.us_to_dt(BASE_TS + MINUTE).strftime("%Y-%m-%dT%H:%M:%SZ")
    upload(client, [("http://bench.site/page", ts1, bench_links(1))], "new.csv")
    new_row = one(app, "SELECT * FROM captures WHERE id=2")
    assert new_row["base_id"] == 1
    links, reads = restore(app, 2)
    assert links == bench_links(1) and reads == 2

    # 旧库上的核查照常工作（选档规则不变）
    rid = client.post(
        "/checks",
        data={"entry_url": "http://bench.site/page", "lookback": "2024-06-01T00:00"},
    ).headers["Location"].rstrip("/").split("/")[-1]
    assert one(app, "SELECT missing_count AS m FROM reports WHERE id=?", (rid,))["m"] == 40


# ------------------------------------------------------------ 核查与页面走还原出链


def test_check_follows_restored_outlinks(app, client):
    # A 的 9 次抓存出链恒定指向 B（链中间的版本必然差量存储），B 指向缺档 C
    rows = []
    for i in range(9):
        ts = logic.us_to_dt(BASE_TS + i * MINUTE).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(("http://s/a", ts, ["http://s/b"]))
    rows.append(("http://s/b", "2024-01-01T00:00:00Z", ["http://s/c"]))
    upload(client, rows)
    deep = one(app, "SELECT * FROM captures WHERE url='http://s/a' ORDER BY captured_at DESC LIMIT 1")
    assert deep["base_id"] is not None and deep["depth"] > 1  # 确认是差量链

    rid = client.post(
        "/checks", data={"entry_url": "http://s/a", "lookback": "2024-06-01T00:00"}
    ).headers["Location"].rstrip("/").split("/")[-1]
    pages_ = q(app, "SELECT * FROM report_pages WHERE report_id=?", (rid,))
    resolved = {p["url"] for p in pages_ if p["captured_at"] is not None}
    missing = {p["url"] for p in pages_ if p["captured_at"] is None}
    assert resolved == {"http://s/a", "http://s/b"}  # 沿还原后的出链走到 B
    assert missing == {"http://s/c"}
    # 选档仍是不晚于回看时刻的最近一条（A 的第 9 版）
    a_page = [p for p in pages_ if p["url"] == "http://s/a"][0]
    assert a_page["captured_at"] == BASE_TS + 8 * MINUTE


def test_batch_detail_shows_full_outlinks(app, client):
    upload(client, bench_rows(10), "bench.csv")
    html = client.get("/batches/1").get_data(as_text=True)
    # 差量存储的抓存，页面上仍显示还原后的完整出链
    assert "差量" in html and "完整" in html
    assert "http://bench.site/l0000" in html
    assert "http://bench.site/l0048" in html  # 第 10 版窗口末端
    assert "出链载荷" in html and "节省" in html


def test_index_shows_payload_stats(app, client):
    upload(client, bench_rows(120), "bench.csv")
    html = client.get("/").get_data(as_text=True)
    assert "出链载荷合计" in html
    assert "逐版存完整 JSON" in html
    payload = payload_total(app)
    baseline = one(app, "SELECT SUM(full_len) AS f FROM captures")["f"]
    assert str(payload) in html and str(baseline) in html


# ------------------------------------------------------------ 事务原子性


def test_failed_write_rolls_back_catalog_and_flags(app, client, monkeypatch):
    """批次生效事务中写入失败：目录与受影响结论的待复查标记均保留原状。"""
    upload(client, [("http://s/a", "2024-01-01T00:00:00Z", ["http://s/b"])])
    rid = client.post(
        "/checks", data={"entry_url": "http://s/a", "lookback": "2024-06-01T00:00"}
    ).headers["Location"].rstrip("/").split("/")[-1]
    assert one(app, "SELECT status AS s FROM reports WHERE id=?", (rid,))["s"] == "valid"

    # 让"标记待复查"这一步写入失败（差量入库已在同事务内执行过）
    monkeypatch.setattr(logic, "INVALIDATE_SQL", "UPDATE no_such_table SET x = 1")
    with pytest.raises(Exception):
        upload(client, [("http://s/a", "2024-02-01T00:00:00Z", ["http://s/b"])])

    # 整事务回滚：新抓存未入库、批次未登记、结论仍是"有效"
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 1
    assert one(app, "SELECT COUNT(*) AS c FROM batches")["c"] == 1
    assert one(app, "SELECT status AS s FROM reports WHERE id=?", (rid,))["s"] == "valid"
