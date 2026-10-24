"""跨平台评论与内容的只读 SQL 片段。

保持此模块无 Flask、无数据库连接；调用方负责执行 SQL 和处理 HTTP 参数。
"""
import time

from scrm_components.platforms import COMMENT_TABLES_ENABLED, PLATFORM_MAP, PLATFORM_OF


def comment_fragment(table):
    mapping = PLATFORM_MAP[table]
    return (
        f"SELECT `id` AS src_pk, '{table}' AS src_table, `{mapping['nick']}` AS nickname, "
        f"`creator_hash` AS creator_hash, CAST(`comment_id` AS CHAR) AS comment_id, "
        f"CAST(`parent_comment_id` AS CHAR) AS parent_comment_id, "
        f"`{mapping['cid']}` AS content_id, CAST(`content` AS CHAR) AS content, "
        f"CAST({mapping['time']} AS UNSIGNED) AS ctime, `add_ts` AS add_ts, "
        f"CAST({mapping['likes']} AS CHAR) AS likes, `analyzed`, `negative_alert` AS neg, "
        f"`purchase_intent` AS intent, CAST(`comment_type` AS CHAR) AS comment_type, `high_negative` AS highneg, `human_review`, `handled`, `no_action`, `reviewed`, "
        f"`delete_requested` AS delreq, CAST(`reply_suggestion` AS CHAR) AS reply FROM `{table}`"
    )


def comments_union(alias="U"):
    return "(" + " UNION ALL ".join(comment_fragment(table) for table in COMMENT_TABLES_ENABLED) + f") {alias}"


def content_fragment(table):
    mapping = PLATFORM_MAP[table]
    update_column = {"xhs_note_comment": "last_update_time", "zhihu_comment": "updated_time"}.get(table, "last_modify_ts")
    update_expression = f"COALESCE(NULLIF(`{update_column}`, 0), NULLIF(`last_modify_ts`, 0), `add_ts`)"
    return (
        f"SELECT '{table}' AS src_table, '{mapping['platform']}' AS platform, `{mapping['cid']}` AS content_id, "
        f"CAST(`{mapping['ctitle']}` AS CHAR) AS title, CAST({mapping['curl']} AS CHAR) AS url, "
        f"CAST({mapping.get('ccover', 'NULL')} AS CHAR) AS cover_url, CAST(`product_name` AS CHAR) AS product_name, "
        f"COALESCE(`product_reviewed`, 0) AS product_reviewed, "
        f"{mapping['cnick']} AS upname, COALESCE(NULLIF(`category`, ''), '原创') AS category, "
        f"CASE WHEN add_ts > 100000000000 THEN ROUND(add_ts/1000) ELSE add_ts END AS add_ts, "
        f"CASE WHEN ({update_expression}) > 100000000000 THEN ROUND(({update_expression})/1000) ELSE ({update_expression}) END AS last_update_ts, "
        f"CAST({mapping['cview']} AS CHAR) AS view_count, CAST({mapping['clike']} AS CHAR) AS like_count, "
        f"CAST({mapping['ccollect']} AS CHAR) AS collect_count, CAST({mapping['cshare']} AS CHAR) AS share_count, "
        f"CAST({mapping['ccomment']} AS CHAR) AS comment_count, CAST({mapping['cduration']} AS CHAR) AS duration, "
        f"CASE WHEN {mapping['cpubtime']} > 100000000000 THEN ROUND({mapping['cpubtime']}/1000) "
        f"WHEN {mapping['cpubtime']} > 0 THEN {mapping['cpubtime']} ELSE NULL END AS publish_time, "
        f"'{mapping['ctable']}' AS ctable FROM `{mapping['ctable']}`"
    )


def contents_union():
    return "(" + " UNION ALL ".join(content_fragment(table) for table in COMMENT_TABLES_ENABLED) + ") V"


def comment_filters(args, alias="U", now=None):
    """Translate supported comment-list filters to a parameterized WHERE clause."""
    clauses, params = [], []
    platform = args.get("platform", "")
    if platform:
        matched = [table for table in COMMENT_TABLES_ENABLED if PLATFORM_OF[table] == platform]
        if matched:
            clauses.append(f"{alias}.src_table IN ({','.join(['%s'] * len(matched))})")
            params.extend(matched)
    for key, column in (("q", "content"), ("user", "nickname")):
        value = (args.get(key) or "").strip()
        if value:
            clauses.append(f"{alias}.{column} LIKE %s")
            params.append(f"%{value}%")
    author = (args.get("author") or "").strip()
    if author:
        author_clauses = []
        for table in COMMENT_TABLES_ENABLED:
            mapping = PLATFORM_MAP[table]
            author_clauses.append(f"({alias}.src_table=%s AND EXISTS (SELECT 1 FROM `{mapping['ctable']}` WHERE `{mapping['cid']}`={alias}.content_id AND {mapping['cnick']} LIKE %s))")
            params.extend([table, f"%{author}%"])
        clauses.append("(" + " OR ".join(author_clauses) + ")" if author_clauses else "1=0")
    video_id = (args.get("vid") or "").strip()
    if video_id:
        clauses.append(f"{alias}.content_id = %s")
        params.append(video_id)
    self_reply = args.get("is_self", "")
    if self_reply in {"0", "1"}:
        # 本人回复：评论人昵称与其所属内容作者昵称一致（NULL 安全比较）。
        per_table = []
        for table in COMMENT_TABLES_ENABLED:
            mapping = PLATFORM_MAP[table]
            equal = (f"TRIM({alias}.nickname) <=> TRIM((SELECT {mapping['cnick']} FROM `{mapping['ctable']}` "
                     f"WHERE `{mapping['cid']}`={alias}.content_id))")
            per_table.append(f"({alias}.src_table=%s AND {'NOT ' if self_reply == '0' else ''}{equal})")
        clauses.append("(" + " OR ".join(per_table) + ")")
        params.extend(COMMENT_TABLES_ENABLED)
    sentiment = args.get("sentiment", "")
    if sentiment == "negative": clauses.append(f"{alias}.neg=1")
    elif sentiment == "positive": clauses.append(f"{alias}.analyzed=1 AND {alias}.neg=0")
    intent = args.get("intent", "")
    if intent == "1": clauses.append(f"{alias}.intent=1")
    elif intent == "0": clauses.append(f"{alias}.analyzed=1 AND {alias}.intent=0")
    for key, column in (("handled", "handled"), ("reviewed", "reviewed")):
        value = args.get(key, "")
        if value in {"0", "1"}: clauses.append(f"{alias}.{column}={value}")
    status = args.get("status", "")
    status_sql = {
        "pending_analysis": f"{alias}.analyzed=0", "analyzed": f"{alias}.analyzed=1", "intent": f"{alias}.intent=1",
        "reply_ready": f"{alias}.reply IS NOT NULL AND {alias}.reply != '' AND {alias}.reply NOT LIKE '⚠ 分析失败%%'",
        "analysis_failed": f"{alias}.reply LIKE '⚠ 分析失败%%'", "reviewed": f"{alias}.reviewed=1",
        "unreviewed": f"{alias}.reviewed=0", "handled": f"{alias}.handled=1", "unhandled": f"{alias}.handled=0",
        "no_action": f"{alias}.no_action=1", "delete_requested": f"{alias}.delreq=1", "human_review": f"{alias}.human_review=1",
    }
    if status in status_sql: clauses.append(status_sql[status])
    days = args.get("days", "")
    if days.isdigit() and int(days) > 0:
        clauses.append(f"{alias}.ctime >= %s")
        params.append(int(time.time() if now is None else now) - int(days) * 86400)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params
