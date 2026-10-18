# -*- coding: utf-8 -*-
"""数据库工具:连接、表/列查询、筛选构建、评论表字段迁移"""
import json
import logging
import pymysql

from config import DB_CONFIG, COMMENT_TABLES, COMMENT_NEW_COLUMNS, CONTENT_TABLES

logger = logging.getLogger("db")


def get_conn(*, stream=False):
    options = dict(DB_CONFIG)
    if stream:
        options['cursorclass'] = pymysql.cursors.SSDictCursor
    return pymysql.connect(**options)


# ---------- 表名白名单缓存 ----------
_TABLES_CACHE = None


def list_tables(refresh=False):
    global _TABLES_CACHE
    if _TABLES_CACHE is not None and not refresh:
        return _TABLES_CACHE
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT TABLE_NAME AS name, TABLE_COMMENT AS comment, TABLE_ROWS AS `rows`
                   FROM information_schema.TABLES
                   WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME""",
                (DB_CONFIG["db"],),
            )
            _TABLES_CACHE = cur.fetchall()
    return _TABLES_CACHE


def valid_table(name):
    return any(t["name"] == name for t in list_tables())


def get_columns(table):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, COLUMN_COMMENT AS comment,
                          IS_NULLABLE AS nullable, COLUMN_KEY AS `key`,
                          COLUMN_DEFAULT AS `default`, EXTRA AS extra
                   FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                   ORDER BY ORDINAL_POSITION""",
                (DB_CONFIG["db"], table),
            )
            return cur.fetchall()


def parse_filters(args):
    """从 ?filters=JSON 或 ?f_col=val 解析 {col: val}"""
    filters = {}
    raw = args.get("filters")
    if raw:
        try:
            filters = json.loads(raw)
        except Exception:
            filters = {}
    for k, v in args.items():
        if k.startswith("f_") and v != "":
            filters[k[2:]] = v
    return {k: v for k, v in filters.items() if v not in (None, "")}


def build_where(columns, filters):
    """构建 WHERE 子句,列名白名单校验,值 LIKE 匹配"""
    col_names = {c["name"] for c in columns}
    clauses, params = [], []
    for k, v in filters.items():
        if k not in col_names:
            continue
        clauses.append("CAST(`%s` AS CHAR) LIKE %%s" % k)
        params.append(f"%{v}%")
    if clauses:
        return " WHERE " + " AND ".join(clauses), params
    return "", []


def ensure_comment_columns():
    """给所有评论表加 analyzed/negative_alert/reply_suggestion,幂等"""
    added = []
    for table in COMMENT_TABLES:
        try:
            existing = {c["name"] for c in get_columns(table)}
        except Exception as e:
            logger.warning("查询 %s 列失败: %s", table, e)
            continue
        for col, defn in COMMENT_NEW_COLUMNS:
            if col in existing:
                continue
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {defn}")
                    conn.commit()
                added.append(f"{table}.{col}")
                logger.info("已为 %s 添加列 %s", table, col)
            except Exception as e:
                logger.error("为 %s 添加列 %s 失败: %s", table, col, e)
    return added


def ensure_content_category_columns():
    """为所有内容/视频表补齐分类和原链接字段。"""
    added = []
    for table in CONTENT_TABLES:
        try:
            existing = {c["name"] for c in get_columns(table)}
        except Exception as e:
            logger.warning("查询内容表 %s 列失败: %s", table, e)
            continue
        missing = []
        if "category" not in existing:
            missing.append("ADD COLUMN `category` VARCHAR(16) NOT NULL DEFAULT '原创' COMMENT '视频分类:商单/原创'")
        if "original_url" not in existing:
            missing.append("ADD COLUMN `original_url` VARCHAR(500) NULL COMMENT '原链接'")
        if not missing:
            continue
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    for statement in missing:
                        cur.execute(f"ALTER TABLE `{table}` {statement}")
                conn.commit()
            added.extend(f"{table}.{name}" for name in ("category", "original_url") if name not in existing)
        except Exception as e:
            logger.error("为内容表 %s 添加 category 失败: %s", table, e)
    return added
