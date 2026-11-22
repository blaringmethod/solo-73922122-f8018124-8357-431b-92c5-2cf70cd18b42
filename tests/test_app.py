"""断链核查应用的自测：断链识别、补传标复查、交错核查、冲突退回、重启持久化。"""
import csv
import io
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402

T1 = "2024-01-01T00:00:00Z"
T2 = "2024-02-01T00:00:00Z"
T3 = "2024-03-01T00:00:00Z"
LOOKBACK = "2024-06-01T00:00"  # datetime-local 格式，按 UTC


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


def check(client, entry, lookback=LOOKBACK):
    resp = client.post(
        "/checks", data={"entry_url": entry, "lookback": lookback}
    )
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def q(app, sql, args=()):
    with app.app_context():
        return get_db().execute(sql, args).fetchall()


def one(app, sql, args=()):
    rows = q(app, sql, args)
    assert len(rows) == 1
    return rows[0]


def report(app, rid):
    return one(app, "SELECT * FROM reports WHERE id=?", (rid,))


def pages(app, rid):
    return q(app, "SELECT * FROM report_pages WHERE report_id=?", (rid,))


def missing_map(app, rid):
    return {
        p["url"]: json.loads(p["path"])
        for p in pages(app, rid)
        if p["captured_at"] is None
    }


def resolved_map(app, rid):
    return {p["url"]: p["captured_at"] for p in pages(app, rid) if p["captured_at"] is not None}


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"DATABASE": str(tmp_path / "test.db"), "TESTING": True, "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


# ------------------------------------------------------------ 基本核查


def test_missing_and_paths(app, client):
    upload(client, [
        ("http://s/a", T1, ["http://s/b", "http://s/c"]),
        ("http://s/b", T1, ["http://s/d"]),
        ("http://s/c", T1, []),
    ])
    rid = check(client, "http://s/a")
    rep = report(app, rid)
    assert rep["status"] == "valid"
    assert rep["visited_count"] == 3
    assert rep["missing_count"] == 1
    assert missing_map(app, rid) == {"http://s/d": ["http://s/a", "http://s/b", "http://s/d"]}
    assert set(resolved_map(app, rid)) == {"http://s/a", "http://s/b", "http://s/c"}


def test_latest_capture_not_after_lookback(app, client):
    # A 在 T1 的抓存指向 B，在 T2 的抓存指向 C
    upload(client, [
        ("http://s/a", T1, ["http://s/b"]),
        ("http://s/a", T2, ["http://s/c"]),
        ("http://s/b", T1, []),
        ("http://s/c", T1, []),
    ])
    # 回看时刻在 T1 与 T2 之间：应选 T1 的抓存，沿 B 走
    rid = check(client, "http://s/a", "2024-01-15T00:00")
    assert set(resolved_map(app, rid)) == {"http://s/a", "http://s/b"}
    # 回看时刻晚于 T2：应选 T2 的抓存，沿 C 走
    rid2 = check(client, "http://s/a", LOOKBACK)
    assert set(resolved_map(app, rid2)) == {"http://s/a", "http://s/c"}


def test_cycle_checked_once(app, client):
    upload(client, [
        ("http://s/a", T1, ["http://s/b"]),
        ("http://s/b", T1, ["http://s/a", "http://s/c"]),
        ("http://s/c", T1, ["http://s/a"]),
    ])
    rid = check(client, "http://s/a")
    rep = report(app, rid)
    assert rep["visited_count"] == 3
    assert rep["missing_count"] == 0


def test_entry_itself_missing(app, client):
    upload(client, [("http://s/other", T1, [])])
    rid = check(client, "http://s/gone")
    assert missing_map(app, rid) == {"http://s/gone": ["http://s/gone"]}
    assert report(app, rid)["visited_count"] == 0


def test_json_outlinks_cell(app, client):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["url", "captured_at", "outlinks"])
    w.writerow(["http://s/a", T1, '["http://s/b", "http://s/c"]'])
    w.writerow(["http://s/b", T1, ""])
    w.writerow(["http://s/c", T1, ""])
    client.post(
        "/uploads",
        data={"file": (io.BytesIO(buf.getvalue().encode()), "j.csv")},
        content_type="multipart/form-data",
    )
    rid = check(client, "http://s/a")
    assert report(app, rid)["visited_count"] == 3


# ------------------------------------------------------------ 补传 → 待复查


def test_upload_changing_selected_capture_flags_recheck(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"]), ("http://s/b", T1, [])])
    rid = check(client, "http://s/a")
    assert report(app, rid)["status"] == "valid"
    # 补传：A 出现更晚但仍不晚于回看时刻的抓存 → 核查所选会变
    upload(client, [("http://s/a", T2, ["http://s/b"])])
    assert report(app, rid)["status"] == "needs_recheck"


def test_upload_filling_missing_flags_recheck(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"])])
    rid = check(client, "http://s/a")
    assert missing_map(app, rid) == {"http://s/b": ["http://s/a", "http://s/b"]}
    # 补传：为缺档的 B 补上不晚于回看时刻的抓存
    upload(client, [("http://s/b", T2, [])])
    assert report(app, rid)["status"] == "needs_recheck"


def test_irrelevant_uploads_keep_valid(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"]), ("http://s/b", T1, [])])
    rid = check(client, "http://s/a")
    # 晚于回看时刻的抓存：不影响
    upload(client, [("http://s/a", "2024-12-01T00:00:00Z", ["http://s/x"])])
    assert report(app, rid)["status"] == "valid"
    # 无关 URL：不影响
    upload(client, [("http://s/unrelated", T1, [])])
    assert report(app, rid)["status"] == "valid"
    # 缺档 URL 的抓存晚于回看时刻：不影响
    upload(client, [("http://s/a2", T1, ["http://s/c"])])
    rid2 = check(client, "http://s/a2")
    assert "http://s/c" in missing_map(app, rid2)
    upload(client, [("http://s/c", "2024-12-01T00:00:00Z", [])])
    assert report(app, rid2)["status"] == "valid"


def test_recheck_after_fix_is_valid_and_updated(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"])])
    rid = check(client, "http://s/a")
    upload(client, [("http://s/b", T2, [])])
    assert report(app, rid)["status"] == "needs_recheck"
    resp = client.post(f"/reports/{rid}/recheck")
    assert resp.status_code == 302
    new_rid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    rep = report(app, new_rid)
    assert rep["status"] == "valid"
    assert rep["missing_count"] == 0
    assert rep["visited_count"] == 2


# ------------------------------------------------------------ 冲突与退回


def test_conflict_with_existing_rejects_whole_batch(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"])])
    before = one(app, "SELECT COUNT(*) AS c FROM captures")["c"]
    # 同 URL 同时刻但出链不同 → 冲突；同批还有新记录，必须一并退回
    upload(client, [
        ("http://s/a", T1, ["http://s/CHANGED"]),
        ("http://s/new", T1, []),
    ])
    after = one(app, "SELECT COUNT(*) AS c FROM captures")["c"]
    assert after == before
    b = one(app, "SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    assert b["applied"] == 0
    assert "冲突" in b["error"]
    assert q(app, "SELECT * FROM captures WHERE url='http://s/new'") == []


def test_conflict_within_file_rejects(app, client):
    upload(client, [
        ("http://s/a", T1, ["http://s/b"]),
        ("http://s/a", T1, ["http://s/c"]),
    ])
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 0
    b = one(app, "SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    assert b["applied"] == 0


def test_exact_duplicate_skipped(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"])])
    upload(client, [("http://s/a", T1, ["http://s/b"]), ("http://s/c", T1, [])])
    b = one(app, "SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    assert b["applied"] == 1
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 2
    assert one(app, "SELECT COUNT(*) AS c FROM captures WHERE url='http://s/a'")["c"] == 1


def test_invalid_rows_rejected(app, client):
    upload(client, [("not-a-url", T1, [])])
    upload(client, [("http://s/a", "not-a-time", [])])
    upload(client, [("http://s/a", T1, ["relative/path"])])
    assert one(app, "SELECT COUNT(*) AS c FROM captures")["c"] == 0
    assert one(app, "SELECT COUNT(*) AS c FROM batches WHERE applied=0")["c"] == 3


# ------------------------------------------------------------ 交错操作


def _delayed_app(tmp_path, delay_ms):
    return create_app({
        "DATABASE": str(tmp_path / "t.db"),
        "TESTING": True,
        "SECRET_KEY": "t",
        "CHECK_STEP_DELAY_MS": delay_ms,
    })


def _chain_rows(n, tail_links):
    rows = []
    for i in range(n):
        nxt = f"http://s/p{i+1}" if i < n - 1 else None
        links = [nxt] if nxt else tail_links
        rows.append((f"http://s/p{i}", T1, links))
    return rows


def _run_check_thread(app, result):
    c = app.test_client()
    resp = c.post("/checks", data={"entry_url": "http://s/p0", "lookback": LOOKBACK})
    result["rid"] = int(resp.headers["Location"].rstrip("/").split("/")[-1])


def test_interleaved_upload_leaves_no_stale_valid_report(tmp_path):
    """核查遍历进行中补传缺档记录：新结论必须直接标为待复查。"""
    app = _delayed_app(tmp_path, delay_ms=50)
    upload(app.test_client(), _chain_rows(15, ["http://s/z"]))
    result = {}
    t = threading.Thread(target=_run_check_thread, args=(app, result))
    t.start()
    time.sleep(0.3)  # 遍历进行中（15 页 × 50ms ≈ 750ms）
    upload(app.test_client(), [("http://s/z", T2, [])])  # 补上缺档 z
    t.join(timeout=60)
    assert t.is_alive() is False
    rep = report(app, result["rid"])
    assert rep["status"] == "needs_recheck"
    # 结论内容基于其快照（z 仍缺档），但状态已正确标记
    assert "http://s/z" in missing_map(app, result["rid"])


def test_interleaved_irrelevant_upload_keeps_valid(tmp_path):
    """核查遍历进行中的无关补传：结论保持有效。"""
    app = _delayed_app(tmp_path, delay_ms=50)
    upload(app.test_client(), _chain_rows(15, []))
    result = {}
    t = threading.Thread(target=_run_check_thread, args=(app, result))
    t.start()
    time.sleep(0.3)
    upload(app.test_client(), [("http://s/unrelated", T1, [])])
    t.join(timeout=60)
    assert report(app, result["rid"])["status"] == "valid"


def test_upload_during_check_not_blocked(tmp_path):
    """遍历期间补传请求能快速完成（不被长核查阻塞）。"""
    app = _delayed_app(tmp_path, delay_ms=100)
    upload(app.test_client(), _chain_rows(20, []))
    result = {}
    t = threading.Thread(target=_run_check_thread, args=(app, result))
    t.start()
    time.sleep(0.3)
    start = time.monotonic()
    resp = upload(app.test_client(), [("http://s/new", T1, [])])
    elapsed = time.monotonic() - start
    t.join(timeout=60)
    assert resp.status_code == 302
    assert elapsed < 5  # 远小于核查总时长 ~2s


# ------------------------------------------------------------ 持久化


def test_reports_survive_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    app1 = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "t"})
    c1 = app1.test_client()
    upload(c1, [("http://s/a", T1, ["http://s/b"])])
    rid = check(c1, "http://s/a")
    # 模拟重启：全新应用实例指向同一数据库文件
    app2 = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "t"})
    rep = report(app2, rid)
    assert rep["entry_url"] == "http://s/a"
    assert rep["status"] == "valid"
    assert missing_map(app2, rid) == {"http://s/b": ["http://s/a", "http://s/b"]}
    resp = app2.test_client().get(f"/reports/{rid}")
    assert resp.status_code == 200
    assert "http://s/b" in resp.get_data(as_text=True)


# ------------------------------------------------------------ 页面冒烟


def test_pages_render(app, client):
    upload(client, [("http://s/a", T1, ["http://s/b"])])
    rid = check(client, "http://s/a")
    assert client.get("/").status_code == 200
    assert client.get(f"/reports/{rid}").status_code == 200
    assert client.get("/batches/1").status_code == 200
    assert client.get("/healthz").data == b"ok"
    html = client.get(f"/reports/{rid}").get_data(as_text=True)
    assert "缺档" in html and "http://s/b" in html
