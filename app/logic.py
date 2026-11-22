"""核心业务逻辑：CSV 解析、目录批次生效、断链核查、结论落库与待复查标记。

一致性设计（对应需求）：
- 一个批次的目录记录与受影响的结论状态在同一个事务里生效（apply_batch）。
- 核查遍历在同一个只读快照事务中进行（run_check），期间补传可并发提交（WAL）。
- 结论落库时在写事务里复查"快照之后新生效的批次"是否影响本结论
  （save_report 中的 REVALIDATE_SQL），保证交错操作不会留下过时的有效结论。
- 同一 URL 同一时刻、出链不同的记录视为冲突，整批退回，不产生任何效果。
"""
import csv
import io
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

REQUIRED_COLUMNS = ("url", "captured_at", "outlinks")

# ---------------------------------------------------------------- 时间工具


def to_us(dt):
    """aware datetime -> UTC 微秒整数。"""
    return int(round((dt - EPOCH).total_seconds() * 1_000_000))


def now_us():
    return to_us(datetime.now(timezone.utc))


def us_to_dt(us):
    return EPOCH + timedelta(microseconds=us)


def fmt_us(us):
    if us is None:
        return "—"
    return us_to_dt(us).strftime("%Y-%m-%d %H:%M:%S")


def parse_timestamp(text):
    """解析 ISO 8601 时间；无时区时按 UTC 处理，返回值统一为 UTC。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("时间为空")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"无法解析时间: {text!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- CSV 解析


def validate_url(u):
    p = urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc)


def parse_outlinks(cell):
    """出链单元格：JSON 数组，或以 | 分隔的多个完整 URL。返回排序去重后的列表。"""
    s = (cell or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"出链 JSON 解析失败: {e}")
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError("出链 JSON 必须是字符串数组")
        links = data
    else:
        links = [x.strip() for x in s.split("|") if x.strip()]
    for link in links:
        if not validate_url(link):
            raise ValueError(f"出链不是完整 URL: {link!r}")
    return sorted(set(links))


def parse_catalog(file_bytes):
    """解析目录 CSV。

    成功返回 (rows, None)，rows 为 [(url, captured_at_us, outlinks)]，
    文件内完全重复的记录已去重；失败返回 (None, [错误信息...])，包括
    文件内同 URL 同时刻但出链不同的冲突。
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ["文件不是有效的 UTF-8 文本"]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None, ["空文件或缺少表头"]
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing_cols:
        return None, [
            "缺少列: " + ", ".join(missing_cols) + "（需要 url, captured_at, outlinks）"
        ]
    rows = []
    errors = []
    seen = {}
    for lineno, raw in enumerate(reader, start=2):
        url = (raw.get(headers["url"]) or "").strip()
        ts = raw.get(headers["captured_at"]) or ""
        cell = raw.get(headers["outlinks"]) or ""
        if not validate_url(url):
            errors.append(f"第{lineno}行: 页面地址不是完整 URL: {url!r}")
            continue
        try:
            us = to_us(parse_timestamp(ts))
        except ValueError as e:
            errors.append(f"第{lineno}行: {e}")
            continue
        try:
            links = parse_outlinks(cell)
        except ValueError as e:
            errors.append(f"第{lineno}行: {e}")
            continue
        key = (url, us)
        if key in seen:
            if seen[key] != links:
                errors.append(
                    f"第{lineno}行: 与文件内先前记录冲突（同 URL 同时刻但出链不同）: "
                    f"{url} @ {fmt_us(us)}"
                )
            continue
        seen[key] = links
        rows.append((url, us, links))
    if errors:
        if len(errors) > 50:
            errors = errors[:50] + [f"……共 {len(errors)} 处问题"]
        return None, errors
    return rows, None


# ---------------------------------------------------------------- 批次生效

# 批次生效的同时，把受影响的"有效"结论标为待复查（同一事务）。
# 触发条件（且仅这两个条件）：
#   1) 结论中某页面出现了更晚但仍不晚于回看时刻的抓存 —— 核查所选抓存会变；
#   2) 结论中的缺档 URL 出现了不晚于回看时刻的抓存 —— 缺档被补上。
INVALIDATE_SQL = """
UPDATE reports SET status = 'needs_recheck'
WHERE status = 'valid' AND EXISTS (
    SELECT 1 FROM report_pages rp
    JOIN captures c ON c.url = rp.url
    WHERE rp.report_id = reports.id
      AND c.batch_id = ?
      AND c.captured_at <= reports.lookback
      AND (rp.captured_at IS NULL OR c.captured_at > rp.captured_at)
)
"""


def _record_rejected_batch(conn, filename, row_count, error, detail=None):
    """退回的批次也留痕（独立事务，便于审计）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO batches(uploaded_at, filename, row_count, applied, error, detail)"
            " VALUES (?,?,?,0,?,?)",
            (now_us(), filename, row_count, error, detail),
        )
        conn.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_rejected_batch(conn, filename, errors):
    return _record_rejected_batch(
        conn, filename, 0, "整批退回：" + "；".join(errors[:10])
    )


def apply_batch(conn, filename, rows):
    """把一个已解析的目录批次写入档案库，并同步刷新结论状态。

    返回 dict(batch_id, applied, inserted, skipped, conflicts)。
    若与既有档案存在同 URL 同时刻但出链不同的冲突：整批退回，
    不写入任何记录（仅登记一条"已退回"的批次记录）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        duplicates = set()
        conflicts = []
        for url, us, links in rows:
            r = conn.execute(
                "SELECT outlinks FROM captures WHERE url=? AND captured_at=?",
                (url, us),
            ).fetchone()
            if r is None:
                continue
            old = json.loads(r["outlinks"])
            if old != links:
                conflicts.append(
                    {"url": url, "captured_at": us, "existing": old, "new": links}
                )
            else:
                duplicates.add((url, us))
        if conflicts:
            conn.execute("ROLLBACK")
            bid = _record_rejected_batch(
                conn,
                filename,
                len(rows),
                f"与既有档案冲突 {len(conflicts)} 处（同 URL 同时刻出链不同），整批退回",
                json.dumps(conflicts[:50], ensure_ascii=False),
            )
            return {
                "batch_id": bid,
                "applied": False,
                "inserted": 0,
                "skipped": 0,
                "conflicts": conflicts,
            }

        cur = conn.execute(
            "INSERT INTO batches(uploaded_at, filename, row_count, applied)"
            " VALUES (?,?,?,1)",
            (now_us(), filename, len(rows)),
        )
        bid = cur.lastrowid
        inserted = 0
        for url, us, links in rows:
            if (url, us) in duplicates:
                continue
            conn.execute(
                "INSERT INTO captures(batch_id, url, captured_at, outlinks)"
                " VALUES (?,?,?,?)",
                (bid, url, us, json.dumps(links, ensure_ascii=False)),
            )
            inserted += 1
        # 目录与结论状态同步生效
        conn.execute(INVALIDATE_SQL, (bid,))
        conn.execute("COMMIT")
        return {
            "batch_id": bid,
            "applied": True,
            "inserted": inserted,
            "skipped": len(rows) - inserted,
            "conflicts": [],
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------- 断链核查


def run_check(conn, entry_url, lookback_us, step_delay=0.0):
    """在同一目录快照上从入口沿出链逐页核查（BFS，互链只查一遍）。

    返回 (base_batch, visited, missing)：
    - base_batch: 快照可见的最大已生效批次号；
    - visited:    {url: (capture_id, captured_at)}，每个 URL 取不晚于
                  回看时刻的最近一条抓存；
    - missing:    {url: [入口, ..., 该URL]}，缺档 URL 及到达路径。
    """
    conn.execute("BEGIN")  # 只读快照事务；WAL 下不阻塞并发补传
    try:
        base_batch = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM batches WHERE applied = 1"
        ).fetchone()[0]
        visited = {}
        missing = {}
        queue = deque([(entry_url, [entry_url])])
        enqueued = {entry_url}
        while queue:
            url, path = queue.popleft()
            if url in visited or url in missing:
                continue
            row = conn.execute(
                "SELECT id, captured_at, outlinks FROM captures"
                " WHERE url=? AND captured_at<=?"
                " ORDER BY captured_at DESC, id DESC LIMIT 1",
                (url, lookback_us),
            ).fetchone()
            if row is None:
                missing[url] = path
            else:
                visited[url] = (row["id"], row["captured_at"])
                for link in json.loads(row["outlinks"]):
                    if link not in enqueued and link not in visited and link not in missing:
                        enqueued.add(link)
                        queue.append((link, path + [link]))
            if step_delay:
                time.sleep(step_delay)
        conn.execute("COMMIT")
        return base_batch, visited, missing
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# 结论落库时复查：快照之后新生效的批次是否已影响本结论，
# 若是则直接标为待复查，避免交错操作留下过时的"有效"结论。
REVALIDATE_SQL = """
UPDATE reports SET status = 'needs_recheck'
WHERE id = ? AND EXISTS (
    SELECT 1 FROM report_pages rp
    JOIN captures c ON c.url = rp.url
    WHERE rp.report_id = ?
      AND c.batch_id > ?
      AND c.captured_at <= ?
      AND (rp.captured_at IS NULL OR c.captured_at > rp.captured_at)
)
"""


def save_report(conn, entry_url, lookback_us, base_batch, visited, missing):
    """把核查结论及其逐页明细落库（单个写事务），返回报告 id。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO reports(created_at, entry_url, lookback, base_batch,"
            " status, visited_count, missing_count) VALUES (?,?,?,?,'valid',?,?)",
            (now_us(), entry_url, lookback_us, base_batch, len(visited), len(missing)),
        )
        rid = cur.lastrowid
        for url, (capture_id, captured_at) in visited.items():
            conn.execute(
                "INSERT INTO report_pages(report_id, url, captured_at, capture_id, path)"
                " VALUES (?,?,?,?,NULL)",
                (rid, url, captured_at, capture_id),
            )
        for url, path in missing.items():
            conn.execute(
                "INSERT INTO report_pages(report_id, url, captured_at, capture_id, path)"
                " VALUES (?,?,NULL,NULL,?)",
                (rid, url, json.dumps(path, ensure_ascii=False)),
            )
        conn.execute(REVALIDATE_SQL, (rid, rid, base_batch, lookback_us))
        conn.execute("COMMIT")
        return rid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
