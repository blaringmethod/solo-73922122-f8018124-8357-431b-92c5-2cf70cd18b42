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


@bp.route("/")
def index():
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 200").fetchall()
    batches = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 200").fetchall()
    stats = conn.execute(
        "SELECT COUNT(*) AS captures, COUNT(DISTINCT url) AS urls FROM captures"
    ).fetchone()
    return render_template(
        "index.html",
        reports=reports,
        batches=batches,
        stats=stats,
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
    if b["applied"]:
        caps = conn.execute(
            "SELECT * FROM captures WHERE batch_id=? ORDER BY url, captured_at LIMIT 500",
            (bid,),
        ).fetchall()
        caps = [
            {"url": c["url"], "captured_at": c["captured_at"],
             "outlinks": json.loads(c["outlinks"])}
            for c in caps
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE batch_id=?", (bid,)
        ).fetchone()["c"]
    conflicts = json.loads(b["detail"]) if b["detail"] else None
    return render_template(
        "batch.html", b=b, caps=caps, total=total, conflicts=conflicts,
        fmt_us=logic.fmt_us,
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
