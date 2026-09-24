# -*- coding: utf-8 -*-
"""media_crawler_pro 数据库前端 + 评论 AI 分析
启动: py app.py  → http://127.0.0.1:5000
- 后台守护线程自动分析新增评论(analyzed=0 的会被拾取)
- 前端可查看分析进度/负面预警/回复推荐
"""
import csv
import io
import logging
import threading
from functools import wraps

from flask import Flask, request, jsonify, Response, render_template, stream_with_context

from config import COMMENT_TABLES
from db import (
    get_conn, list_tables, valid_table, get_columns,
    parse_filters, build_where, ensure_comment_columns,
)
from analyzer import analyzer_loop, get_status_dict

app = Flask(__name__, static_folder="static", template_folder="templates")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("app")

MAX_EXPORT_ROWS = 100000


def require_table(fn):
    @wraps(fn)
    def wrapper(table, *args, **kwargs):
        if not valid_table(table):
            return jsonify({"error": "非法表名"}), 400
        return fn(table, *args, **kwargs)
    return wrapper


# ---------- 页面 ----------
@app.route("/")
def index():
    return render_template("index.html")


# ---------- API ----------
@app.route("/api/tables")
def api_tables():
    tables = list_tables()
    groups = {}
    for t in tables:
        n = t["name"]
        if n == "crawler_cookies_account": g = "账号管理"
        elif n.startswith("platform_hot_search"): g = "跨平台热搜"
        elif n.startswith("bilibili"): g = "B站"
        elif n.startswith("douyin"): g = "抖音"
        elif n.startswith("kuaishou"): g = "快手"
        elif n.startswith("weibo"): g = "微博"
        elif n.startswith("xhs"): g = "小红书"
        elif n.startswith("zhihu"): g = "知乎"
        elif n.startswith("tieba"): g = "贴吧"
        elif n.startswith("reddit"): g = "Reddit"
        else: g = "其他"
        t["group"] = g
        groups.setdefault(g, []).append(n)
    return jsonify({"tables": tables, "groups": groups, "comment_tables": COMMENT_TABLES})


@app.route("/api/schema/<table>")
@require_table
def api_schema(table):
    return jsonify({"columns": get_columns(table)})


@app.route("/api/data/<table>")
@require_table
def api_data(table):
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(500, int(request.args.get("size", 50))))
    columns = get_columns(table)
    where_sql, params = build_where(columns, parse_filters(request.args))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM `{table}`{where_sql}", params)
            total = cur.fetchone()["c"]
            offset = (page - 1) * size
            col_sql = ", ".join(f"`{c['name']}`" for c in columns)
            sql = f"SELECT {col_sql} FROM `{table}`{where_sql} ORDER BY `id` LIMIT %s OFFSET %s"
            cur.execute(sql, params + [size, offset])
            rows = cur.fetchall()

    data_rows = [[("" if v is None else str(v)) for v in r.values()] for r in rows]
    return jsonify({
        "columns": [{"name": c["name"], "comment": c["comment"], "type": c["type"]} for c in columns],
        "rows": data_rows, "total": total, "page": page, "size": size,
        "pages": (total + size - 1) // size if total else 1,
    })


@app.route("/api/export/<table>")
@require_table
def api_export(table):
    columns = get_columns(table)
    where_sql, params = build_where(columns, parse_filters(request.args))

    def generate():
        yield "\ufeff"
        buf = io.StringIO()
        csv.writer(buf).writerow([c["name"] for c in columns])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        with get_conn() as conn:
            with conn.cursor() as cur:
                col_sql = ", ".join(f"`{c['name']}`" for c in columns)
                cur.execute(f"SELECT {col_sql} FROM `{table}`{where_sql} LIMIT %s", params + [MAX_EXPORT_ROWS])
                batch = cur.fetchmany(500)
                while batch:
                    for r in batch:
                        b = io.StringIO()
                        csv.writer(b).writerow(["" if v is None else (v if isinstance(v, str) else str(v)) for v in r.values()])
                        yield b.getvalue()
                    batch = cur.fetchmany(500)

    headers = {"Content-Disposition": f'attachment; filename="{table}.csv"', "Content-Type": "text/csv; charset=utf-8"}
    return Response(stream_with_context(generate()), headers=headers)


@app.route("/api/analyze/status")
def api_analyze_status():
    return jsonify(get_status_dict())


@app.route("/api/analyze/reset/<table>", methods=["POST"])
@require_table
def api_analyze_reset(table):
    """重置某表分析状态(analyzed/negative_alert 清零,reply_suggestion 置空),便于重跑"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE `{table}` SET `analyzed`=0, `negative_alert`=0, `reply_suggestion`=NULL")
        conn.commit()
    return jsonify({"reset": table, "affected": n})


def start_background():
    """启动后台分析守护线程 + 确保评论表字段就位"""
    added = ensure_comment_columns()
    if added:
        logger.info("已新增分析字段: %s", ", ".join(added))
    t = threading.Thread(target=analyzer_loop, name="analyzer", daemon=True)
    t.start()
    logger.info("评论 AI 分析守护线程已启动")


if __name__ == "__main__":
    start_background()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
