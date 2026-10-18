# -*- coding: utf-8 -*-
"""media_crawler_pro 数据库前端 + 评论 AI 分析
启动: py app.py  → http://127.0.0.1:5000
- 后台守护线程自动分析新增评论(analyzed=0 的会被拾取)
- 前端可查看分析进度/负面预警/回复推荐
"""
import csv
import io
import json
import logging
import time
import threading
from functools import wraps

from flask import Flask, request, jsonify, Response, render_template, stream_with_context

from config import COMMENT_TABLES
from db import (
    get_conn, list_tables, valid_table, get_columns,
    parse_filters, build_where, ensure_comment_columns, ensure_content_category_columns,
)
from analyzer import analyzer_loop, get_status_dict
from scrm import bp as scrm_bp, init_scrm

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 模板热更新,改 HTML 不必重启服务
app.register_blueprint(scrm_bp)
from auto_tasks import bp as auto_tasks_bp
app.register_blueprint(auto_tasks_bp)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("app")

MAX_EXPORT_ROWS = 100000
_EXPORT_SLOTS = threading.BoundedSemaphore(2)

# 侧边栏隐藏的表(仅前端不展示,不影响数据与后台分析)
HIDDEN_TABLES = {
    "platform_hot_search_content_rel",
    "platform_hot_search_item",
    "kuaishou_video",
    "kuaishou_video_comment",
    "tieba_comment",
    "tieba_note",
    "reddit_comment",
    "reddit_creator",
    "reddit_post",
}


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
    tables = [t for t in list_tables(refresh=True) if t["name"] not in HIDDEN_TABLES]
    groups = {}
    for t in tables:
        n = t["name"]
        if n in ("crawler_cookies_account", "平台运营账号", "UP主管理"): g = "账号管理"
        elif n.startswith("platform_hot_search"): g = "跨平台热搜"
        elif n.startswith("bilibili"): g = "B站"
        elif n.startswith("channels"): g = "视频号"
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

    if not _EXPORT_SLOTS.acquire(blocking=False):
        return jsonify(error="同时导出数量已达上限，请稍后重试"), 429
    release_lock = threading.Lock()
    released = False

    def release_slot():
        nonlocal released
        with release_lock:
            if not released:
                released = True
                _EXPORT_SLOTS.release()

    def generate():
        try:
            yield "\ufeff"
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([c["name"] for c in columns])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            # Close the cursor before its connection. PyMySQL drains remaining
            # packets without buffering them, including on client disconnect.
            with get_conn(stream=True) as conn:
                with conn.cursor() as cur:
                    col_sql = ", ".join(f"`{c['name']}`" for c in columns)
                    cur.execute(f"SELECT {col_sql} FROM `{table}`{where_sql} LIMIT %s", params + [MAX_EXPORT_ROWS])
                    batch = cur.fetchmany(500)
                    while batch:
                        for r in batch:
                            writer.writerow(["" if v is None else str(v) for v in r.values()])
                        yield buf.getvalue()
                        buf.seek(0); buf.truncate(0)
                        batch = cur.fetchmany(500)
        finally:
            release_slot()

    headers = {"Content-Disposition": f'attachment; filename="{table}.csv"', "Content-Type": "text/csv; charset=utf-8"}
    response = Response(stream_with_context(generate()), headers=headers)
    response.call_on_close(release_slot)
    return response


@app.route("/api/analyze/status")
def api_analyze_status():
    return jsonify(get_status_dict())


@app.route("/api/analyze/reset/<table>", methods=["POST"])
@require_table
def api_analyze_reset(table):
    """重置分析状态便于重跑(analyzed/negative_alert/purchase_intent 清零,reply_suggestion 置空)
    人工已处理(handled=1)的评论不受影响"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(
                f"UPDATE `{table}` SET `analyzed`=0, `negative_alert`=0, "
                f"`purchase_intent`=0, `reply_suggestion`=NULL WHERE IFNULL(`handled`,0)=0"
            )
        conn.commit()
    return jsonify({"reset": table, "affected": n})


def _parse_comment_payload():
    """校验评论修改请求,返回 (table, rid, error_response)"""
    body = request.get_json(silent=True) or {}
    table = body.get("table", "")
    if table not in COMMENT_TABLES:
        return None, None, (jsonify({"error": "非法评论表"}), 400)
    try:
        rid = int(body.get("id"))
    except (TypeError, ValueError):
        return None, None, (jsonify({"error": "非法记录 id"}), 400)
    return table, rid, None


def ensure_auto_task_table():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS `comment_auto_task` (
                  `id` BIGINT NOT NULL AUTO_INCREMENT,
                  `platform` VARCHAR(32) NOT NULL,
                  `src_table` VARCHAR(128) NOT NULL,
                  `src_pk` BIGINT NOT NULL,
                  `comment_id` VARCHAR(128) DEFAULT NULL,
                  `content_id` VARCHAR(128) DEFAULT NULL,
                  `account_nickname` VARCHAR(255) DEFAULT NULL,
                  `bit_window` VARCHAR(255) DEFAULT NULL,
                  `action_type` VARCHAR(16) NOT NULL COMMENT 'reply/delete',
                  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
                  `error_message` VARCHAR(1000) DEFAULT NULL,
                  `created_ts` BIGINT NOT NULL,
                  `started_ts` BIGINT DEFAULT NULL,
                  `finished_ts` BIGINT DEFAULT NULL,
                  PRIMARY KEY (`id`),
                  UNIQUE KEY `uk_comment_action` (`src_table`, `src_pk`, `action_type`),
                  KEY `idx_status_platform` (`status`, `platform`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论自动操作任务'
            """)
        conn.commit()

def ensure_zhihu_comment_url_column():
    """为知乎评论补齐来源帖子链接，并从知乎内容表回填。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                          WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='zhihu_comment'
                          AND COLUMN_NAME='content_url'""")
            if not cur.fetchone()['n']:
                cur.execute("ALTER TABLE `zhihu_comment` ADD COLUMN `content_url` VARCHAR(1000) NULL COMMENT '知乎帖子链接'")
            cur.execute("""UPDATE zhihu_comment c JOIN zhihu_content z ON z.content_id=c.content_id
                          SET c.content_url=z.content_url
                          WHERE (c.content_url IS NULL OR c.content_url='') AND z.content_url IS NOT NULL AND z.content_url<>''""")
        conn.commit()


def _normalize_account_name(name):
    return str(name or "").strip().replace("～", "").replace("~", "")


def _find_platform_bit_window(cur, platform, nickname):
    raw_name = str(nickname or "").strip()
    if not raw_name:
        return ""
    cur.execute("""
        SELECT `昵称`, `Bit窗口`
        FROM `平台运营账号`
        WHERE `平台`=%s AND (`昵称`=%s OR `昵称` LIKE %s OR %s LIKE CONCAT(`昵称`, '%%'))
        LIMIT 20
    """, (platform, raw_name, f"%{_normalize_account_name(raw_name)}%", raw_name))
    target = _normalize_account_name(raw_name)
    fallback = ""
    for account in cur.fetchall():
        bit_window = str(account.get("Bit窗口") or "").strip()
        if not bit_window:
            continue
        account_name = _normalize_account_name(account.get("昵称"))
        if account_name == target or account_name in target or target in account_name:
            return bit_window
        fallback = fallback or bit_window
    return fallback



def _get_bilibili_cookie_account(cur, nickname=""):
    raw_name = str(nickname or "").strip()
    target = _normalize_account_name(raw_name)
    if not target:
        return None
    cur.execute("""
        SELECT `昵称` AS account_name, `cookie` AS cookies, `Bit窗口` AS bit_window
        FROM `平台运营账号`
        WHERE `平台`='B站' AND COALESCE(`cookie`, '') <> ''
        ORDER BY (`昵称`=%s) DESC, `昵称`
    """, (raw_name,))
    for row in cur.fetchall():
        account_name = _normalize_account_name(row.get("account_name"))
        if account_name == target or account_name in target or target in account_name:
            return row
    return None


def _create_bilibili_auto_task(cur, rid):
    cur.execute("""
        SELECT c.id, c.comment_id, c.video_id, c.reviewed, c.handled, c.no_action,
               c.delete_requested, CAST(c.reply_suggestion AS CHAR) reply,
               v.nickname AS account_nickname, v.bvid, v.title
        FROM bilibili_video_comment c
        LEFT JOIN bilibili_video v ON v.video_id = c.video_id
        WHERE c.id=%s
    """, (rid,))
    row = cur.fetchone()
    if not row:
        return "skipped", "评论不存在"
    if row.get("handled") or row.get("no_action"):
        return "skipped", "已处理或无需操作"
    reply = str(row.get("reply") or "")
    if row.get("delete_requested"):
        action = "delete"
    elif row.get("reviewed") and reply and not reply.startswith("⚠ 分析失败"):
        action = "reply"
    else:
        return "skipped", "未达到自动操作条件"
    cookie_account = _get_bilibili_cookie_account(cur, row.get("account_nickname"))
    status = "pending" if cookie_account else "failed"
    error = None if cookie_account else "所属B站账号缺少cookie，请先给该账号配置Bit窗口并同步cookie"
    account_name = row.get("account_nickname")
    cur.execute("""
        INSERT INTO comment_auto_task
          (platform, src_table, src_pk, comment_id, content_id, account_nickname,
           bit_window, action_type, status, error_message, created_ts)
        VALUES ('B站', 'bilibili_video_comment', %s, %s, %s, %s, '', %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          account_nickname=VALUES(account_nickname),
          status=IF(status IN ('success','running'), status, VALUES(status)),
          error_message=IF(status IN ('success','running'), error_message, VALUES(error_message))
    """, (row["id"], row["comment_id"], row["video_id"], account_name,
          action, status, error, int(time.time())))
    return "created", action

def _create_douyin_auto_task(cur, rid):
    cur.execute("""
        SELECT c.id, c.comment_id, c.aweme_id, c.reviewed, c.handled, c.no_action,
               c.delete_requested, CAST(c.reply_suggestion AS CHAR) reply,
               a.nickname AS account_nickname, p.`Bit窗口` AS bit_window
        FROM douyin_aweme_comment c
        LEFT JOIN douyin_aweme a ON a.aweme_id = c.aweme_id
        LEFT JOIN `平台运营账号` p ON p.`平台`='抖音' AND p.`昵称`=a.nickname
        WHERE c.id=%s
    """, (rid,))
    row = cur.fetchone()
    if row and not str(row.get("bit_window") or "").strip():
        row["bit_window"] = _find_platform_bit_window(cur, "抖音", row.get("account_nickname"))
    if not row:
        return "skipped", "评论不存在"
    if row.get("handled") or row.get("no_action"):
        return "skipped", "已处理或无需操作"
    reply = str(row.get("reply") or "")
    if row.get("delete_requested"):
        action = "delete"
    elif row.get("reviewed") and reply and not reply.startswith("⚠ 分析失败"):
        action = "reply"
    else:
        return "skipped", "未达到自动操作条件"
    bit_window = str(row.get("bit_window") or "").strip()
    status = "pending" if bit_window else "failed"
    error = None if bit_window else "缺少Bit窗口"
    cur.execute("""
        INSERT INTO comment_auto_task
          (platform, src_table, src_pk, comment_id, content_id, account_nickname,
           bit_window, action_type, status, error_message, created_ts)
        VALUES ('抖音', 'douyin_aweme_comment', %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          bit_window=VALUES(bit_window),
          account_nickname=VALUES(account_nickname),
          status=IF(status IN ('success','running'), status, VALUES(status)),
          error_message=IF(status IN ('success','running'), error_message, VALUES(error_message))
    """, (row["id"], row["comment_id"], row["aweme_id"], row.get("account_nickname"),
          bit_window, action, status, error, int(time.time())))
    return "created", action

def _create_xhs_auto_task(cur, rid):
    cur.execute("""SELECT c.*, n.nickname AS account_name, p.`Bit窗口` AS account_bit
                   FROM xhs_note_comment c LEFT JOIN xhs_note n ON n.note_id=c.note_id
                   LEFT JOIN `平台运营账号` p ON p.`平台`='小红书' AND p.`昵称`=n.nickname WHERE c.id=%s""", (rid,))
    row = cur.fetchone()
    if not row: return "skipped", "评论不存在"
    if row.get("handled") or row.get("no_action"): return "skipped", "已处理或无需操作"
    reply = str(row.get("reply_suggestion") or "")
    if row.get("delete_requested"): action = "delete"
    elif row.get("reviewed") and reply and not reply.startswith("⚠ 分析失败"): action = "reply"
    else: return "skipped", "未达到自动操作条件"
    bit = str(row.get("account_bit") or "").strip()
    cur.execute("""INSERT INTO comment_auto_task
      (platform,src_table,src_pk,comment_id,content_id,account_nickname,bit_window,action_type,status,error_message,created_ts)
      VALUES ('小红书','xhs_note_comment',%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON DUPLICATE KEY UPDATE bit_window=VALUES(bit_window),status=IF(status IN ('success','running'),status,VALUES(status)),error_message=IF(status IN ('success','running'),error_message,VALUES(error_message))""",
      (row['id'],row.get('comment_id'),row.get('note_id'),row.get('account_name') or row.get('creator_hash'),bit,action,'pending' if bit else 'failed',None if bit else '缺少Bit窗口',int(time.time())))
    return "created", action

def _create_zhihu_auto_task(cur, rid):
    cur.execute("""SELECT c.*, z.content_url, p.`Bit窗口` AS account_bit FROM zhihu_comment c
                   LEFT JOIN zhihu_content z ON z.content_id=c.content_id
                   LEFT JOIN `平台运营账号` p ON p.`平台`='知乎' AND p.`昵称`=c.user_nickname WHERE c.id=%s""", (rid,))
    row = cur.fetchone()
    if not row: return "skipped", "评论不存在"
    if row.get("handled") or row.get("no_action"): return "skipped", "已处理或无需操作"
    reply = str(row.get("reply_suggestion") or "")
    if row.get("delete_requested"): action = "delete"
    elif row.get("reviewed") and reply and not reply.startswith("⚠ 分析失败"): action = "reply"
    else: return "skipped", "未达到自动操作条件"
    bit = str(row.get("account_bit") or "").strip()
    cur.execute("""INSERT INTO comment_auto_task
      (platform,src_table,src_pk,comment_id,content_id,account_nickname,bit_window,action_type,status,error_message,created_ts)
      VALUES ('知乎','zhihu_comment',%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON DUPLICATE KEY UPDATE bit_window=VALUES(bit_window),status=IF(status IN ('success','running'),status,VALUES(status)),error_message=IF(status IN ('success','running'),error_message,VALUES(error_message))""",
      (row['id'],row.get('comment_id'),row.get('content_id'),row.get('user_nickname'),bit,action,'pending' if bit else 'failed',None if bit else '缺少Bit窗口',int(time.time())))
    return "created", action


@app.route("/api/comment/handle", methods=["POST"])
def api_comment_handle():
    """标记/取消人工已处理。已处理评论守护进程不再分析,重跑也不会覆盖"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    handled = 1 if (request.get_json(silent=True) or {}).get("handled") else 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(
                f"UPDATE `{table}` SET `handled`=%s, `last_modify_ts`=UNIX_TIMESTAMP() WHERE `id`=%s",
                (handled, rid),
            )
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "handled": handled, "affected": n})


@app.route("/api/comment/review", methods=["POST"])
def api_comment_review():
    """标记/取消人工已审核。"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    reviewed = 1 if (request.get_json(silent=True) or {}).get("reviewed") else 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(
                f"UPDATE `{table}` SET `reviewed`=%s, `last_modify_ts`=UNIX_TIMESTAMP() WHERE `id`=%s",
                (reviewed, rid),
            )
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "reviewed": reviewed, "affected": n})


@app.route("/api/comment/delete-request", methods=["POST"])
def api_comment_delete_request():
    """设置/取消评论自动删除请求；实际平台删除由自动化 worker 执行。"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    requested = 1 if (request.get_json(silent=True) or {}).get("delete_requested") else 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(
                f"UPDATE `{table}` SET `delete_requested`=%s, `last_modify_ts`=UNIX_TIMESTAMP() WHERE `id`=%s",
                (requested, rid),
            )
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "delete_requested": requested, "affected": n})


@app.route("/api/comment/delete-accept", methods=["POST"])
def api_comment_delete_accept():
    """标注删除并置为已审核。实际平台删除由后续自动化能力执行。"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(
                f"UPDATE `{table}` SET `delete_requested`=1, `reviewed`=1, "
                f"`last_modify_ts`=UNIX_TIMESTAMP() WHERE `id`=%s",
                (rid,),
            )
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "delete_requested": 1,
                    "reviewed": 1, "affected": n})


@app.route("/api/comment/batch", methods=["POST"])
def api_comment_batch():
    """批量更新评论状态: intent/review/no-action/handle/delete-request。"""
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").strip()
    records = body.get("records") or []
    if action not in {"intent", "review", "no-action", "handle", "delete-request"}:
        return jsonify({"error": "非法批量操作"}), 400
    grouped = {}
    for item in records:
        table = item.get("table") if isinstance(item, dict) else None
        if table not in COMMENT_TABLES:
            return jsonify({"error": "包含非法评论表"}), 400
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"error": "包含非法记录 id"}), 400
        grouped.setdefault(table, set()).add(rid)
    if not grouped:
        return jsonify({"error": "请先选择评论"}), 400
    field = {
        "intent": "purchase_intent",
        "review": "reviewed",
        "no-action": "no_action",
        "handle": "handled",
        "delete-request": "delete_requested",
    }[action]
    value = 1 if body.get("value", 1) else 0
    affected = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table, ids in grouped.items():
                ph = ",".join(["%s"] * len(ids))
                affected += cur.execute(
                    f"UPDATE `{table}` SET `{field}`=%s, `last_modify_ts`=UNIX_TIMESTAMP() "
                    f"WHERE `id` IN ({ph})",
                    [value] + list(ids),
                )
        conn.commit()
    return jsonify({"ok": True, "action": action, "value": value,
                    "selected": sum(len(v) for v in grouped.values()), "affected": affected})


@app.route("/api/comment/auto-operate", methods=["POST"])
def api_comment_auto_operate():
    """生成自动操作任务。已支持抖音与 B 站，其他平台跳过。"""
    body = request.get_json(silent=True) or {}
    records = body.get("records") or []
    ensure_auto_task_table()
    selected = 0
    created = 0
    skipped = 0
    details = []
    created_task_ids = []
    for item in records:
        table = item.get("table") if isinstance(item, dict) else None
        if table not in COMMENT_TABLES:
            return jsonify({"error": "包含非法评论表"}), 400
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"error": "包含非法记录 id"}), 400
        selected += 1
        with get_conn() as conn:
            with conn.cursor() as cur:
                if table == "douyin_aweme_comment":
                    status, info = _create_douyin_auto_task(cur, rid)
                elif table == "bilibili_video_comment":
                    status, info = _create_bilibili_auto_task(cur, rid)
                elif table == "xhs_note_comment":
                    status, info = _create_xhs_auto_task(cur, rid)
                elif table == "zhihu_comment":
                    status, info = _create_zhihu_auto_task(cur, rid)
                else:
                    status, info = "skipped", "当前平台暂未接入自动操作"
            conn.commit()
        if status == "created":
            created += 1
            with get_conn() as task_conn:
                with task_conn.cursor() as task_cur:
                    task_cur.execute("SELECT id FROM comment_auto_task WHERE src_table=%s AND src_pk=%s", (table, rid))
                    task_row = task_cur.fetchone()
                    if task_row: created_task_ids.append(task_row['id'])
        else:
            skipped += 1
        details.append({"table": table, "id": rid, "status": status, "info": info})
    if not selected:
        return jsonify({"error": "请先选择评论"}), 400
    queued = 0
    for task_id in created_task_ids:
        try:
            from auto_tasks import launch_task
            queued += int(launch_task(task_id))
        except Exception as exc:
            logger.exception("自动任务 %s 启动失败: %s", task_id, exc)
    return jsonify({"ok": True, "selected": selected, "created": created,
                    "skipped": skipped, "queued": queued,
                    "waiting_for_retry": len(created_task_ids) - queued,
                    "details": details[:50]})


@app.route("/api/comment/update", methods=["POST"])
def api_comment_update():
    """人工修改分析字段:negative_alert/purchase_intent/reply_suggestion"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    sets, params = [], []
    for k in ("negative_alert", "purchase_intent"):
        if k in body:
            sets.append(f"`{k}`=%s")
            params.append(1 if body[k] else 0)
    if "reply_suggestion" in body:
        sets.append("`reply_suggestion`=%s")
        params.append(str(body["reply_suggestion"] or "")[:1000])
    if not sets:
        return jsonify({"error": "没有可更新的分析字段"}), 400
    sets.append("`last_modify_ts`=UNIX_TIMESTAMP()")
    params.append(rid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE `{table}` SET {', '.join(sets)} WHERE `id`=%s", params)
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "affected": n})


@app.route("/api/comment/calibration", methods=["POST"])
def api_comment_calibration():
    """保存人工校准结果，保留模型原判断并允许工作台统计。"""
    table, rid, err = _parse_comment_payload()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    reason = str(body.get("correction_reason") or "").strip()[:64]
    if not reason:
        return jsonify({"error": "请选择校准原因"}), 400
    intents = body.get("corrected_intents", body.get("intents", []))
    if not isinstance(intents, list):
        intents = [str(intents)] if intents else []
    model_intents = body.get("model_intents", [])
    if not isinstance(model_intents, list):
        model_intents = [str(model_intents)] if model_intents else []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO scrm_comment_calibration
              (src_table,src_pk,model_intents,corrected_intents,model_sentiment,corrected_sentiment,
               model_high_negative,corrected_high_negative,model_emotion_target,corrected_emotion_target,
               correction_reason,correction_note,context_title,context_parent_comment,created_ts)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UNIX_TIMESTAMP())
              ON DUPLICATE KEY UPDATE model_intents=VALUES(model_intents), corrected_intents=VALUES(corrected_intents),
               model_sentiment=VALUES(model_sentiment), corrected_sentiment=VALUES(corrected_sentiment),
               model_high_negative=VALUES(model_high_negative), corrected_high_negative=VALUES(corrected_high_negative),
               model_emotion_target=VALUES(model_emotion_target), corrected_emotion_target=VALUES(corrected_emotion_target),
               correction_reason=VALUES(correction_reason), correction_note=VALUES(correction_note),
               context_title=VALUES(context_title), context_parent_comment=VALUES(context_parent_comment), created_ts=UNIX_TIMESTAMP()""",
              (table, rid, json.dumps(model_intents, ensure_ascii=False), json.dumps(intents, ensure_ascii=False),
               str(body.get("model_sentiment") or "")[:32], str(body.get("corrected_sentiment") or "")[:32],
               1 if body.get("model_high_negative") else 0, 1 if body.get("corrected_high_negative") else 0,
               str(body.get("model_emotion_target") or "")[:255], str(body.get("corrected_emotion_target") or "")[:255],
               reason, str(body.get("correction_note") or "")[:500], str(body.get("context_title") or "")[:500],
               str(body.get("context_parent_comment") or "")))
        conn.commit()
    return jsonify({"ok": True, "table": table, "id": rid, "reason": reason})


def start_background():
    """启动后台分析守护线程 + 确保评论表字段就位"""
    init_scrm()
    ensure_zhihu_comment_url_column()
    added = ensure_comment_columns()
    if added:
        logger.info("已新增分析字段: %s", ", ".join(added))
    content_added = ensure_content_category_columns()
    if content_added:
        logger.info("已新增视频分类字段: %s", ", ".join(content_added))
    t = threading.Thread(target=analyzer_loop, name="analyzer", daemon=True)
    t.start()
    logger.info("评论 AI 分析守护线程已启动")


if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
