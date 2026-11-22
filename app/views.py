"""HTTP 路由。"""
import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic
from .db import get_db

bp = Blueprint("main", __name__)


# 出链载荷统计：实际落库的出链字节数 vs 逐版存完整 JSON 的字节数。
_PAYLOAD_SQL = """
COALESCE(SUM(LENGTH(outlinks)
           + LENGTH(COALESCE(delta_add, ''))
           + LENGTH(COALESCE(delta_remove, ''))), 0) AS payload,
COALESCE(SUM(full_len), 0) AS full_total
"""


def _payload_stats(row):
    payload, full = row["payload"], row["full_total"]
    saving = round((1 - payload / full) * 100, 1) if full else 0.0
    return {"payload": payload, "full_total": full, "saving": saving}


@bp.route("/")
def index():
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 200").fetchall()
    batches = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 200").fetchall()
    stats = conn.execute(
        "SELECT COUNT(*) AS captures, COUNT(DISTINCT url) AS urls,"
        + _PAYLOAD_SQL
        + " FROM captures"
    ).fetchone()
    return render_template(
        "index.html",
        reports=reports,
        batches=batches,
        stats=stats,
        payload=_payload_stats(stats),
        fmt_us=logic.fmt_us,
    )


@bp.route("/healthz")
def healthz():
    return "ok"


@bp.route("/uploads", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))
    rows, errors = logic.parse_catalog(f.read())
    conn = get_db()
    if errors:
        bid = logic.record_rejected_batch(conn, f.filename, errors)
        flash(f"批次 #{bid} 已整批退回：{errors[0]}" + ("……" if len(errors) > 1 else ""), "error")
        return redirect(url_for("main.batch_detail", bid=bid))
    result = logic.apply_batch(conn, f.filename, rows)
    if not result["applied"]:
        flash(
            f"批次 #{result['batch_id']} 已整批退回：与既有档案冲突 "
            f"{len(result['conflicts'])} 处",
            "error",
        )
        return redirect(url_for("main.batch_detail", bid=result["batch_id"]))
    flash(
        f"批次 #{result['batch_id']} 已生效：新增 {result['inserted']} 条"
        f"（跳过完全重复 {result['skipped']} 条），相关结论状态已同步刷新",
        "ok",
    )
    return redirect(url_for("main.batch_detail", bid=result["batch_id"]))


@bp.route("/batches/<int:bid>")
def batch_detail(bid):
    conn = get_db()
    b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if b is None:
        abort(404)
    caps = []
    total = 0
    payload = None
    if b["applied"]:
        rows = conn.execute(
            "SELECT * FROM captures WHERE batch_id=? ORDER BY url, captured_at LIMIT 500",
            (bid,),
        ).fetchall()
        cache = {}
        for c in rows:
            # 批次详情始终展示还原后的完整出链
            links, _ = logic.load_outlinks(conn, c, cache)
            if c["base_id"] is None:
                storage = "完整"
            else:
                n_add = len(json.loads(c["delta_add"] or "[]"))
                n_rm = len(json.loads(c["delta_remove"] or "[]"))
                storage = f"差量 +{n_add}/−{n_rm}（基 #{c['base_id']}）"
            caps.append(
                {"url": c["url"], "captured_at": c["captured_at"],
                 "outlinks": links, "storage": storage}
            )
        stats = conn.execute(
            "SELECT COUNT(*) AS c," + _PAYLOAD_SQL + " FROM captures WHERE batch_id=?",
            (bid,),
        ).fetchone()
        total = stats["c"]
        payload = _payload_stats(stats)
    conflicts = json.loads(b["detail"]) if b["detail"] else None
    return render_template(
        "batch.html", b=b, caps=caps, total=total, conflicts=conflicts,
        payload=payload, fmt_us=logic.fmt_us,
    )


def _run_and_save(entry_url, lookback_us):
    conn = get_db()
    delay = current_app.config.get("CHECK_STEP_DELAY_MS", 0) / 1000.0
    base, visited, missing = logic.run_check(
        conn, entry_url, lookback_us, step_delay=delay
    )
    return logic.save_report(conn, entry_url, lookback_us, base, visited, missing)


@bp.route("/checks", methods=["POST"])
def check():
    entry = (request.form.get("entry_url") or "").strip()
    lookback_s = (request.form.get("lookback") or "").strip()
    if not logic.validate_url(entry):
        flash("入口必须是完整 URL（http/https）", "error")
        return redirect(url_for("main.index"))
    try:
        lookback_us = logic.to_us(logic.parse_timestamp(lookback_s))
    except ValueError as e:
        flash(f"回看时刻无效：{e}", "error")
        return redirect(url_for("main.index"))
    rid = _run_and_save(entry, lookback_us)
    return redirect(url_for("main.report_detail", rid=rid))


@bp.route("/reports/<int:rid>")
def report_detail(rid):
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if rep is None:
        abort(404)
    pages = conn.execute(
        "SELECT * FROM report_pages WHERE report_id=? ORDER BY url", (rid,)
    ).fetchall()
    missing = [
        {"url": p["url"], "path": json.loads(p["path"])}
        for p in pages
        if p["captured_at"] is None
    ]
    resolved = [p for p in pages if p["captured_at"] is not None]
    return render_template(
        "report.html", rep=rep, missing=missing, resolved=resolved,
        fmt_us=logic.fmt_us,
    )


@bp.route("/reports/<int:rid>/recheck", methods=["POST"])
def recheck(rid):
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if rep is None:
        abort(404)
    new_rid = _run_and_save(rep["entry_url"], rep["lookback"])
    flash(f"已按相同入口与回看时刻重新核查，生成结论 #{new_rid}", "ok")
    return redirect(url_for("main.report_detail", rid=new_rid))
