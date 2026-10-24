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
    """为所有内容/视频表补齐分类、原链接和产品名称字段。"""
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
        if "product_name" not in existing:
            missing.append("ADD COLUMN `product_name` VARCHAR(512) NULL COMMENT '产品名称: 品牌+产品+型号'")
        if "product_checked_ts" not in existing:
            missing.append("ADD COLUMN `product_checked_ts` BIGINT NULL COMMENT '原创产品 OCR 识别时间'")
        if "product_reviewed" not in existing:
            missing.append("ADD COLUMN `product_reviewed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '原创产品名称是否已人工审核'")
        if not missing:
            continue
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    for statement in missing:
                        cur.execute(f"ALTER TABLE `{table}` {statement}")
                conn.commit()
            added.extend(f"{table}.{name}" for name in ("category", "original_url", "product_name", "product_checked_ts", "product_reviewed") if name not in existing)
        except Exception as e:
            logger.error("为内容表 %s 添加 category 失败: %s", table, e)
    return added


def mark_self_replies_handled():
    """将评论人等于内容作者的本人回复自动标记为已处理，返回本次更新数。"""
    from scrm_components.platforms import COMMENT_TABLES_ENABLED, PLATFORM_MAP

    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in COMMENT_TABLES_ENABLED:
                mapping = PLATFORM_MAP[table]
                # 内容表表达式中的列需要带 v. 前缀，避免 JOIN 中与评论表同名列歧义。
                author_expr = (mapping["cnick"].replace("`nickname`", "v.`nickname`")
                               .replace("`user_nickname`", "v.`user_nickname`")
                               .replace("creator_hash", "v.`creator_hash`"))
                try:
                    total += cur.execute(
                        f"UPDATE `{table}` c JOIN `{mapping['ctable']}` v "
                        f"ON c.`{mapping['cid']}`=v.`{mapping['cid']}` "
                        f"SET c.`handled`=1 "
                        f"WHERE IFNULL(c.`handled`,0)=0 "
                        f"AND NULLIF(TRIM(CAST(c.`{mapping['nick']}` AS CHAR)), '') IS NOT NULL "
                        f"AND TRIM(CAST(c.`{mapping['nick']}` AS CHAR))="
                        f"TRIM(CAST({author_expr} AS CHAR))"
                    )
                except Exception as exc:
                    logger.warning("标记 %s 本人回复为已处理失败: %s", table, exc)
        conn.commit()
    return total
