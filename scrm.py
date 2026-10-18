# -*- coding: utf-8 -*-
"""SCRM + 舆论监控 MVP 蓝图
- 页面路由:/scrm/... 渲染 templates/scrm/*.html
- 聚合 API:/scrm/api/... 跨 9 张评论表 / 内容表做 UNION 聚合
- 新实体(线索跟进、品牌触达、人设 Skill)落库到 scrm_ 前缀的轻量表
"""
import re
import json
import os
import time
import logging
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template, request

import requests
from pymysql.err import IntegrityError

from db import get_conn
from config import (
    COMMENT_TABLES, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_INQUIRY_TABLE,
)
from analyzer import call_deepseek, mark_analyzed
from comment_paging import thread_roots_sql, thread_page_sql

logger = logging.getLogger("scrm")
bp = Blueprint("scrm", __name__, url_prefix="/scrm")

# ---------- 平台 / 表映射 ----------
# cid=评论表中关联内容的业务ID列, nick=评论人昵称列, time=时间戳表达式, likes=点赞表达式
# ctitle/curl=内容表标题/链接列, cnick=内容表作者昵称表达式
# cview/clike/ccollect/cshare=内容表浏览/点赞/收藏/分享列(无则 NULL), cpubtime=内容发布时间表达式(秒/毫秒自动归一)
MAP = {
    "bilibili_video_comment": dict(platform="B站", ctable="bilibili_video", cid="video_id",
                                   nick="nickname", time="create_time", likes="like_count",
                                   ctitle="title", curl="video_url",
                                   cnick="`nickname`",
                                   ccomment="video_comment",
                                   cduration="duration",
                                   cview="video_play_count", clike="liked_count", ccollect="collected_count", cshare="NULL", cpubtime="create_time"),
    "channels_video_comment": dict(platform="视频号", ctable="channels_video", cid="video_id",
                                   nick="nickname", time="create_time", likes="like_count",
                                   ctitle="title",
                                   curl="CONCAT('https://channels.weixin.qq.com/web/pages/feed?eid=', REPLACE(COALESCE(NULLIF(`export_id`, ''), `video_id`), '/', '%%2F'))",
                                   cnick="CONCAT('视频号创作者#', LEFT(creator_hash, 6))",
                                   ccomment="comment_count",
                                   cduration="video_play_len",
                                   cview="read_count", clike="like_count", ccollect="fav_count", cshare="forward_count", cpubtime="create_time"),
    "douyin_aweme_comment": dict(platform="抖音", ctable="douyin_aweme", cid="aweme_id",
                                 nick="nickname", time="create_time", likes="like_count",
                                 ctitle="title", curl="aweme_url",
                                 cnick="`nickname`",
                                 ccomment="comment_count",
                                 cduration="NULL",
                                 cview="NULL", clike="liked_count", ccollect="collected_count", cshare="share_count", cpubtime="create_time"),
    "kuaishou_video_comment": dict(platform="快手", ctable="kuaishou_video", cid="video_id",
                                   nick="nickname", time="create_time", likes="like_count",
                                   ctitle="title", curl="video_url",
                                   cnick="`nickname`",
                                   ccomment="NULL",
                                   cduration="NULL",
                                   cview="viewd_count", clike="liked_count", ccollect="NULL", cshare="NULL", cpubtime="create_time"),
    "weibo_note_comment": dict(platform="微博", ctable="weibo_note", cid="note_id",
                               nick="nickname", time="create_time", likes="like_count",
                               ctitle="content", curl="note_url",
                               cnick="`nickname`",
                               ccomment="comments_count",
                               cduration="NULL",
                               cview="NULL", clike="liked_count", ccollect="NULL", cshare="shared_count", cpubtime="create_time"),
    "xhs_note_comment": dict(platform="小红书", ctable="xhs_note", cid="note_id",
                             nick="nickname", time="create_time", likes="like_count",
                             ctitle="title", curl="note_url",
                             cnick="`nickname`",
                             ccomment="comment_count",
                             cduration="NULL",
                             cview="NULL", clike="liked_count", ccollect="collected_count", cshare="share_count", cpubtime="time"),
    "zhihu_comment": dict(platform="知乎", ctable="zhihu_content", cid="content_id",
                          nick="user_nickname", time="add_ts", likes="like_count",
                          ctitle="title", curl="content_url",
                          cnick="`user_nickname`",
                          ccomment="comment_count",
                          cduration="NULL",
                          cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="created_time"),
    "tieba_comment": dict(platform="贴吧", ctable="tieba_note", cid="note_id",
                          nick="user_nickname", time="add_ts", likes="0",
                          ctitle="title", curl="note_url",
                          cnick="`user_nickname`",
                          ccomment="total_replay_num",
                          cduration="NULL",
                          cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="publish_time"),
    "reddit_comment": dict(platform="Reddit", ctable="reddit_post", cid="post_id",
                           nick="nickname", time="add_ts", likes="score",
                           ctitle="title", curl="post_url",
                           cnick="CONCAT('reddit#', LEFT(creator_hash, 6))",
                           ccomment="comment_count",
                           cduration="NULL",
                           cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="created_utc"),
}
# 只聚合配置中已启用且在映射里的评论表
TABLES = [t for t in COMMENT_TABLES if t in MAP]
PLATFORM_OF = {t: MAP[t]["platform"] for t in TABLES}

LEAD_STATUS = [
    ("new", "待联系", "#6b7280"),
    ("contacted", "已联系", "#4f46e5"),
    ("following", "跟进中", "#0d9488"),
    ("feedback", "客户反馈", "#d97706"),
    ("won", "已成交", "#16a34a"),
    ("lost", "已流失", "#9ca3af"),
]
LEAD_STATUS_MAP = {s[0]: s[1] for s in LEAD_STATUS}
BRAND_STATUS = [
    ("new", "新品牌", "#6b7280"),
    ("touching", "已触达", "#4f46e5"),
    ("intent", "有意向", "#d97706"),
    ("cooperate", "合作中", "#16a34a"),
    ("lost", "暂未合作", "#9ca3af"),
]
BRAND_STATUS_MAP = {s[0]: s[1] for s in BRAND_STATUS}
TEAM = ["希乙", "路飞", "晓敏", "陈晨"]
CREATOR_TABLE = "`UP主管理`"

KPI_PLATFORMS = ["B站", "抖音", "视频号", "小红书", "快手", "微博", "知乎"]


def _calc_kpi(amount, cpme, cpe):
    """按商单金额和平台单价计算 KPI，播放取万位、互动取百位。"""
    try:
        amount, cpme, cpe = Decimal(str(amount)), Decimal(str(cpme)), Decimal(str(cpe))
        views = ((amount / cpme * Decimal(1000)) / Decimal(10000)).to_integral_value(rounding=ROUND_DOWN) * Decimal(10000) if cpme > 0 else None
        interactions = ((amount / cpe) / Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP) * Decimal(100) if cpe > 0 else None
        return (int(views) if views is not None else None,
                int(interactions) if interactions is not None else None)
    except (TypeError, ValueError, ArithmeticError):
        return None, None


def _metric_number(value):
    """把内容表中的数值/带万单位文本转换为可比较的整数。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([万wWkK]?)", text)
    if not m:
        return None
    mult = {"": 1, "万": 10000, "w": 10000, "W": 10000, "k": 1000, "K": 1000}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _to_epoch(value):
    try:
        ts = int(float(value)) if value not in (None, "") else None
        if ts and ts > 100000000000:
            ts //= 1000
        return ts
    except (TypeError, ValueError):
        return None


def _split_platform_accounts(raw):
    """解析询单中的平台账户文本，例如“家电宅老炮(B站)”。"""
    if not raw:
        return []
    result = []
    platforms = set(KPI_PLATFORMS)
    for item in re.split(r"[,，;；\n]+", str(raw)):
        item = item.strip()
        if not item:
            continue
        match = re.match(r"^(.+?)\s*[（(]\s*([^）)]+)\s*[）)]$", item)
        if match and match.group(2).strip() in platforms:
            result.append((match.group(2).strip(), match.group(1).strip()))
            continue
        match = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", item)
        if match and match.group(1).strip() in platforms:
            result.append((match.group(1).strip(), match.group(2).strip()))
            continue
        match = re.match(r"^(.+?)\s*[-—_]\s*(B站|抖音|视频号|小红书|快手|微博|知乎)$", item)
        if match:
            result.append((match.group(2).strip(), match.group(1).strip()))
            continue
        result.append((None, item))
    return result


# ---------- 跨表 UNION 构造 ----------
def _comment_frag(t):
    m = MAP[t]
    return (
        f"SELECT `id` AS src_pk, '{t}' AS src_table, `{m['nick']}` AS nickname, "
        f"`creator_hash` AS creator_hash, CAST(`comment_id` AS CHAR) AS comment_id, "
        f"CAST(`parent_comment_id` AS CHAR) AS parent_comment_id, "
        f"`{m['cid']}` AS content_id, CAST(`content` AS CHAR) AS content, "
        f"CAST({m['time']} AS UNSIGNED) AS ctime, `add_ts` AS add_ts, "
        f"CAST({m['likes']} AS CHAR) AS likes, "
        f"`analyzed`, `negative_alert` AS neg, `purchase_intent` AS intent, "
        f"`handled`, `no_action`, `reviewed`, `delete_requested` AS delreq, "
        f"CAST(`reply_suggestion` AS CHAR) AS reply "
        f"FROM `{t}`"
    )


def _union(alias="U"):
    return "(" + " UNION ALL ".join(_comment_frag(t) for t in TABLES) + f") {alias}"


def _content_frag(t):
    m = MAP[t]
    update_col = {
        "xhs_note_comment": "last_update_time",
        "zhihu_comment": "updated_time",
    }.get(t, "last_modify_ts")
    # 内容表更新时间字段各平台略有差异;统一成秒级时间戳供账号统计使用。
    update_expr = f"COALESCE(NULLIF(`{update_col}`, 0), NULLIF(`last_modify_ts`, 0), `add_ts`)"
    # add_ts 部分平台为毫秒,统一归一化为秒;浏览/点赞/收藏用 varchar 兼容带单位字符串;
    # 发布时间可能是秒或毫秒,同样归一化
    return (
        f"SELECT '{t}' AS src_table, '{m['platform']}' AS platform, "
        f"`{m['cid']}` AS content_id, CAST(`{m['ctitle']}` AS CHAR) AS title, "
        f"CAST({m['curl']} AS CHAR) AS url, {m['cnick']} AS upname, "
        f"COALESCE(NULLIF(`category`, ''), '原创') AS category, "
        f"CASE WHEN add_ts > 100000000000 THEN ROUND(add_ts/1000) ELSE add_ts END AS add_ts, "
        f"CASE WHEN ({update_expr}) > 100000000000 THEN ROUND(({update_expr})/1000) ELSE ({update_expr}) END AS last_update_ts, "
        f"CAST({m['cview']} AS CHAR) AS view_count, "
        f"CAST({m['clike']} AS CHAR) AS like_count, "
        f"CAST({m['ccollect']} AS CHAR) AS collect_count, "
        f"CAST({m['cshare']} AS CHAR) AS share_count, "
        f"CAST({m['ccomment']} AS CHAR) AS comment_count, "
        f"CAST({m['cduration']} AS CHAR) AS duration, "
        f"CASE WHEN {m['cpubtime']} > 100000000000 THEN ROUND({m['cpubtime']}/1000) "
        f"WHEN {m['cpubtime']} > 0 THEN {m['cpubtime']} ELSE NULL END AS publish_time, "
        f"'{m['ctable']}' AS ctable "
        f"FROM `{m['ctable']}`"
    )


def _content_union():
    return "(" + " UNION ALL ".join(_content_frag(t) for t in TABLES) + ") V"


def _apply_comment_filters(args, alias="U"):
    """评论类查询统一过滤,返回 (where_sql, params)"""
    clauses, params = [], []
    platform = args.get("platform", "")
    if platform:
        matched = [t for t in TABLES if PLATFORM_OF[t] == platform]
        if matched:
            clauses.append(f"{alias}.src_table IN (%s)" % ",".join(["%s"] * len(matched)))
            params.extend(matched)
    q = (args.get("q") or "").strip()
    if q:
        clauses.append(f"{alias}.content LIKE %s")
        params.append(f"%{q}%")
    user = (args.get("user") or "").strip()
    if user:
        clauses.append(f"{alias}.nickname LIKE %s")
        params.append(f"%{user}%")
    author = (args.get("author") or "").strip()
    if author:
        author_clauses = []
        for t in TABLES:
            m = MAP[t]
            author_clauses.append(
                f"({alias}.src_table=%s AND EXISTS (SELECT 1 FROM `{m['ctable']}` "
                f"WHERE `{m['cid']}`={alias}.content_id AND {m['cnick']} LIKE %s))"
            )
            params.extend([t, f"%{author}%"])
        clauses.append("(" + " OR ".join(author_clauses) + ")" if author_clauses else "1=0")
    vid = (args.get("vid") or "").strip()
    if vid:
        clauses.append(f"{alias}.content_id = %s")
        params.append(vid)
    sent = args.get("sentiment", "")
    if sent == "negative":
        clauses.append(f"{alias}.neg=1")
    elif sent == "positive":
        clauses.append(f"{alias}.analyzed=1 AND {alias}.neg=0")
    intent_filter = args.get("intent", "")
    if intent_filter == "1":
        clauses.append(f"{alias}.intent=1")
    elif intent_filter == "0":
        clauses.append(f"{alias}.analyzed=1 AND {alias}.intent=0")
    handled = args.get("handled", "")
    if handled == "1":
        clauses.append(f"{alias}.handled=1")
    elif handled == "0":
        clauses.append(f"{alias}.handled=0")
    reviewed = args.get("reviewed", "")
    if reviewed == "1":
        clauses.append(f"{alias}.reviewed=1")
    elif reviewed == "0":
        clauses.append(f"{alias}.reviewed=0")
    status = args.get("status", "")
    if status == "pending_analysis":
        clauses.append(f"{alias}.analyzed=0")
    elif status == "analyzed":
        clauses.append(f"{alias}.analyzed=1")
    elif status == "intent":
        clauses.append(f"{alias}.intent=1")
    elif status == "reply_ready":
        clauses.append(f"{alias}.reply IS NOT NULL AND {alias}.reply != '' AND {alias}.reply NOT LIKE '⚠ 分析失败%%'")
    elif status == "analysis_failed":
        clauses.append(f"{alias}.reply LIKE '⚠ 分析失败%%'")
    elif status == "reviewed":
        clauses.append(f"{alias}.reviewed=1")
    elif status == "unreviewed":
        clauses.append(f"{alias}.reviewed=0")
    elif status == "handled":
        clauses.append(f"{alias}.handled=1")
    elif status == "unhandled":
        clauses.append(f"{alias}.handled=0")
    elif status == "no_action":
        clauses.append(f"{alias}.no_action=1")
    elif status == "delete_requested":
        clauses.append(f"{alias}.delreq=1")
    days = args.get("days", "")
    if days.isdigit() and int(days) > 0:
        clauses.append(f"{alias}.ctime >= %s")
        params.append(int(time.time()) - int(days) * 86400)
    sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def _with_platform(rows):
    for r in rows or []:
        r["platform"] = PLATFORM_OF.get(r.get("src_table"), "?")
    return rows


def _load_contents(cur, rows):
    """按来源表分组批量补充所属内容标题、链接和原作者昵称。"""
    grouped = {}
    for r in rows or []:
        cid = str(r.get("content_id") or "")
        if cid:
            grouped.setdefault(r["src_table"], set()).add(cid)
    info = {}
    for t, ids in grouped.items():
        m = MAP[t]
        try:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT `{m['cid']}` cid, CAST(`{m['ctitle']}` AS CHAR) title, "
                f"CAST({m['curl']} AS CHAR) url, {m['cnick']} AS author_nickname, "
                f"COALESCE(NULLIF(`category`, ''), '原创') AS video_category "
                f"FROM `{m['ctable']}` WHERE `{m['cid']}` IN ({ph})", list(ids))
            for x in cur.fetchall():
                info[(t, str(x["cid"]))] = x
        except Exception:
            pass
    for r in rows or []:
        x = info.get((r.get("src_table"), str(r.get("content_id") or "")))
        r["video_title"] = x["title"] if x else None
        r["video_url"] = x["url"] if x else None
        r["author_nickname"] = x.get("author_nickname") if x else None
        r["video_category"] = x.get("video_category") if x else None
    return rows


def _table_required(fn):
    @wraps(fn)
    def wrapper(table, *args, **kwargs):
        if table not in MAP:
            return jsonify({"error": "非法表名"}), 400
        return fn(table, *args, **kwargs)
    return wrapper


# ============================================================
# 建表 / 种子数据
# ============================================================
def _create_scrm_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_comment_calibration` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `src_table` VARCHAR(64) NOT NULL,
          `src_pk` BIGINT NOT NULL,
          `model_intents` JSON NULL,
          `corrected_intents` JSON NULL,
          `model_sentiment` VARCHAR(32) DEFAULT NULL,
          `corrected_sentiment` VARCHAR(32) DEFAULT NULL,
          `model_high_negative` TINYINT(1) NOT NULL DEFAULT 0,
          `corrected_high_negative` TINYINT(1) NOT NULL DEFAULT 0,
          `model_emotion_target` VARCHAR(255) DEFAULT NULL,
          `corrected_emotion_target` VARCHAR(255) DEFAULT NULL,
          `correction_reason` VARCHAR(64) NOT NULL,
          `correction_note` VARCHAR(500) DEFAULT NULL,
          `context_title` VARCHAR(500) DEFAULT NULL,
          `context_parent_comment` TEXT NULL,
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_comment` (`src_table`,`src_pk`),
          KEY `idx_reason` (`correction_reason`),
          KEY `idx_created` (`created_ts`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论人工校准记录'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_lead` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `src_table` VARCHAR(64) NOT NULL COMMENT '来源评论表',
          `src_pk` BIGINT NOT NULL COMMENT '来源评论主键id',
          `comment_id` VARCHAR(128) DEFAULT NULL,
          `status` VARCHAR(16) NOT NULL DEFAULT 'new',
          `owner` VARCHAR(64) DEFAULT NULL COMMENT '负责人',
          `intent_desc` VARCHAR(255) DEFAULT NULL,
          `note` VARCHAR(500) DEFAULT NULL,
          `last_follow_ts` BIGINT DEFAULT NULL,
          `due_ts` BIGINT DEFAULT NULL COMMENT '下次跟进截止',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_src` (`src_table`,`src_pk`),
          KEY `idx_status` (`status`),
          KEY `idx_owner` (`owner`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SCRM 购买意向线索'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_lead_follow` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `lead_id` BIGINT NOT NULL,
          `action` VARCHAR(32) NOT NULL DEFAULT 'note',
          `content` VARCHAR(1000) NOT NULL,
          `creator` VARCHAR(64) NOT NULL DEFAULT '运营',
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          KEY `idx_lead` (`lead_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='线索跟进记录'
    """)
    # 品牌不再系统预置/关键词自动识别:品牌即合作媒介(scrm_media),旧表废弃清理
    cur.execute("DROP TABLE IF EXISTS `scrm_brand_follow`")
    cur.execute("DROP TABLE IF EXISTS `scrm_brand`")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_skill` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(64) NOT NULL,
          `platform` VARCHAR(32) DEFAULT NULL,
          `bind_up` VARCHAR(128) DEFAULT NULL COMMENT '绑定UP主',
          `style` VARCHAR(1000) DEFAULT NULL COMMENT '人设风格描述',
          `example` VARCHAR(1000) DEFAULT NULL COMMENT '示例回复',
          `status` VARCHAR(16) NOT NULL DEFAULT 'ready',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UP主人设 Skill'
    """)
    cur.execute("SHOW COLUMNS FROM scrm_skill LIKE 'persona'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE scrm_skill ADD COLUMN persona LONGTEXT NULL COMMENT '结构化人设与评论来源'")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_inquiry` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `fs_record_id` VARCHAR(32) NOT NULL COMMENT '飞书记录ID',
          `inquiry_no` VARCHAR(32) DEFAULT NULL COMMENT '询单ID',
          `name` VARCHAR(255) DEFAULT NULL COMMENT '询单名称',
          `inquiry_ts` BIGINT DEFAULT NULL COMMENT '询单时间',
          `sales` VARCHAR(64) DEFAULT NULL COMMENT '商务组员',
          `media_company` VARCHAR(128) DEFAULT NULL COMMENT '媒介公司',
          `brand` VARCHAR(128) DEFAULT NULL COMMENT '品牌',
          `product` VARCHAR(255) DEFAULT NULL COMMENT '产品名称',
          `category` VARCHAR(64) DEFAULT NULL COMMENT '品类',
          `competitor_rule` VARCHAR(64) DEFAULT NULL COMMENT '排竞',
          `expect_ts` BIGINT DEFAULT NULL COMMENT '预计发布时间',
          `coop_types` VARCHAR(255) DEFAULT NULL COMMENT '合作类型,逗号分隔',
          `unit_count` VARCHAR(16) DEFAULT NULL COMMENT '横测台数',
          `platform_accounts` VARCHAR(500) DEFAULT NULL COMMENT '平台账号,逗号分隔',
          `extras` VARCHAR(255) DEFAULT NULL COMMENT '其他要求',
          `follow_detail` VARCHAR(1000) DEFAULT NULL COMMENT '跟进详情',
          `kpi` VARCHAR(255) DEFAULT NULL,
          `remark` VARCHAR(255) DEFAULT NULL COMMENT '备注(多选拼接)',
          `status` VARCHAR(16) NOT NULL DEFAULT '跟进中' COMMENT '是否合作:跟进中/有意向/未合作/无意向/合作成功',
          `contact` VARCHAR(64) DEFAULT NULL COMMENT '联系人',
          `wechat` VARCHAR(128) DEFAULT NULL COMMENT '微信',
          `synced_ts` BIGINT DEFAULT NULL COMMENT '最近同步时间',
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_fs_record` (`fs_record_id`),
          KEY `idx_status` (`status`(8)),
          KEY `idx_brand` (`brand`(32))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商务询单(飞书同步)'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_deal` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `inquiry_id` BIGINT DEFAULT NULL COMMENT '来源询单',
          `name` VARCHAR(255) NOT NULL COMMENT '商单名称',
          `brand` VARCHAR(128) DEFAULT NULL,
          `product` VARCHAR(255) DEFAULT NULL,
          `media_company` VARCHAR(128) DEFAULT NULL,
          `owner` VARCHAR(64) DEFAULT NULL COMMENT '商务负责人',
          `coop_types` VARCHAR(255) DEFAULT NULL,
          `platform_accounts` VARCHAR(500) DEFAULT NULL COMMENT '平台账号,逗号分隔',
          `kpi` VARCHAR(255) DEFAULT NULL,
          `expect_ts` BIGINT DEFAULT NULL COMMENT '预计发布时间',
          `status` VARCHAR(16) NOT NULL DEFAULT 'won' COMMENT 'won合作成功/executing执行中/published已发布/kpi_doneKPI已完成/ended已结束',
          `note` VARCHAR(500) DEFAULT NULL,
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          KEY `idx_inquiry` (`inquiry_id`),
          KEY `idx_status` (`status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商单'
    """)
    for col, ddl in (
        ("amount", "ALTER TABLE `scrm_inquiry` ADD COLUMN `amount` DECIMAL(12,2) DEFAULT NULL COMMENT '商单金额' AFTER `category`"),
        ("rebate", "ALTER TABLE `scrm_inquiry` ADD COLUMN `rebate` DECIMAL(12,2) DEFAULT NULL COMMENT '返点' AFTER `amount`"),
        ("media_amount", "ALTER TABLE `scrm_inquiry` ADD COLUMN `media_amount` DECIMAL(12,2) DEFAULT NULL COMMENT '媒介金额' AFTER `rebate`"),
        ("follow_stage", "ALTER TABLE `scrm_inquiry` ADD COLUMN `follow_stage` VARCHAR(16) NOT NULL DEFAULT 'registered' COMMENT '跟进节点:registered/second' AFTER `status`"),
        ("follow_stage_ts", "ALTER TABLE `scrm_inquiry` ADD COLUMN `follow_stage_ts` BIGINT DEFAULT NULL COMMENT '进入当前跟进节点时间' AFTER `follow_stage`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_inquiry' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_deal_video` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `deal_id` BIGINT NOT NULL,
          `platform` VARCHAR(32) DEFAULT NULL COMMENT '发布平台',
          `account` VARCHAR(128) DEFAULT NULL COMMENT '平台账户',
          `title` VARCHAR(255) DEFAULT NULL COMMENT '视频标题',
          `category` VARCHAR(16) NOT NULL DEFAULT '原创' COMMENT '视频分类:商单/原创',
          `url` VARCHAR(500) NOT NULL COMMENT '展示视频链接',
          `creator` VARCHAR(64) NOT NULL DEFAULT '运营',
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          KEY `idx_deal` (`deal_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商单展示视频链接'
    """)
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_deal_video' AND COLUMN_NAME='category'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_deal_video` ADD COLUMN `category` VARCHAR(16) NOT NULL DEFAULT '原创' COMMENT '视频分类:商单/原创' AFTER `title`")
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_deal_video' AND COLUMN_NAME='amount'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_deal_video` ADD COLUMN `amount` DECIMAL(12,2) DEFAULT NULL COMMENT '视频金额' AFTER `category`")
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_deal_video' AND COLUMN_NAME='publish_ts'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_deal_video` ADD COLUMN `publish_ts` BIGINT DEFAULT NULL COMMENT '视频发布时间' AFTER `amount`")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_media` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(128) NOT NULL COMMENT '媒介公司(=品牌名)',
          `party_a` VARCHAR(255) DEFAULT NULL COMMENT '甲方主体信息',
          `contact` VARCHAR(64) DEFAULT NULL COMMENT '联系人',
          `phone` VARCHAR(32) DEFAULT NULL COMMENT '联系人手机号',
          `source` VARCHAR(16) NOT NULL DEFAULT 'manual' COMMENT 'auto询单联动/manual手动',
          `src_deal_id` BIGINT DEFAULT NULL,
          `status` VARCHAR(16) NOT NULL DEFAULT 'cooperate' COMMENT '品牌跟进状态 new/touching/intent/cooperate/lost',
          `owner` VARCHAR(64) DEFAULT NULL COMMENT '商务负责人',
          `background` VARCHAR(1000) DEFAULT NULL COMMENT '品牌背景',
          `note` VARCHAR(500) DEFAULT NULL,
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='合作媒介(即品牌)'
    """)
    # 既有库补列(幂等)
    for col, ddl in (
        ("status", "ALTER TABLE `scrm_media` ADD COLUMN `status` VARCHAR(16) NOT NULL DEFAULT 'cooperate' "
                   "COMMENT '品牌跟进状态' AFTER `src_deal_id`"),
        ("owner", "ALTER TABLE `scrm_media` ADD COLUMN `owner` VARCHAR(64) DEFAULT NULL "
                  "COMMENT '商务负责人' AFTER `status`"),
        ("background", "ALTER TABLE `scrm_media` ADD COLUMN `background` VARCHAR(1000) DEFAULT NULL "
                       "COMMENT '品牌背景' AFTER `owner`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_media' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_media' AND COLUMN_NAME='wechat'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_media` ADD COLUMN `wechat` VARCHAR(128) DEFAULT NULL COMMENT '联系人微信账户' AFTER `contact`")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_business_wechat` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `wechat` VARCHAR(128) NOT NULL COMMENT '商务号/微信账户',
          `member` VARCHAR(64) NOT NULL COMMENT '商务组员',
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          `completed_ts` BIGINT DEFAULT NULL COMMENT '完成时间',
          PRIMARY KEY (`id`), UNIQUE KEY `uk_wechat` (`wechat`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商务号与组员映射'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_category` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(64) NOT NULL,
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`), UNIQUE KEY `uk_category_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商单品类'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_kpi_config` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `platform` VARCHAR(32) NOT NULL COMMENT '平台',
          `cpme_ratio` DECIMAL(12,4) NOT NULL COMMENT 'CPME比例',
          `cpe_ratio` DECIMAL(12,4) NOT NULL COMMENT 'CPE比例',
          `original_base_value` BIGINT NOT NULL DEFAULT 0 COMMENT '原创基础值',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`), UNIQUE KEY `uk_kpi_platform` (`platform`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='平台KPI计算配置'
    """)
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_kpi_config' AND COLUMN_NAME='original_base_value'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_kpi_config` ADD COLUMN `original_base_value` BIGINT NOT NULL DEFAULT 0 COMMENT '原创基础值' AFTER `cpe_ratio`")
    for col, ddl in (
        ("order_no", "ALTER TABLE `scrm_deal` ADD COLUMN `order_no` VARCHAR(64) DEFAULT NULL COMMENT '订单编号' AFTER `inquiry_id`"),
        ("category", "ALTER TABLE `scrm_deal` ADD COLUMN `category` VARCHAR(64) DEFAULT NULL COMMENT '品类' AFTER `product`"),
        ("rebate", "ALTER TABLE `scrm_deal` ADD COLUMN `rebate` DECIMAL(12,2) DEFAULT NULL COMMENT '返点' AFTER `category`"),
        ("kpi_views", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_views` BIGINT DEFAULT NULL COMMENT 'KPI播放' AFTER `kpi`"),
        ("kpi_interactions", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_interactions` BIGINT DEFAULT NULL COMMENT 'KPI互动' AFTER `kpi_views`"),
        ("amount", "ALTER TABLE `scrm_deal` ADD COLUMN `amount` DECIMAL(12,2) DEFAULT NULL COMMENT '商单金额' AFTER `rebate`"),
        ("media_amount", "ALTER TABLE `scrm_deal` ADD COLUMN `media_amount` DECIMAL(12,2) DEFAULT NULL COMMENT '媒介金额' AFTER `amount`"),
        ("kpi_platform", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_platform` VARCHAR(32) DEFAULT NULL COMMENT 'KPI平台' AFTER `kpi_interactions`"),
        ("completed_ts", "ALTER TABLE `scrm_deal` ADD COLUMN `completed_ts` BIGINT DEFAULT NULL COMMENT '完成时间' AFTER `updated_ts`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_deal' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_media_follow` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `media_id` BIGINT NOT NULL COMMENT '合作媒介(品牌)ID',
          `content` VARCHAR(1000) NOT NULL,
          `next_plan` VARCHAR(255) DEFAULT NULL,
          `creator` VARCHAR(64) NOT NULL DEFAULT '运营',
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          KEY `idx_media` (`media_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='品牌跟进记录'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_creator` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `platform` VARCHAR(32) NOT NULL COMMENT '平台',
          `name` VARCHAR(128) NOT NULL COMMENT 'UP主/作者昵称',
          `note` VARCHAR(500) DEFAULT NULL COMMENT '运营备注',
          `status` VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active合作中/reserve储备/retired停用',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_plat_name` (`platform`, `name`(64))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UP主管理'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `UP主管理` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `昵称` VARCHAR(128) NOT NULL COMMENT 'UP主/作者昵称',
          `BIT窗口` VARCHAR(255) DEFAULT NULL COMMENT 'BitBrowser窗口',
          `账号等级` VARCHAR(64) DEFAULT NULL COMMENT '账号等级',
          `运营人员` VARCHAR(64) DEFAULT NULL COMMENT '运营人员',
          `出镜人` VARCHAR(64) DEFAULT NULL COMMENT '出镜人',
          `组别` VARCHAR(64) DEFAULT NULL COMMENT '组别',
          `账号绑定公司` VARCHAR(128) DEFAULT NULL COMMENT '账号绑定公司',
          `商务微信` VARCHAR(128) DEFAULT NULL COMMENT '商务微信',
          `商务邮箱` VARCHAR(128) DEFAULT NULL COMMENT '商务邮箱',
          `状态` VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active合作中/reserve储备/retired停用',
          `备注` VARCHAR(500) DEFAULT NULL COMMENT '运营备注',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_nickname` (`昵称`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UP主管理'
    """)
    # 平台运营账号:账号自身信息保留在账号表,UP主体信息迁入 `UP主管理`
    from account_cookie_health import ensure_schema
    ensure_schema(cur)
    for col, ddl in (
        ("UP主ID", "ALTER TABLE `平台运营账号` ADD COLUMN `UP主ID` BIGINT DEFAULT NULL "
                   "COMMENT '关联UP主管理.id' AFTER `昵称`"),
        ("状态", "ALTER TABLE `平台运营账号` ADD COLUMN `状态` VARCHAR(16) NOT NULL DEFAULT 'active' "
                "COMMENT 'active运营中/paused暂停/retired停用' AFTER `Bit窗口`"),
        ("备注", "ALTER TABLE `平台运营账号` ADD COLUMN `备注` VARCHAR(500) DEFAULT NULL "
                "COMMENT '运营备注' AFTER `状态`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='平台运营账号' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("SELECT COUNT(*) c FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='平台运营账号' AND INDEX_NAME='idx_up_creator'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `平台运营账号` ADD KEY `idx_up_creator` (`UP主ID`)")


SKILL_SEEDS = [
    ("家电老炮·老王", "抖音", "家电测评老王",
     "直爽专业的拆机党,说话干脆,喜欢用实测数据说话,偶尔犀利吐槽。",
     "这台机器我拆过,压缩机用的是大牌货,噪音控制也到位,预算合适闭眼入,别被花里胡哨的功能带偏了。"),
    ("精致生活家·Lily", "小红书", "Lily的家电研究所",
     "精致种草风格,把参数翻译成生活场景,语气温柔有画面感,善用emoji。",
     "姐妹们!这台烘干机真的改变生活✨ 阴雨天再也不用囤袜子了,容量够一家三口,颜值也很能打~"),
    ("大白话家电", "视频号", "大白话聊家电",
     "面向中老年用户,口播亲切,不用专业术语,爱打比方,耐心解释。",
     "叔叔阿姨记住喽,买空调就看两点:一级能效省电,变频的住着舒服。这台两样都占,价格也实在。"),
]


def _seed(cur):
    now = int(time.time())
    for name, plat, bind, style, ex in SKILL_SEEDS:
        cur.execute(
            "INSERT IGNORE INTO scrm_skill (name,platform,bind_up,style,example,status,created_ts,updated_ts) "
            "VALUES (%s,%s,%s,%s,%s,'ready',%s,%s)", (name, plat, bind, style, ex, now, now))


def _provision_leads(cur):
    """把 purchase_intent=1 的评论同步为线索(幂等),新线索 3 天后到期"""
    now = int(time.time())
    # add_ts 在部分平台为毫秒,统一归一化为秒
    ts = "CASE WHEN add_ts > 100000000000 THEN ROUND(add_ts/1000) ELSE add_ts END"
    cur.execute(f"""
        INSERT IGNORE INTO scrm_lead (src_table, src_pk, comment_id, created_ts, due_ts, updated_ts)
        SELECT src_table, src_pk, comment_id, {ts}, {ts}+259200, %s
        FROM {_union()} WHERE intent=1
    """, (now,))
    # 历史毫秒数据一次性归一化
    cur.execute("""
        UPDATE scrm_lead SET
          created_ts=CASE WHEN created_ts>100000000000 THEN ROUND(created_ts/1000) ELSE created_ts END,
          due_ts=CASE WHEN due_ts>100000000000 THEN ROUND(due_ts/1000) ELSE due_ts END,
          last_follow_ts=CASE WHEN last_follow_ts>100000000000 THEN ROUND(last_follow_ts/1000) ELSE last_follow_ts END
        WHERE created_ts>100000000000 OR due_ts>100000000000 OR last_follow_ts>100000000000
    """)
    # 仅在首批线索(负责人全空)时做演示分布,后续新增线索保持"待分配"
    cur.execute("SELECT COUNT(*) c FROM scrm_lead WHERE owner IS NOT NULL")
    if cur.fetchone()["c"] == 0:
        cur.execute("SELECT id FROM scrm_lead ORDER BY id")
        ids = [r["id"] for r in cur.fetchall()]
        for lid in ids:
            owner = TEAM[lid % 4]
            mod = lid % 10
            status = ["new", "new", "contacted", "contacted", "following",
                      "following", "following", "feedback", "won", "lost"][mod]
            cur.execute(
                "UPDATE scrm_lead SET owner=%s, status=%s, "
                "last_follow_ts=IF(%s='new', NULL, created_ts+86400), updated_ts=%s WHERE id=%s",
                (owner, status, status, int(time.time()), lid))


def init_scrm():
    """启动时幂等建表 + 种子 + 线索同步 + 飞书询单同步 + UP主同步"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _create_scrm_tables(cur)
                _seed(cur)
                _provision_leads(cur)
                _sync_creators(cur)
            conn.commit()
        logger.info("SCRM 表结构与种子数据已就绪")
    except Exception as e:
        logger.exception("SCRM 初始化失败: %s", e)
    try:
        sync_feishu_inquiries()
    except Exception as e:
        logger.exception("飞书询单同步失败: %s", e)


# ============================================================
# 飞书商单询单表同步 + 合作成功自动联动
# ============================================================
INQUIRY_STATUSES = ["跟进中", "有意向", "未合作", "无意向", "合作成功"]
DEAL_STATUS = [
    ("executing", "执行中", "#4f46e5"),
    ("published", "已发布", "#0d9488"),
    ("won", "合作成功", "#16a34a"),
    ("kpi_done", "KPI已完成", "#16a34a"),
    ("ended", "已结束", "#6b7280"),
    ("canceled", "已取消", "#dc2626"),
]
DEAL_STATUS_MAP = {s[0]: s[1] for s in DEAL_STATUS}
CREATOR_STATUS = [
    ("active", "合作中", "#16a34a"),
    ("reserve", "储备", "#d97706"),
    ("retired", "停用", "#9ca3af"),
]
CREATOR_STATUS_MAP = {s[0]: s[1] for s in CREATOR_STATUS}
_FS_BASE = "https://open.feishu.cn/open-apis"


def _fs_token():
    r = requests.post(f"{_FS_BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                      timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg')}")
    return data["tenant_access_token"]


def _cell_text(v):
    """飞书单元格值 → 文本(兼容文本/单选/多选/关联/公式)"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(str(x.get("text") or x.get("name") or "").strip())
            else:
                parts.append(str(x).strip())
        return ",".join(p for p in parts if p)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or "").strip()
    return str(v).strip()


def _cell_ts(v):
    """飞书日期(毫秒) → 秒时间戳"""
    if isinstance(v, (int, float)) and v > 0:
        return int(v // 1000)
    return None


def _cell_money(v):
    text = _cell_text(v).replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except ArithmeticError:
        return None


def _fetch_fs_inquiries():
    """拉取飞书商单询单表全部记录"""
    tok = _fs_token()
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{_FS_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_INQUIRY_TABLE}/records"
    records, page_token = [], ""
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"读取询单表失败: {body.get('msg')}")
        data = body.get("data") or {}
        records.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
    return records


def _upsert_inquiries(cur, records):
    """按飞书 record_id 幂等写入 scrm_inquiry"""
    now = int(time.time())
    new_n = upd_n = 0
    for rec in records:
        rid = rec.get("record_id") or _cell_text((rec.get("fields") or {}).get("记录ID"))
        if not rid:
            continue
        f = rec.get("fields") or {}
        status = _cell_text(f.get("biz_state") or f.get("是否合作")) or "跟进中"
        if status == "询单":
            status = "有意向"
        if status not in INQUIRY_STATUSES:
            status = "跟进中"
        row = (
            rid,
            _cell_text(f.get("询单ID"))[:32] or None,
            _cell_text(f.get("询单名称"))[:255] or None,
            _cell_ts(f.get("created_ts") or f.get("询单时间")),
            _cell_text(f.get("商务组员"))[:64] or None,
            _cell_text(f.get("媒介公司"))[:128] or None,
            _cell_text(f.get("品牌"))[:128] or None,
            _cell_text(f.get("产品名称"))[:255] or None,
            _cell_text(f.get("品类"))[:64] or None,
            _cell_text(f.get("排竞"))[:64] or None,
            _cell_ts(f.get("预计发布时间")),
            _cell_text(f.get("deal_type") or f.get("合作类型"))[:255] or None,
            _cell_text(f.get("横测台数"))[:16] or None,
            _cell_text(f.get("平台账号（任务）"))[:500] or None,
            _cell_text(f.get("extras") or f.get("其他"))[:255] or None,
            _cell_text(f.get("跟进详情（询单沟通具体情况、是否合作，是否一对多）"))[:1000] or None,
            _cell_text(f.get("KPI"))[:255] or None,
            _cell_text(f.get("remark") or f.get("备注 副本"))[:255] or None,
            status,
            _cell_text(f.get("联系人"))[:64] or None,
            _cell_text(f.get("wechat") or f.get("微信"))[:128] or None,
            _cell_money(next((f.get(k) for k in ("amount", "商单金额", "合作金额", "金额", "报价", "客户报价", "总金额") if f.get(k) not in (None, "")), None)),
            _cell_money(next((f.get(k) for k in ("rebate", "返点", "返点金额", "返点比例", "返佣", "返利") if f.get(k) not in (None, "")), None)),
            _cell_money(next((f.get(k) for k in ("media_amount", "媒介金额", "媒介报价", "媒介金额（元）", "媒介费用", "媒介成本", "给媒介金额", "成本") if f.get(k) not in (None, "")), None)),
            _cell_ts(f.get("updated_ts")) or now, now,
        )
        n = cur.execute("""
            INSERT INTO scrm_inquiry
              (fs_record_id, inquiry_no, name, inquiry_ts, sales, media_company,
               brand, product, category, competitor_rule, expect_ts, coop_types,
               unit_count, platform_accounts, extras, follow_detail, kpi, remark,
               status, contact, wechat, amount, rebate, media_amount, synced_ts, created_ts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              inquiry_no=VALUES(inquiry_no), name=VALUES(name), inquiry_ts=VALUES(inquiry_ts),
              sales=VALUES(sales), media_company=VALUES(media_company), brand=VALUES(brand),
              product=VALUES(product), category=VALUES(category), competitor_rule=VALUES(competitor_rule),
              expect_ts=VALUES(expect_ts), coop_types=VALUES(coop_types), unit_count=VALUES(unit_count),
              platform_accounts=VALUES(platform_accounts), extras=VALUES(extras),
              follow_detail=VALUES(follow_detail), kpi=VALUES(kpi), remark=VALUES(remark),
              status=VALUES(status), contact=VALUES(contact), wechat=VALUES(wechat),
              amount=VALUES(amount), rebate=VALUES(rebate), media_amount=VALUES(media_amount),
              synced_ts=VALUES(synced_ts)
        """, row)
        if n == 1:
            new_n += 1
        elif n == 2:
            upd_n += 1
    return new_n, upd_n


def _ensure_won_artifacts(cur, inq):
    """询单合作成功 → 自动补建商单 + 合作媒介(均幂等)"""
    now = int(time.time())
    # 1) 商单
    cur.execute("SELECT id FROM scrm_deal WHERE inquiry_id=%s LIMIT 1", (inq["id"],))
    deal = cur.fetchone()
    if not deal:
        cur.execute("""
            INSERT INTO scrm_deal (inquiry_id, name, brand, product, media_company, owner,
                                   coop_types, platform_accounts, kpi, amount, rebate, media_amount, expect_ts,
                                   status, created_ts, updated_ts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'won',%s,%s)
        """, (inq["id"], inq.get("name") or f"商单-{inq['inquiry_no'] or inq['id']}",
              inq.get("brand"), inq.get("product"), inq.get("media_company"), inq.get("sales"),
              inq.get("coop_types"), inq.get("platform_accounts"), inq.get("kpi"),
              inq.get("amount"), inq.get("rebate"), inq.get("media_amount"), inq.get("expect_ts"), now, now))
        cur.execute("SELECT id FROM scrm_deal WHERE inquiry_id=%s LIMIT 1", (inq["id"],))
        deal = cur.fetchone()
    deal_id = deal["id"] if deal else None
    if deal_id:
        cur.execute("UPDATE scrm_deal SET amount=COALESCE(%s,amount), rebate=COALESCE(%s,rebate), media_amount=COALESCE(%s,media_amount), updated_ts=%s WHERE id=%s",
                    (inq.get("amount"), inq.get("rebate"), inq.get("media_amount"), now, deal_id))
        parsed_accounts = _split_platform_accounts(inq.get("platform_accounts"))
        first_platform = next((p for p, _ in parsed_accounts if p), None)
        if first_platform:
            cur.execute("UPDATE scrm_deal SET kpi_platform=COALESCE(kpi_platform,%s) WHERE id=%s", (first_platform, deal_id))
    else:
        parsed_accounts = _split_platform_accounts(inq.get("platform_accounts"))
    # 询单中的平台账户自动拆分为商单详情的视频列表初始行。
    if deal_id:
        cur.execute("SELECT id, platform, account FROM scrm_deal_video WHERE deal_id=%s AND (url='' OR url IS NULL)", (deal_id,))
        for placeholder in cur.fetchall():
            if not placeholder.get("platform") and _split_platform_accounts(placeholder.get("account")):
                parsed = _split_platform_accounts(placeholder.get("account"))
                if parsed[0][0] and parsed[0][1] != placeholder.get("account"):
                    cur.execute("DELETE FROM scrm_deal_video WHERE id=%s", (placeholder["id"],))
        for platform, account in parsed_accounts:
            cur.execute("""SELECT id FROM scrm_deal_video
                         WHERE deal_id=%s AND platform <=> %s AND account=%s AND (url='' OR url IS NULL) LIMIT 1""",
                        (deal_id, platform, account))
            existing_video = cur.fetchone()
            if existing_video:
                cur.execute("UPDATE scrm_deal_video SET category='商单' WHERE id=%s", (existing_video["id"],))
            else:
                cur.execute("""INSERT INTO scrm_deal_video
                             (deal_id, platform, account, title, category, url, creator, created_ts)
                             VALUES (%s,%s,%s,%s,'商单','',%s,%s)""",
                            (deal_id, platform, account, "询单平台账户", "系统", now))
    # 2) 合作媒介(=品牌,按公司名幂等,联动时回填联系人/负责人)
    company = (inq.get("media_company") or "").strip()
    if company:
        cur.execute("SELECT id, contact, owner, party_a FROM scrm_media WHERE name=%s", (company,))
        media = cur.fetchone()
        if not media:
            cur.execute("""
                INSERT INTO scrm_media (name, party_a, contact, wechat, source, src_deal_id,
                                        status, owner, created_ts, updated_ts)
                VALUES (%s,%s,%s,%s,'auto',%s,'cooperate',%s,%s,%s)
            """, (company[:128], inq.get("brand"), inq.get("contact"), inq.get("wechat"), deal_id,
                  inq.get("sales"), now, now))
        else:
            sets, params = [], []
            if not media.get("contact") and inq.get("contact"):
                sets.append("contact=%s"); params.append(inq.get("contact"))
            if not media.get("wechat") and inq.get("wechat"):
                sets.append("wechat=%s"); params.append(inq.get("wechat"))
            if not media.get("owner") and inq.get("sales"):
                sets.append("owner=%s"); params.append(inq.get("sales"))
            if not media.get("party_a") and inq.get("brand"):
                sets.append("party_a=%s"); params.append(inq.get("brand"))
            if sets:
                sets.append("updated_ts=%s"); params.append(now); params.append(media["id"])
                cur.execute(f"UPDATE scrm_media SET {', '.join(sets)} WHERE id=%s", params)
    return deal_id


def _run_won_flow(cur):
    """对全部合作成功的询单补齐商单/媒介,返回处理数量"""
    cur.execute("SELECT * FROM scrm_inquiry WHERE status='合作成功'")
    rows = cur.fetchall()
    for r in rows:
        _ensure_won_artifacts(cur, r)
    return len(rows)


def sync_feishu_inquiries():
    """拉取飞书询单并触发合作成功联动,返回统计"""
    records = _fetch_fs_inquiries()
    with get_conn() as conn:
        with conn.cursor() as cur:
            new_n, upd_n = _upsert_inquiries(cur, records)
            won_n = _run_won_flow(cur)
        conn.commit()
    logger.info("飞书询单同步完成: 新增 %s, 更新 %s, 合作成功联动 %s", new_n, upd_n, won_n)
    return {"total": len(records), "new": new_n, "updated": upd_n, "won": won_n}


# ============================================================
# UP主管理(平台运营账号同步 + Skill 绑定)
# ============================================================
def _sync_creators(cur):
    """从平台运营账号同步 UP 主档案,并回写账号到 UP 主的 N-1 关系"""
    now = int(time.time())
    cur.execute("SELECT COUNT(*) c FROM `UP主管理`")
    before = int(cur.fetchone()["c"] or 0)
    cur.execute("""
        INSERT INTO `UP主管理` (`昵称`, `BIT窗口`, `created_ts`, `updated_ts`)
        SELECT `昵称`, MAX(NULLIF(`Bit窗口`, '')), %s, %s
        FROM `平台运营账号`
        WHERE `昵称` IS NOT NULL AND `昵称` != ''
        GROUP BY `昵称`
        ON DUPLICATE KEY UPDATE
          `BIT窗口` = IF((`BIT窗口` IS NULL OR `BIT窗口` = '') AND VALUES(`BIT窗口`) IS NOT NULL,
                         VALUES(`BIT窗口`), `BIT窗口`),
          `updated_ts` = `updated_ts`
    """, (now, now))
    cur.execute("""
        UPDATE `平台运营账号` A
        JOIN `UP主管理` C ON C.`昵称` = A.`昵称`
        SET A.`UP主ID` = C.`id`
        WHERE A.`昵称` IS NOT NULL AND A.`昵称` != ''
          AND (A.`UP主ID` IS NULL OR A.`UP主ID` != C.`id`)
    """)
    cur.execute("SELECT COUNT(*) c FROM `UP主管理`")
    after = int(cur.fetchone()["c"] or 0)
    return max(0, after - before)


def _propagate_creator_bit_window(cur, creator_id):
    """UP主保存窗口后，将其写入所有已关联的平台运营账号。"""
    cur.execute("""
        UPDATE `平台运营账号` A
        JOIN `UP主管理` C ON C.`id` = A.`UP主ID`
        SET A.`Bit窗口` = C.`BIT窗口`
        WHERE C.`id`=%s
    """, (creator_id,))
    return cur.rowcount


@bp.get("/creators")
def page_creators():
    return _page("creators.html", "creators")


# ============================================================
# 页面路由
# ============================================================
def _page(tpl, active, **kw):
    return render_template(f"scrm/{tpl}", active=active, **kw)


@bp.get("/")
def page_overview():
    return _page("overview.html", "overview")


@bp.get("/comments")
def page_comments():
    return _page("comments.html", "comments")


@bp.get("/videos")
def page_videos():
    return _page("videos.html", "videos")


@bp.get("/account-management")
def page_account_management():
    return _page("account_management.html", "account_management")


@bp.get("/comments/<table>/<int:rid>")
@_table_required
def page_comment_detail(table, rid):
    return _page("comment_detail.html", "comments", table=table, rid=rid)


@bp.get("/alerts")
def page_alerts():
    return _page("alerts.html", "alerts")


@bp.get("/analysis")
def page_workbench():
    return _page("workbench.html", "workbench")


@bp.get("/analysis/reply/<table>/<int:rid>")
@_table_required
def page_reply(table, rid):
    return _page("reply.html", "workbench", table=table, rid=rid)


@bp.get("/skills")
def page_skills():
    return _page("skills.html", "skills")


@bp.get("/leads/overview")
def page_leads_overview():
    return _page("leads_overview.html", "leads_overview")


@bp.get("/leads")
def page_leads():
    return _page("leads.html", "leads")


@bp.get("/leads/<int:lid>")
def page_lead_detail(lid):
    return _page("lead_detail.html", "leads", lid=lid)


@bp.get("/leads/<int:lid>/workflow")
def page_lead_workflow(lid):
    return _page("lead_workflow.html", "leads", lid=lid)


@bp.get("/leads/alerts")
def page_lead_alerts():
    return _page("lead_alerts.html", "lead_alerts")


@bp.get("/brands")
def page_brands():
    return _page("brands.html", "brands")


@bp.get("/brands/<int:bid>")
def page_brand_detail(bid):
    return _page("brand_detail.html", "brands", bid=bid)


@bp.get("/brands/funnel")
def page_brand_funnel():
    return _page("brand_funnel.html", "brands")


@bp.get("/inquiries")
def page_inquiries():
    return _page("inquiries.html", "inquiries")


@bp.get("/business-wechat")
def page_business_wechat():
    return _page("business_wechat.html", "business_wechat")


@bp.get("/categories")
def page_categories():
    return _page("categories.html", "categories")


@bp.get("/settings")
def page_settings():
    return _page("settings.html", "settings")


@bp.get("/deals")
def page_deals():
    return _page("deals.html", "deals")


@bp.get("/deals/<int:did>")
def page_deal_detail(did):
    return _page("deal_detail.html", "deals", did=did)


@bp.get("/media")
def page_media():
    return _page("media.html", "media")


@bp.get("/accounts")
def page_accounts():
    return _page("accounts.html", "accounts")


# ============================================================
# API: 监控总览
# ============================================================
@bp.get("/api/overview")
def api_overview():
    now = int(time.time())
    today0 = now - now % 86400 + 8 * 3600  # 本地今日0点(GMT+8)
    if today0 > now:
        today0 -= 86400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM {_union()} WHERE add_ts >= %s", (today0,))
            today_comments = cur.fetchone()["c"]
            cur.execute(f"SELECT COUNT(*) c FROM {_union()} WHERE neg=1 AND handled=0")
            pending_alerts = cur.fetchone()["c"]
            cur.execute(f"SELECT COUNT(*) c FROM {_union()} WHERE neg=1")
            negative_total = cur.fetchone()["c"]
            cur.execute(f"SELECT COUNT(DISTINCT content_id) c FROM {_union()}")
            video_cover = cur.fetchone()["c"]

            # 近14天趋势(按发布时间)
            cur.execute(f"""
                SELECT FROM_UNIXTIME(ctime, '%%Y-%%m-%%d') d,
                       COUNT(*) total, SUM(neg) neg, SUM(intent) intent
                FROM {_union()}
                WHERE ctime >= %s GROUP BY d ORDER BY d
            """, (now - 14 * 86400,))
            trend_map = {r["d"]: r for r in cur.fetchall()}

            # 平台分布
            cur.execute(f"""
                SELECT src_table, COUNT(*) total, SUM(neg) neg, SUM(intent) intent,
                       SUM(handled) handled
                FROM {_union()} GROUP BY src_table
            """)
            platforms = []
            for r in cur.fetchall():
                r["platform"] = PLATFORM_OF.get(r["src_table"], r["src_table"])
                platforms.append(r)

            # UP主分布(取内容表)
            top_creators = []
            for t in TABLES:
                m = MAP[t]
                try:
                    cur.execute(f"""
                        SELECT {m['cnick']} upname, COUNT(*) videos
                        FROM `{m['ctable']}` GROUP BY upname ORDER BY videos DESC LIMIT 5
                    """)
                    for r in cur.fetchall():
                        r["platform"] = m["platform"]
                        top_creators.append(r)
                except Exception:
                    pass
            top_creators.sort(key=lambda r: -r["videos"])
            top_creators = top_creators[:10]

            # 最近负面告警
            cur.execute(f"""
                SELECT src_table, src_pk, nickname, content, ctime, reply
                FROM {_union()} WHERE neg=1 ORDER BY ctime DESC LIMIT 10
            """)
            recent_alerts = _with_platform(cur.fetchall())

    # 补齐14天
    trend = []
    for i in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        r = trend_map.get(d)
        trend.append({
            "date": d[5:],
            "total": r["total"] if r else 0,
            "neg": int(r["neg"] or 0) if r else 0,
            "intent": int(r["intent"] or 0) if r else 0,
        })
    return jsonify({
        "cards": {
            "today_comments": today_comments,
            "pending_alerts": pending_alerts,
            "negative_total": negative_total,
            "video_cover": video_cover,
        },
        "trend": trend,
        "platforms": platforms,
        "top_creators": top_creators,
        "recent_alerts": recent_alerts,
    })


# ============================================================
# API: 评论列表 / 详情
# ============================================================
def _comment_threads(records, matched):
    """Keep complete threads together; IDs are scoped to platform and content."""
    def key(row, cid=None):
        return (row["src_table"], str(row["content_id"]), str(row["comment_id"] if cid is None else cid))
    nodes = {key(r): r for r in records}
    parents = {}
    for k, r in nodes.items():
        pid = str(r.get("parent_comment_id") or "")
        pk = key(r, pid)
        parents[k] = pk if pid not in ("", "0") and pk in nodes and pk != k else None
    # Break malformed cycles so no records disappear or recurse indefinitely.
    for k in nodes:
        seen = set()
        current = k
        while current is not None:
            if current in seen:
                parents[current] = None
                break
            seen.add(current)
            current = parents[current]
    children = {k: [] for k in nodes}
    roots = []
    for k, parent in parents.items():
        (roots if parent is None else children[parent]).append(k)
    def order(k):
        return (int(nodes[k].get("ctime") or 0), int(nodes[k]["src_pk"]))
    threads = []
    for root in roots:
        flat = []
        stack = [(root, 0)]
        while stack:
            k, depth = stack.pop()
            row = dict(nodes[k], depth=depth)
            row["matches_filter"] = (row["src_table"], row["src_pk"]) in matched
            row["parent_missing"] = depth == 0 and str(row.get("parent_comment_id") or "") not in ("", "0")
            flat.append(row)
            stack.extend((child, depth + 1) for child in sorted(children[k], key=order, reverse=True))
        if any(r["matches_filter"] for r in flat):
            threads.append((max(order(key(r)) for r in flat if r["matches_filter"]), flat))
    threads.sort(key=lambda item: item[0], reverse=True)
    return [flat for _, flat in threads]


_COMMENT_READ_SLOTS = threading.BoundedSemaphore(2)
THREAD_PAGE_SIZE = 50


def _bounded_comment_read(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _COMMENT_READ_SLOTS.acquire(blocking=False):
            return jsonify(error="评论查询繁忙，请稍后重试"), 429
        try:
            return fn(*args, **kwargs)
        finally:
            _COMMENT_READ_SLOTS.release()
    return wrapped


def _load_thread_page(cur, table, root_pk, args, offset=0):
    m = MAP[table]
    cur.execute(f"SELECT `{m['cid']}` AS content_id FROM `{table}` WHERE id=%s", (root_pk,))
    root = cur.fetchone()
    if not root:
        return [], False
    where, params = _apply_comment_filters(args)
    sql = thread_page_sql(table, m['cid'], m['time'], _comment_frag(table), where[7:] if where else '1=1')
    cur.execute(sql, [root['content_id'], root_pk] + params + [THREAD_PAGE_SIZE + 1, offset])
    records = cur.fetchall()
    more = len(records) > THREAD_PAGE_SIZE
    rows = _with_platform(records[:THREAD_PAGE_SIZE])
    for row in rows:
        row['matches_filter'] = bool(row['matches_filter'])
        row['parent_missing'] = bool(row['parent_missing'])
        row['thread_root'] = root_pk
    _load_contents(cur, rows)
    return rows, more


@bp.get("/api/comments/thread/<table>/<int:root_pk>")
@_table_required
@_bounded_comment_read
def api_comment_thread(table, root_pk):
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify(error='分页参数无效'), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            rows, more = _load_thread_page(cur, table, root_pk, request.args, offset)
    return jsonify(rows=rows, offset=offset, has_more=more, batch_size=THREAD_PAGE_SIZE)


@bp.get("/api/comments")
@_bounded_comment_read
def api_comments():
    try:
        page = max(1, int(request.args.get("page", 1)))
        size = max(1, min(100, int(request.args.get("size", 20))))
    except ValueError:
        return jsonify(error='分页参数无效'), 400
    where, params = _apply_comment_filters(request.args)
    nodes = ' UNION ALL '.join(
        f"SELECT n.id AS src_pk, '{t}' AS src_table, p.id AS parent_pk "
        f"FROM `{t}` n LEFT JOIN `{t}` p ON p.comment_id=n.parent_comment_id "
        f"AND p.`{MAP[t]['cid']}`=n.`{MAP[t]['cid']}` "
        f"AND n.parent_comment_id NOT IN ('','0') AND p.id<>n.id"
        for t in TABLES)
    cte = thread_roots_sql(nodes, f"SELECT src_table, src_pk, ctime FROM {_union()}{where}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(cte + 'SELECT /*+ MAX_EXECUTION_TIME(15000) */ COUNT(*) AS total FROM roots', params)
            total = cur.fetchone()['total']
            cur.execute(cte + 'SELECT /*+ MAX_EXECUTION_TIME(15000) */ src_table,root_pk FROM roots ORDER BY sort_key DESC,src_table,root_pk LIMIT %s OFFSET %s', params + [size, (page-1)*size])
            roots = cur.fetchall()
            rows, threads = [], []
            for root in roots:
                part, more = _load_thread_page(cur, root['src_table'], root['root_pk'], request.args)
                rows.extend(part)
                threads.append(dict(root, offset=0, has_more=more, batch_size=THREAD_PAGE_SIZE))
    return jsonify(rows=rows, total=total, page=page, size=size, threads=threads)


def _load_content(cur, table, content_id):
    m = MAP[table]
    try:
        cur.execute(
            f"SELECT CAST(`{m['ctitle']}` AS CHAR) title, CAST({m['curl']} AS CHAR) url, "
            f"{m['cnick']} upname FROM `{m['ctable']}` WHERE `{m['cid']}`=%s LIMIT 1",
            (content_id,))
        return cur.fetchone()
    except Exception:
        return None


@bp.get("/api/comment/<table>/<int:rid>")
@_table_required
def api_comment_detail(table, rid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({_comment_frag(table)}) Z WHERE Z.src_pk=%s", (rid,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "评论不存在"}), 404
            row["platform"] = MAP[table]["platform"]
            row["content_info"] = _load_content(cur, table, row["content_id"])
            # 同视频下的上下文评论(前5/后5)
            cur.execute(f"""
                SELECT src_pk, nickname, content, ctime, neg, intent, handled
                FROM {_union()} WHERE src_table=%s AND content_id=%s
                ORDER BY ctime DESC LIMIT 11
            """, (table, row["content_id"]))
            row["thread"] = cur.fetchall()
    return jsonify(row)


@bp.post("/api/comment/<table>/<int:rid>/analyze")
@_table_required
def api_comment_analyze(table, rid):
    """手动触发单条 AI 分析(工作台/推荐回复页使用)"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT content FROM `{table}` WHERE id=%s", (rid,))
            r = cur.fetchone()
    if not r:
        return jsonify({"error": "评论不存在"}), 404
    res = call_deepseek((r["content"] or "")[:500])
    if res is None:
        return jsonify({"ok": False, "error": "AI 分析失败,请检查 DeepSeek 额度或稍后重试"}), 502
    is_neg, has_intent, reply = res
    mark_analyzed(table, rid, is_neg, has_intent, reply)
    return jsonify({"ok": True, "negative": is_neg, "intent": has_intent, "reply": reply})


# ============================================================
# API: 视频列表(跨平台内容聚合)
# ============================================================
@bp.get("/api/videos")
def api_videos():
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    clauses, params = [], []
    platform = request.args.get("platform", "")
    if platform:
        matched = [t for t in TABLES if PLATFORM_OF[t] == platform]
        if matched:
            clauses.append("V.src_table IN (%s)" % ",".join(["%s"] * len(matched)))
            params.extend(matched)
    q = (request.args.get("q") or "").strip()
    if q:
        clauses.append("V.title LIKE %s")
        params.append(f"%{q}%")
    up = (request.args.get("up") or "").strip()
    if up:
        clauses.append("V.upname LIKE %s")
        params.append(f"%{up}%")
    category = (request.args.get("category") or "").strip()
    if category:
        if category not in ("商单", "原创"):
            return jsonify({"error": "视频分类必须为商单或原创"}), 400
        clauses.append("V.category = %s")
        params.append(category)
    vid = (request.args.get("vid") or "").strip()
    if vid:
        clauses.append("V.content_id = %s")
        params.append(vid)
    days = request.args.get("days", "")
    if days.isdigit() and int(days) > 0:
        clauses.append("V.add_ts >= %s")
        params.append(int(time.time()) - int(days) * 86400)
    publish_days = request.args.get("publish_days", "")
    if publish_days.isdigit() and int(publish_days) > 0:
        clauses.append("V.publish_time >= %s")
        params.append(int(time.time()) - int(publish_days) * 86400)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM {_content_union()}{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"SELECT src_table, platform, content_id, title, url, upname, add_ts, "
                f"view_count, like_count, collect_count, share_count, comment_count, duration, publish_time, category "
                f"FROM {_content_union()}{where} ORDER BY add_ts DESC LIMIT %s OFFSET %s",
                params + [size, (page - 1) * size])
            rows = cur.fetchall()
            # 评论数直接使用内容表采集的接口字段；负面/意向仍按已入库评论统计。
            cur.execute(f"""
                SELECT src_table, content_id, SUM(neg) neg, SUM(intent) intent
                FROM {_union()} GROUP BY src_table, content_id
            """)
            cmap = {(r["src_table"], str(r["content_id"])): r for r in cur.fetchall()}
    for r in rows:
        st = cmap.get((r["src_table"], str(r["content_id"])))
        r["neg_count"] = int(st["neg"] or 0) if st else 0
        r["intent_count"] = int(st["intent"] or 0) if st else 0
    return jsonify({"rows": rows, "total": total, "page": page, "size": size})


# ============================================================
# API: 告警中心
# ============================================================
HIGH_WORDS = ("骗", "垃圾", "投诉", "曝光", "维权", "退钱", "造假", "劣质", "恶心", "避雷", "智商税", "坑")


@bp.get("/api/alerts")
def api_alerts():
    args = dict(request.args)
    args.setdefault("sentiment", "negative")
    where, params = _apply_comment_filters(args)
    level = request.args.get("level", "")
    clauses_extra, p_extra = [], []
    if level == "high":
        like = " OR ".join(["content LIKE %s"] * len(HIGH_WORDS))
        clauses_extra.append(f"({like})")
        p_extra.extend(f"%{w}%" for w in HIGH_WORDS)
    elif level == "normal":
        like = " AND ".join(["content NOT LIKE %s"] * len(HIGH_WORDS))
        clauses_extra.append(f"({like})")
        p_extra.extend(f"%{w}%" for w in HIGH_WORDS)
    if clauses_extra:
        where += (" AND " if where else " WHERE ") + " AND ".join(clauses_extra)
        params += p_extra
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM {_union()}{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"SELECT * FROM {_union()}{where} ORDER BY ctime DESC LIMIT %s OFFSET %s",
                params + [size, (page - 1) * size])
            rows = _with_platform(cur.fetchall())
    for r in rows:
        r["level"] = "high" if any(w in (r["content"] or "") for w in HIGH_WORDS) else "normal"
        r["alert_type"] = "负面情感告警"
    return jsonify({"rows": rows, "total": total, "page": page, "size": size})


# ============================================================
# API: 智能分析工作台
# ============================================================
@bp.get("/api/workbench")
def api_workbench():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT src_table,
                       SUM(CASE WHEN analyzed=0 AND handled=0 THEN 1 ELSE 0 END) pending,
                       SUM(analyzed=1) analyzed,
                       SUM(neg=1) neg, SUM(intent=1) intent
                FROM {_union()} GROUP BY src_table
            """)
            stats = []
            for r in cur.fetchall():
                r["platform"] = PLATFORM_OF.get(r["src_table"], r["src_table"])
                stats.append(r)
            tab = request.args.get("tab", "pending")
            page = max(1, int(request.args.get("page", 1)))
            size = max(1, min(100, int(request.args.get("size", 20))))
            where_extra = " WHERE U.analyzed=0 AND U.handled=0" if tab == "pending" else \
                          " WHERE U.analyzed=1"
            platform = request.args.get("platform", "")
            params = []
            if platform:
                matched = [t for t in TABLES if PLATFORM_OF[t] == platform]
                if matched:
                    where_extra += " AND U.src_table IN (%s)" % ",".join(["%s"] * len(matched))
                    params.extend(matched)
            # 意图筛选(仅对已分析数据有意义)
            intent_f = request.args.get("intent", "")
            if intent_f == "1":
                where_extra += " AND U.intent=1"
            elif intent_f == "0":
                where_extra += " AND U.intent=0"
            # 回复推荐状态:ready=已生成 / failed=分析失败 / none=无回复
            reply_st = request.args.get("reply_status", "")
            if reply_st == "ready":
                where_extra += (" AND U.reply IS NOT NULL AND U.reply != ''"
                                " AND U.reply NOT LIKE '⚠ 分析失败%%'")
            elif reply_st == "failed":
                where_extra += " AND U.reply LIKE '⚠ 分析失败%%'"
            elif reply_st == "none":
                where_extra += " AND (U.reply IS NULL OR U.reply = '')"
            # 时间范围筛选(按评论时间 ctime,支持 YYYY-MM-DD 或时间戳)
            def _to_ts(v, end_of_day=False):
                v = str(v).strip()
                if v.isdigit():
                    n = int(v)
                    return n // 1000 if n > 100000000000 else n
                try:
                    from datetime import datetime
                    dt = datetime.strptime(v[:10], "%Y-%m-%d")
                    dt = dt.replace(hour=23, minute=59, second=59) if end_of_day else dt
                    return int(dt.timestamp())
                except ValueError:
                    return None
            start_ts = _to_ts(request.args.get("start", "")) if request.args.get("start") else None
            end_ts = _to_ts(request.args.get("end", ""), end_of_day=True) if request.args.get("end") else None
            if start_ts:
                where_extra += " AND U.ctime >= %s"
                params.append(start_ts)
            if end_ts:
                where_extra += " AND U.ctime <= %s"
                params.append(end_ts)
            cur.execute(f"SELECT COUNT(*) c FROM {_union()}{where_extra}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"SELECT * FROM {_union()}{where_extra} ORDER BY ctime DESC LIMIT %s OFFSET %s",
                params + [size, (page - 1) * size])
            rows = _with_platform(cur.fetchall())
    calibration = {"analyzed": 0, "corrected": 0, "coverage": 0,
                   "reason_stats": [], "recent": []}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) corrected FROM scrm_comment_calibration")
                corrected = int(cur.fetchone()["corrected"] or 0)
                cur.execute(f"SELECT SUM(analyzed=1) analyzed FROM {_union()}")
                analyzed = int(cur.fetchone()["analyzed"] or 0)
                cur.execute("SELECT correction_reason reason, COUNT(*) total FROM scrm_comment_calibration GROUP BY correction_reason ORDER BY total DESC")
                reasons = cur.fetchall()
                calibration.update(analyzed=analyzed, corrected=corrected,
                                   coverage=round(corrected * 100 / analyzed, 1) if analyzed else 0,
                                   reason_stats=reasons)
    except Exception:
        logger.exception("读取评论校准统计失败")
    return jsonify({"stats": stats, "rows": rows, "total": total,
                    "page": page, "size": size, "tab": tab, "calibration": calibration})


@bp.get("/api/workbench/calibrations")
def api_workbench_calibrations():
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) total FROM scrm_comment_calibration")
            total = int(cur.fetchone()["total"] or 0)
            cur.execute("""SELECT src_table,src_pk,model_intents,corrected_intents,
              model_sentiment,corrected_sentiment,correction_reason,correction_note,
              context_title,context_parent_comment,created_ts
              FROM scrm_comment_calibration ORDER BY created_ts DESC LIMIT %s OFFSET %s""",
                        (size, (page - 1) * size))
            rows = cur.fetchall()
    for row in rows:
        row["platform"] = PLATFORM_OF.get(row.get("src_table"), row.get("src_table"))
        try:
            cur_table = row["src_table"]
            cur.execute(f"SELECT CAST(content AS CHAR) content FROM `{cur_table}` WHERE id=%s", (row["src_pk"],))
            found = cur.fetchone()
            row["content"] = found["content"] if found else ""
        except Exception:
            row["content"] = ""
        for key in ("model_intents", "corrected_intents"):
            try: row[key] = json.loads(row[key] or "[]")
            except Exception: row[key] = []
    return jsonify({"rows": rows, "total": total, "page": page, "size": size})


# ============================================================
# API: 线索
# ============================================================
def _lead_outer_where(args):
    clauses, params = [], []
    status = args.get("status", "")
    if status:
        clauses.append("L.status=%s")
        params.append(status)
    owner = args.get("owner", "")
    if owner:
        clauses.append("L.owner=%s")
        params.append(owner)
    platform = args.get("platform", "")
    if platform:
        matched = [t for t in TABLES if PLATFORM_OF[t] == platform]
        if matched:
            clauses.append("C.src_table IN (%s)" % ",".join(["%s"] * len(matched)))
            params.extend(matched)
    q = (args.get("q") or "").strip()
    if q:
        clauses.append("C.content LIKE %s")
        params.append(f"%{q}%")
    overdue = args.get("overdue", "")
    if overdue == "1":
        clauses.append("L.status NOT IN ('won','lost') AND L.due_ts < %s")
        params.append(int(time.time()))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


@bp.get("/api/leads/overview")
def api_leads_overview():
    with get_conn() as conn:
        with conn.cursor() as cur:
            _provision_leads(cur)
            cur.execute("""
                SELECT status, COUNT(*) c,
                       SUM(CASE WHEN due_ts < %s AND status NOT IN ('won','lost')
                                THEN 1 ELSE 0 END) overdue
                FROM scrm_lead GROUP BY status
            """, (int(time.time()),))
            by_status = {r["status"]: r for r in cur.fetchall()}
            cur.execute("""
                SELECT owner, COUNT(*) c,
                       SUM(status='won') won,
                       SUM(status NOT IN ('won','lost')) active
                FROM scrm_lead WHERE owner IS NOT NULL GROUP BY owner ORDER BY c DESC
            """)
            by_owner = cur.fetchall()
            cur.execute(f"""
                SELECT L.id, L.owner, L.due_ts, L.status, C.nickname, C.content, C.ctime,
                       C.src_table AS src_table
                FROM scrm_lead L JOIN {_union("C")}
                  ON C.src_table=L.src_table AND C.src_pk=L.src_pk
                WHERE L.status NOT IN ('won','lost') AND L.due_ts < %s
                ORDER BY L.due_ts LIMIT 8
            """, (int(time.time()),))
            overdue_list = _with_platform(cur.fetchall())
    statuses = []
    total = sum(int(v["c"]) for v in by_status.values())
    won = int(by_status.get("won", {}).get("c", 0) or 0)
    for key, label, color in LEAD_STATUS:
        r = by_status.get(key, {"c": 0, "overdue": 0})
        statuses.append({"key": key, "label": label, "color": color,
                         "count": int(r["c"]), "overdue": int(r["overdue"] or 0)})
    return jsonify({"total": total, "won": won,
                    "win_rate": round(won * 100 / total, 1) if total else 0,
                    "statuses": statuses, "by_owner": by_owner,
                    "overdue_list": overdue_list, "team": TEAM})


@bp.get("/api/leads")
def api_leads():
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    where, params = _lead_outer_where(request.args)
    sql_base = f"""
        FROM scrm_lead L JOIN {_union("C")}
          ON C.src_table=L.src_table AND C.src_pk=L.src_pk
        LEFT JOIN {_content_union()}
          ON V.src_table=L.src_table AND V.content_id=C.content_id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            _provision_leads(cur)
            cur.execute(f"SELECT COUNT(*) c {sql_base}{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(f"""
                SELECT L.id, L.status, L.owner, L.intent_desc, L.note,
                       L.last_follow_ts, L.due_ts, L.created_ts, L.updated_ts,
                       C.src_table AS src_table, C.src_pk AS src_pk,
                       C.nickname, C.content, C.ctime,
                       C.neg, C.intent, C.content_id, C.reply,
                       V.title AS video_title, V.upname
                {sql_base}{where}
                ORDER BY L.updated_ts DESC LIMIT %s OFFSET %s
            """, params + [size, (page - 1) * size])
            rows = _with_platform(cur.fetchall())
    now = int(time.time())
    for r in rows:
        r["overdue"] = r["status"] not in ("won", "lost") and r["due_ts"] and r["due_ts"] < now
    return jsonify({"rows": rows, "total": total, "page": page, "size": size,
                    "status_map": LEAD_STATUS_MAP, "team": TEAM})


def _load_lead(cur, lid):
    cur.execute(f"""
        SELECT L.*, C.nickname, C.content, C.ctime,
               C.src_table AS src_table, C.src_pk AS src_pk,
               C.content_id, C.neg, C.reply,
               V.title AS video_title, V.url AS video_url, V.upname
        FROM scrm_lead L
        JOIN {_union("C")} ON C.src_table=L.src_table AND C.src_pk=L.src_pk
        LEFT JOIN {_content_union()} ON V.src_table=L.src_table AND V.content_id=C.content_id
        WHERE L.id=%s
    """, (lid,))
    lead = cur.fetchone()
    if not lead:
        return None
    lead["platform"] = PLATFORM_OF.get(lead["src_table"], "?")
    lead["status_label"] = LEAD_STATUS_MAP.get(lead["status"], lead["status"])
    cur.execute(
        "SELECT * FROM scrm_lead_follow WHERE lead_id=%s ORDER BY created_ts DESC", (lid,))
    lead["follows"] = cur.fetchall()
    return lead


@bp.get("/api/leads/locate")
def api_lead_locate():
    """按来源评论定位线索(评论详情页跳转用)"""
    table = request.args.get("table", "")
    pk = request.args.get("pk", "")
    if table not in MAP or not str(pk).isdigit():
        return jsonify({"lead_id": None})
    with get_conn() as conn:
        with conn.cursor() as cur:
            _provision_leads(cur)
            cur.execute("SELECT id FROM scrm_lead WHERE src_table=%s AND src_pk=%s",
                        (table, int(pk)))
            r = cur.fetchone()
    return jsonify({"lead_id": r["id"] if r else None})


@bp.get("/api/leads/<int:lid>")
def api_lead_detail(lid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            lead = _load_lead(cur, lid)
    if not lead:
        return jsonify({"error": "线索不存在"}), 404
    return jsonify({"lead": lead, "statuses": LEAD_STATUS, "team": TEAM})


@bp.post("/api/leads/<int:lid>/update")
def api_lead_update(lid):
    body = request.get_json(silent=True) or {}
    sets, params = [], []
    if body.get("status") in LEAD_STATUS_MAP:
        sets.append("status=%s"); params.append(body["status"])
    if "owner" in body:
        sets.append("owner=%s"); params.append((body["owner"] or None))
    if "intent_desc" in body:
        sets.append("intent_desc=%s"); params.append(str(body["intent_desc"] or "")[:255])
    if "note" in body:
        sets.append("note=%s"); params.append(str(body["note"] or "")[:500])
    if "due_ts" in body:
        try:
            sets.append("due_ts=%s"); params.append(int(body["due_ts"]))
        except (TypeError, ValueError):
            pass
    if not sets:
        return jsonify({"error": "无可更新字段"}), 400
    sets.append("updated_ts=%s"); params.append(int(time.time()))
    params.append(lid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE scrm_lead SET {', '.join(sets)} WHERE id=%s", params)
            if body.get("status"):
                cur.execute(
                    "INSERT INTO scrm_lead_follow (lead_id, action, content, creator, created_ts) "
                    "VALUES (%s,'status',%s,%s,%s)",
                    (lid, f"状态变更为「{LEAD_STATUS_MAP[body['status']]}」",
                     body.get("creator", "运营"), int(time.time())))
        conn.commit()
    return jsonify({"ok": True, "affected": n})


@bp.post("/api/leads/<int:lid>/follow")
def api_lead_follow(lid):
    body = request.get_json(silent=True) or {}
    content = str(body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "跟进内容不能为空"}), 400
    action = body.get("action", "note")
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scrm_lead_follow (lead_id, action, content, creator, created_ts) "
                "VALUES (%s,%s,%s,%s,%s)",
                (lid, action, content[:1000], body.get("creator", "运营"), now))
            cur.execute("""
                UPDATE scrm_lead SET last_follow_ts=%s, updated_ts=%s,
                  due_ts=IF(due_ts IS NULL OR due_ts<%s, %s+3*86400, due_ts)
                WHERE id=%s
            """, (now, now, now, now, lid))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/leads/alerts")
def api_lead_alerts():
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            _provision_leads(cur)
            # 超时未跟进
            cur.execute(f"""
                SELECT L.id, L.owner, L.status, L.due_ts, L.last_follow_ts,
                       C.nickname, C.content, C.ctime, C.src_table AS src_table, 'overdue' AS alert_type
                FROM scrm_lead L JOIN {_union("C")}
                  ON C.src_table=L.src_table AND C.src_pk=L.src_pk
                WHERE L.status NOT IN ('won','lost') AND L.due_ts < %s
                ORDER BY L.due_ts LIMIT 200
            """, (now,))
            overdue = _with_platform(cur.fetchall())
            # 跟进中但 7 天无动作
            cur.execute(f"""
                SELECT L.id, L.owner, L.status, L.due_ts, L.last_follow_ts,
                       C.nickname, C.content, C.ctime, C.src_table AS src_table, 'stuck' AS alert_type
                FROM scrm_lead L JOIN {_union("C")}
                  ON C.src_table=L.src_table AND C.src_pk=L.src_pk
                WHERE L.status IN ('contacted','following','feedback')
                  AND L.last_follow_ts IS NOT NULL AND L.last_follow_ts < %s
                ORDER BY L.last_follow_ts LIMIT 50
            """, (now - 7 * 86400,))
            stuck = _with_platform(cur.fetchall())
            unassigned = []
            cur.execute(f"""
                SELECT L.id, L.status, C.nickname, C.content, C.ctime,
                       C.src_table AS src_table
                FROM scrm_lead L JOIN {_union("C")}
                  ON C.src_table=L.src_table AND C.src_pk=L.src_pk
                WHERE L.owner IS NULL ORDER BY L.id DESC LIMIT 50
            """)
            unassigned = _with_platform(cur.fetchall())
    return jsonify({"overdue": overdue, "stuck": stuck, "unassigned": unassigned})


# ============================================================
# API: 品牌触达(品牌=合作媒介;声量来源=商单展示视频链接)
# ============================================================
_URL_TOKEN_RE = re.compile(r"(BV[0-9A-Za-z]{10}|[0-9a-f]{16,}|\d{8,})")


def _norm_url(u):
    """归一化:去协议/query/末尾斜杠/www,用于视频链接比对"""
    try:
        p = urlsplit((u or "").strip())
        return p.netloc.lower().replace("www.", "") + re.sub(r"/+$", "", p.path)
    except Exception:
        return (u or "").strip()


def _url_tokens(u):
    return set(_URL_TOKEN_RE.findall(_norm_url(u).upper()))


def _load_content_index(cur):
    """全量内容索引:候选 + 按平台分组(存下标) + URL token 反向索引"""
    cur.execute(f"SELECT src_table, platform, content_id, title, url, upname, add_ts "
                f"FROM {_content_union()}")
    cands = cur.fetchall()
    by_plat, tok_idx = {}, {}
    for i, c in enumerate(cands):
        by_plat.setdefault(c.get("platform") or "", []).append(i)
        for t in _url_tokens(c.get("url") or ""):
            tok_idx.setdefault(t, []).append(i)
    return cands, by_plat, tok_idx


def _match_content(dv, cands, by_plat, tok_idx):
    """把一条商单展示视频链接匹配到内容库,返回候选 dict 或 None"""
    url = (dv.get("url") or "").strip()
    if not url.lower().startswith("http"):
        return None
    nu = _norm_url(url)
    toks = _url_tokens(url)
    ids = set()
    for t in toks:
        ids.update(tok_idx.get(t, []))
    for i in by_plat.get(dv.get("platform") or "", []):
        ids.add(i)
    best, best_score = None, 0
    for i in ids:
        c = cands[i]
        cu = c.get("url") or ""
        if not cu:
            continue
        if _norm_url(cu) == nu:
            score = 100
        else:
            inter = toks & _url_tokens(cu)
            score = 30 if inter else 0
        if score > best_score:
            best, best_score = c, score
    # 兜底:同平台标题完全一致
    if best_score < 20 and (dv.get("title") or "").strip():
        t = dv["title"].strip()
        for i in by_plat.get(dv.get("platform") or "", []):
            c = cands[i]
            if (c.get("title") or "").strip() == t:
                return c
    return best if best_score >= 20 else None


def _media_scope(cur, media, cands, by_plat, tok_idx, with_samples=False):
    """合作媒介(品牌) -> 关联商单 -> 展示视频链接 -> 内容库匹配 -> 评论聚合"""
    cur.execute("SELECT id, name, status, brand, product FROM scrm_deal "
                "WHERE media_company=%s OR id=%s ORDER BY id",
                (media["name"], media.get("src_deal_id")))
    deals = cur.fetchall()
    deal_ids = [d["id"] for d in deals]
    videos, keys = [], set()
    if deal_ids:
        ph = ",".join(["%s"] * len(deal_ids))
        cur.execute(f"SELECT * FROM scrm_deal_video WHERE deal_id IN ({ph}) ORDER BY id", deal_ids)
        for dv in cur.fetchall():
            row = dict(dv)
            m = _match_content(row, cands, by_plat, tok_idx)
            if m:
                keys.add((m["src_table"], str(m["content_id"])))
                row.update(matched=1, m_title=m.get("title"), m_url=m.get("url"),
                           m_upname=m.get("upname"), m_platform=m.get("platform"),
                           src_table=m["src_table"], content_id=str(m["content_id"]))
            else:
                row.update(matched=0, m_title=None, m_url=None, m_upname=None,
                           m_platform=None, src_table=None, content_id=None)
            videos.append(row)
    valid = [v for v in videos if (v.get("url") or "").lower().startswith("http")]
    stats = {"deal_count": len(deals), "video_count": len(valid), "matched_count": len(keys),
             "comment_count": 0, "intent_count": 0, "negative_count": 0}
    samples = []
    if keys:
        kl = list(keys)
        ph = ",".join(["(%s,%s)"] * len(kl))
        args = [x for k in kl for x in k]
        cur.execute(f"SELECT COUNT(*) c, SUM(intent) i, SUM(neg) n FROM {_union()} "
                    f"WHERE (U.src_table,U.content_id) IN ({ph})", args)
        r = cur.fetchone()
        stats.update(comment_count=int(r["c"] or 0),
                     intent_count=int(r["i"] or 0), negative_count=int(r["n"] or 0))
        if with_samples:
            cur.execute(
                f"SELECT U.src_table, U.src_pk, U.nickname, U.content, U.ctime, U.neg, U.intent "
                f"FROM {_union()} WHERE (U.src_table,U.content_id) IN ({ph}) "
                f"ORDER BY U.ctime DESC LIMIT 30", args)
            samples = _with_platform(cur.fetchall())
    stats.update(deals=deals, videos=videos, comment_samples=samples)
    return stats


@bp.get("/api/brands")
def api_brands():
    q = (request.args.get("q") or "").strip()
    where, params = "", []
    if q:
        where = " WHERE name LIKE %s OR party_a LIKE %s"
        params = [f"%{q}%", f"%{q}%"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM scrm_media{where} ORDER BY updated_ts DESC", params)
            brands = cur.fetchall()
            cands, by_plat, tok_idx = _load_content_index(cur)
            for b in brands:
                sc = _media_scope(cur, b, cands, by_plat, tok_idx)
                for k in ("deal_count", "video_count", "matched_count",
                          "comment_count", "intent_count", "negative_count"):
                    b[k] = sc[k]
    return jsonify({"rows": brands, "status_map": BRAND_STATUS_MAP, "team": TEAM})


@bp.get("/api/brands/<int:bid>")
def api_brand_detail(bid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_media WHERE id=%s", (bid,))
            brand = cur.fetchone()
            if not brand:
                return jsonify({"error": "品牌(合作媒介)不存在"}), 404
            cands, by_plat, tok_idx = _load_content_index(cur)
            sc = _media_scope(cur, brand, cands, by_plat, tok_idx, with_samples=True)
            brand.update({k: sc[k] for k in ("deal_count", "video_count", "matched_count",
                                             "comment_count", "intent_count", "negative_count")})
            cur.execute(
                "SELECT * FROM scrm_media_follow WHERE media_id=%s ORDER BY created_ts DESC",
                (bid,))
            follows = cur.fetchall()
    return jsonify({"brand": brand, "deals": sc["deals"], "video_samples": sc["videos"],
                    "comment_samples": sc["comment_samples"], "follows": follows,
                    "statuses": BRAND_STATUS, "team": TEAM})


@bp.post("/api/brands/<int:bid>/update")
def api_brand_update(bid):
    body = request.get_json(silent=True) or {}
    sets, params = [], []
    if body.get("status") in BRAND_STATUS_MAP:
        sets.append("status=%s"); params.append(body["status"])
    if "owner" in body:
        sets.append("owner=%s"); params.append((body["owner"] or None))
    if "background" in body:
        sets.append("background=%s"); params.append(str(body["background"] or "")[:1000])
    if not sets:
        return jsonify({"error": "无可更新字段"}), 400
    sets.append("updated_ts=%s"); params.append(int(time.time())); params.append(bid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE scrm_media SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()
    return jsonify({"ok": True, "affected": n})


@bp.post("/api/brands/<int:bid>/follow")
def api_brand_follow(bid):
    body = request.get_json(silent=True) or {}
    content = str(body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "跟进内容不能为空"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scrm_media_follow (media_id, content, next_plan, creator, created_ts) "
                "VALUES (%s,%s,%s,%s,%s)",
                (bid, content[:1000], str(body.get("next_plan") or "")[:255],
                 body.get("creator", "运营"), now))
            cur.execute("UPDATE scrm_media SET updated_ts=%s WHERE id=%s", (now, bid))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/brands/funnel")
def api_brand_funnel():
    """询单转化漏斗:基于 scrm_inquiry 状态,非品牌状态"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) c FROM scrm_inquiry GROUP BY status")
            by_status = {r["status"]: int(r["c"]) for r in cur.fetchall()}
            cur.execute("""
                SELECT sales, COUNT(*) total,
                       SUM(status IN ('有意向','合作成功')) intent,
                       SUM(status='合作成功') won
                FROM scrm_inquiry WHERE sales IS NOT NULL AND sales != ''
                GROUP BY sales ORDER BY total DESC
            """)
            by_sales = cur.fetchall()
    # 主漏斗:询单总数 → 有意向(含合作成功) → 合作成功
    total = sum(by_status.get(s, 0) for s in INQUIRY_STATUSES)
    intent_n = by_status.get("有意向", 0) + by_status.get("合作成功", 0)
    won_n = by_status.get("合作成功", 0)
    lost_n = by_status.get("未合作", 0) + by_status.get("无意向", 0)
    stages = [
        {"key": "跟进中", "label": "跟进中", "color": "#6b7280", "count": by_status.get("跟进中", 0)},
        {"key": "有意向", "label": "有意向", "color": "#d97706", "count": by_status.get("有意向", 0)},
        {"key": "合作成功", "label": "合作成功", "color": "#16a34a", "count": won_n},
        {"key": "未合作", "label": "未合作", "color": "#9ca3af", "count": by_status.get("未合作", 0)},
        {"key": "无意向", "label": "无意向", "color": "#9ca3af", "count": by_status.get("无意向", 0)},
    ]
    funnel = [
        {"label": "询单总数", "value": total, "color": "#6b7280"},
        {"label": "有意向(含已合作)", "value": intent_n, "color": "#4f46e5"},
        {"label": "合作成功", "value": won_n, "color": "#16a34a"},
    ]
    top = funnel[0]["value"] or 1
    for i, f in enumerate(funnel):
        f["rate"] = round(f["value"] * 100 / top, 1)
        if i > 0:
            prev = funnel[i - 1]["value"] or 1
            f["prev_rate"] = round(f["value"] * 100 / prev, 1)
    return jsonify({"stages": stages, "funnel": funnel, "by_owner": by_sales, "lost": lost_n})


# ============================================================
# API: 商务询单(飞书同步) / 商单 / 合作媒介
# ============================================================
@bp.get("/api/inquiries")
def api_inquiries():
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    clauses, params = [], []
    for key, col in [("q", "name"), ("brand", "brand"), ("sales", "sales"),
                     ("media", "media_company")]:
        v = (request.args.get(key) or "").strip()
        if v:
            clauses.append(f"{col} LIKE %s")
            params.append(f"%{v}%")
    status = (request.args.get("status") or "").strip()
    if status:
        clauses.append("status=%s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    follow_cutoff = int(time.time()) - 5 * 86400
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 二核后 5 天仍未明确合作或拒绝，自动结束跟进并标记为无意向。
            cur.execute("""UPDATE scrm_inquiry
                          SET status='无意向', synced_ts=%s
                          WHERE follow_stage='second' AND status IN ('跟进中','有意向')
                            AND follow_stage_ts IS NOT NULL AND follow_stage_ts < %s""",
                        (int(time.time()), follow_cutoff))
            conn.commit()
            cur.execute(f"SELECT COUNT(*) c FROM scrm_inquiry{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(f"""
                SELECT * FROM scrm_inquiry{where}
                ORDER BY inquiry_ts DESC, id DESC LIMIT %s OFFSET %s
            """, params + [size, (page - 1) * size])
            rows = cur.fetchall()
            inquiry_ids = [r["id"] for r in rows]
            linked_deals = set()
            if inquiry_ids:
                ph = ",".join(["%s"] * len(inquiry_ids))
                cur.execute(f"SELECT inquiry_id FROM scrm_deal WHERE inquiry_id IN ({ph})", inquiry_ids)
                linked_deals = {r["inquiry_id"] for r in cur.fetchall()}
            for row in rows:
                row["deal_linked"] = row["id"] in linked_deals
                stage = row.get("follow_stage") or "registered"
                if row.get("status") == "有意向":
                    row["follow_node"] = "询单"
                    row["follow_timeout_label"] = ""
                elif row.get("status") in ("无意向", "未合作"):
                    row["follow_node"] = "无意向"
                    row["follow_timeout_label"] = ""
                elif row.get("status") == "合作成功":
                    row["follow_node"] = "合作成功"
                    row["follow_timeout_label"] = ""
                elif stage == "second":
                    row["follow_node"] = "二核"
                    base_ts = row.get("follow_stage_ts") or row.get("synced_ts") or row.get("inquiry_ts")
                    row["follow_timeout_label"] = "登记超时" if base_ts and int(base_ts) < int(time.time()) - 5 * 86400 else ""
                else:
                    row["follow_node"] = "询单"
                    base_ts = row.get("inquiry_ts") or row.get("synced_ts")
                    row["follow_timeout_label"] = "2核超时" if base_ts and int(base_ts) < int(time.time()) - 5 * 86400 else ""
                row["follow_timeout"] = bool(row["follow_timeout_label"])
            cur.execute("SELECT MAX(synced_ts) m FROM scrm_inquiry")
            last_sync = cur.fetchone()["m"]
            cur.execute("SELECT status, COUNT(*) c FROM scrm_inquiry GROUP BY status")
            by_status = {r["status"]: int(r["c"]) for r in cur.fetchall()}
    return jsonify({"rows": rows, "total": total, "page": page, "size": size,
                    "statuses": INQUIRY_STATUSES, "by_status": by_status,
                    "last_sync": last_sync, "team": TEAM})


@bp.post("/api/inquiries/create")
def api_inquiry_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:255]
    if not name:
        return jsonify({"error": "询单名称必填"}), 400
    now = int(time.time())
    fs_id = f"manual-{int(time.time() * 1000)}"
    fields = {k: (str(body.get(k) or "").strip()[:limit] or None) for k, limit in {
        "brand": 128, "product": 255, "category": 64, "media_company": 128,
        "contact": 64, "wechat": 128, "sales": 64, "coop_types": 255,
        "platform_accounts": 500, "follow_detail": 1000, "remark": 255}.items()}
    status = str(body.get("status") or "跟进中")
    if status not in INQUIRY_STATUSES:
        status = "跟进中"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO scrm_inquiry
                (fs_record_id,inquiry_no,name,brand,product,category,media_company,contact,wechat,sales,
                 coop_types,platform_accounts,follow_detail,remark,status,created_ts,synced_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (fs_id, fs_id.replace("manual-", ""), name, fields["brand"], fields["product"], fields["category"],
                 fields["media_company"], fields["contact"], fields["wechat"], fields["sales"], fields["coop_types"],
                 fields["platform_accounts"], fields["follow_detail"], fields["remark"], status, now, now))
            iid = cur.lastrowid
            if status == "合作成功":
                cur.execute("SELECT * FROM scrm_inquiry WHERE id=%s", (iid,))
                _ensure_won_artifacts(cur, cur.fetchone())
        conn.commit()
    return jsonify({"ok": True, "id": iid})


@bp.post("/api/inquiries/send-wechat")
def api_inquiries_send_wechat():
    body = request.get_json(silent=True) or {}
    try:
        ids = [int(x) for x in (body.get("ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "询单选择无效"}), 400
    text = str(body.get("text") or "").strip()
    if not ids:
        return jsonify({"error": "请至少选择一条询单"}), 400
    if not text:
        return jsonify({"error": "消息内容不能为空"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id, wechat FROM scrm_inquiry WHERE id IN ({ph})", ids)
            rows = cur.fetchall()
    wxids = list(dict.fromkeys(str(r.get("wechat") or "").strip() for r in rows if str(r.get("wechat") or "").strip()))
    if not wxids:
        return jsonify({"error": "所选询单没有联系人微信号"}), 400
    try:
        wechat_api_url = os.environ.get(
            "WECHATAUTO_API_URL", "http://192.168.11.200:18765/v1/messages")
        wechat_api_token = os.environ.get("WECHATAUTO_API_TOKEN", "")
        headers = ({"Authorization": f"Bearer {wechat_api_token}", "X-API-Key": wechat_api_token}
                   if wechat_api_token else {})
        resp = requests.post(wechat_api_url, headers=headers,
                             json={"wxids": wxids, "text": text, "confirm": True}, timeout=20)
        data = resp.json() if resp.content else {}
        if not resp.ok:
            return jsonify({"error": (data.get("error") or data.get("message")
                                      or f"微信接口返回 HTTP {resp.status_code}")}), 502
    except requests.ConnectionError:
        return jsonify({"error": "微信接口未启动或端口不可用，请检查 WECHATAUTO_API_URL 与微信服务状态"}), 502
    except requests.Timeout:
        return jsonify({"error": "微信接口连接超时，请检查本地微信服务状态"}), 504
    except requests.RequestException as exc:
        return jsonify({"error": f"微信接口调用失败: {str(exc)[:120]}"}), 502
    return jsonify({"ok": True, "count": len(wxids), "result": data})


@bp.get("/api/business-wechat")
def api_business_wechat():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_business_wechat ORDER BY updated_ts DESC, id DESC")
            rows = cur.fetchall()
            media_names = {r["media_company"] for r in rows if r.get("media_company")}
            media_exists = set()
            if media_names:
                mph = ",".join(["%s"] * len(media_names))
                cur.execute(f"SELECT name FROM scrm_media WHERE name IN ({mph})", list(media_names))
                media_exists = {r["name"] for r in cur.fetchall()}
            for row in rows:
                row["media_exists"] = row.get("media_company") in media_exists
    return jsonify({"rows": rows})


@bp.post("/api/business-wechat/save")
def api_business_wechat_save():
    body = request.get_json(silent=True) or {}
    wechat, member = str(body.get("wechat") or "").strip()[:128], str(body.get("member") or "").strip()[:64]
    if not wechat or not member:
        return jsonify({"error": "微信号和商务组员必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                if body.get("id"):
                    cur.execute("UPDATE scrm_business_wechat SET wechat=%s,member=%s,updated_ts=%s WHERE id=%s",
                                (wechat, member, now, int(body["id"])))
                else:
                    cur.execute("""INSERT INTO scrm_business_wechat (wechat,member,created_ts,updated_ts)
                                 VALUES (%s,%s,%s,%s)
                                 ON DUPLICATE KEY UPDATE member=VALUES(member),updated_ts=VALUES(updated_ts)""",
                                (wechat, member, now, now))
            except Exception as exc:
                return jsonify({"error": str(exc)[:160]}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/categories")
def api_categories():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_category ORDER BY status DESC, name")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.post("/api/categories/save")
def api_categories_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:64]
    if not name:
        return jsonify({"error": "品类名称必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.get("id"):
                cur.execute("UPDATE scrm_category SET name=%s, status='active', updated_ts=%s WHERE id=%s",
                            (name, now, int(body["id"])))
            else:
                cur.execute("""INSERT INTO scrm_category (name,created_ts,updated_ts) VALUES (%s,%s,%s)
                             ON DUPLICATE KEY UPDATE status='active',updated_ts=VALUES(updated_ts)""", (name, now, now))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/kpi-configs")
def api_kpi_configs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_kpi_config ORDER BY platform")
            rows = cur.fetchall()
    return jsonify({"rows": rows, "platforms": KPI_PLATFORMS})


@bp.post("/api/kpi-configs/save")
def api_kpi_config_save():
    body = request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "").strip()[:32]
    if platform not in KPI_PLATFORMS:
        return jsonify({"error": "请选择有效平台"}), 400
    try:
        cpme = Decimal(str(body.get("cpme_ratio")))
        cpe = Decimal(str(body.get("cpe_ratio")))
        original_base_value = int(body.get("original_base_value") or 0)
        if cpme <= 0 or cpe <= 0 or original_base_value < 0:
            raise ValueError
    except (TypeError, ValueError, ArithmeticError):
        return jsonify({"error": "CPME比例和CPE比例必须为大于0的数字"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                if body.get("id"):
                    cur.execute("UPDATE scrm_kpi_config SET platform=%s,cpme_ratio=%s,cpe_ratio=%s,original_base_value=%s,updated_ts=%s WHERE id=%s",
                                (platform, cpme, cpe, original_base_value, now, int(body["id"])))
                else:
                    cur.execute("""INSERT INTO scrm_kpi_config (platform,cpme_ratio,cpe_ratio,original_base_value,created_ts,updated_ts)
                                 VALUES (%s,%s,%s,%s,%s,%s)
                                 ON DUPLICATE KEY UPDATE cpme_ratio=VALUES(cpme_ratio),cpe_ratio=VALUES(cpe_ratio),original_base_value=VALUES(original_base_value),updated_ts=VALUES(updated_ts)""",
                                (platform, cpme, cpe, original_base_value, now, now))
            except Exception as exc:
                return jsonify({"error": str(exc)[:160]}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/inquiries/sync")
def api_inquiries_sync():
    try:
        stat = sync_feishu_inquiries()
    except Exception as e:
        return jsonify({"error": f"同步失败:{str(e)[:150]}"}), 502
    return jsonify({"ok": True, **stat})


@bp.post("/api/inquiries/<int:iid>/status")
def api_inquiry_status(iid):
    body = request.get_json(silent=True) or {}
    status = str(body.get("status") or "")
    if status not in INQUIRY_STATUSES:
        return jsonify({"error": "非法状态"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE scrm_inquiry SET status=%s, synced_ts=%s WHERE id=%s",
                            (status, int(time.time()), iid))
            if n:
                cur.execute("SELECT * FROM scrm_inquiry WHERE id=%s", (iid,))
                if status == "合作成功":
                    _ensure_won_artifacts(cur, cur.fetchone())
        conn.commit()
    if not n:
        return jsonify({"error": "询单不存在"}), 404
    return jsonify({"ok": True})


@bp.post("/api/inquiries/<int:iid>/update")
def api_inquiry_update(iid):
    body = request.get_json(silent=True) or {}
    allowed = {
        "name": 255, "brand": 128, "product": 255, "category": 64,
        "media_company": 128, "contact": 64, "wechat": 128, "sales": 64,
        "coop_types": 255, "platform_accounts": 500, "follow_detail": 1000,
        "remark": 255, "extras": 255,
    }
    sets, params = [], []
    for key, limit in allowed.items():
        if key in body:
            sets.append(f"`{key}`=%s")
            params.append(str(body.get(key) or "").strip()[:limit] or None)
    for key in ("amount", "rebate", "media_amount"):
        if key in body:
            try:
                value = Decimal(str(body.get(key))) if body.get(key) not in (None, "") else None
                if value is not None and value < 0:
                    raise ValueError
            except (TypeError, ValueError, ArithmeticError):
                return jsonify({"error": f"{key}必须为有效数字"}), 400
            sets.append(f"{key}=%s"); params.append(value)
    if "status" in body:
        status = str(body.get("status") or "")
        if status not in INQUIRY_STATUSES:
            return jsonify({"error": "非法状态"}), 400
        sets.append("status=%s"); params.append(status)
    if not sets:
        return jsonify({"error": "没有可更新字段"}), 400
    sets.append("synced_ts=%s"); params.append(int(time.time())); params.append(iid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE scrm_inquiry SET {', '.join(sets)} WHERE id=%s", params)
            if n:
                cur.execute("SELECT * FROM scrm_inquiry WHERE id=%s", (iid,))
                updated = cur.fetchone()
                if updated and str(body.get("follow_detail") or "").strip() and updated.get("status") in ("跟进中", "有意向") and (updated.get("follow_stage") or "registered") == "registered":
                    cur.execute("UPDATE scrm_inquiry SET follow_stage='second', follow_stage_ts=%s WHERE id=%s", (int(time.time()), iid))
                if updated and updated.get("status") == "合作成功":
                    _ensure_won_artifacts(cur, updated)
        conn.commit()
    if not n:
        return jsonify({"error": "询单不存在"}), 404
    return jsonify({"ok": True})


@bp.get("/api/deals")
def api_deals():
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    clauses, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        clauses.append("(D.name LIKE %s OR D.brand LIKE %s OR D.media_company LIKE %s)")
        params.extend([f"%{q}%"] * 3)
    status = (request.args.get("status") or "").strip()
    if status:
        clauses.append("D.status=%s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM scrm_deal D{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(f"""
                SELECT D.*, (SELECT COUNT(*) FROM scrm_deal_video V WHERE V.deal_id=D.id) video_count,
                       (SELECT COUNT(*) FROM scrm_deal_video V WHERE V.deal_id=D.id AND V.url IS NOT NULL AND V.url<>'' AND V.url LIKE 'http%%') published_video_count
                FROM scrm_deal D{where}
                ORDER BY D.updated_ts DESC LIMIT %s OFFSET %s
            """, params + [size, (page - 1) * size])
            rows = cur.fetchall()
    for row in rows:
        if row.get("status") == "ended":
            row["display_status"] = "已结束"
        elif row.get("status") == "canceled":
            row["display_status"] = "已取消"
        elif row.get("status") == "kpi_done":
            row["display_status"] = "KPI已完成"
        elif row.get("status") == "published" or row.get("published_video_count", 0):
            row["display_status"] = "已发布"
        else:
            row["display_status"] = DEAL_STATUS_MAP.get(row.get("status"), row.get("status"))
    return jsonify({"rows": rows, "total": total, "page": page, "size": size,
                    "status_map": DEAL_STATUS_MAP, "team": TEAM})


@bp.get("/api/deals/<int:did>")
def api_deal_detail(did):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
            if not deal:
                return jsonify({"error": "商单不存在"}), 404
            cur.execute("SELECT * FROM scrm_deal_video WHERE deal_id=%s ORDER BY id DESC", (did,))
            videos = cur.fetchall()
            kpi_cfg_cache = {}
            for video in videos:
                video["amount"] = video.get("amount") if video.get("amount") is not None else deal.get("amount")
                video["publish_ts"] = video.get("publish_ts")
                cfg_platform = deal.get("kpi_platform") or video.get("platform")
                if cfg_platform not in kpi_cfg_cache:
                    cur.execute("SELECT cpme_ratio, cpe_ratio, original_base_value FROM scrm_kpi_config WHERE platform=%s", (cfg_platform,))
                    kpi_cfg_cache[cfg_platform] = cur.fetchone() if cfg_platform else None
                kpi_cfg = kpi_cfg_cache.get(cfg_platform)
                if kpi_cfg and video.get("category") != "商单":
                    video["kpi_views"] = video["kpi_interactions"] = int(kpi_cfg["original_base_value"] or 0)
                elif kpi_cfg and video.get("amount") not in (None, ""):
                    video["kpi_views"], video["kpi_interactions"] = _calc_kpi(
                        video["amount"], kpi_cfg["cpme_ratio"], kpi_cfg["cpe_ratio"])
                else:
                    video["kpi_views"] = deal.get("kpi_views")
                    video["kpi_interactions"] = deal.get("kpi_interactions")
                video["actual_views"] = None
                video["actual_interactions"] = None
                table = next((t for t in TABLES if MAP[t]["platform"] == (video.get("platform") or "")), None)
                if table and video.get("url"):
                    m = MAP[table]
                    cur.execute(
                        f"SELECT *, CAST({m['ctitle']} AS CHAR) AS matched_title, CAST({m['cview']} AS CHAR) AS actual_views, CAST({m['cpubtime']} AS CHAR) AS actual_publish_ts "
                        f"FROM `{m['ctable']}` WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s LIMIT 1",
                        (video["url"], video["url"]))
                    metrics = cur.fetchone()
                    if metrics:
                        if not video.get("title") and metrics.get("matched_title"):
                            video["title"] = str(metrics["matched_title"])
                        if not video.get("publish_ts"):
                            video["publish_ts"] = _to_epoch(metrics.get("actual_publish_ts"))
                        video["actual_views"] = _metric_number(metrics.get("actual_views"))
                        interaction_total = 0
                        interaction_found = False
                        for key, value in metrics.items():
                            name = str(key).lower()
                            if any(token in name for token in ("like", "share", "comment", "danmu", "danmaku", "barrage", "collect", "favorite", "fav_", "forward", "coin", "replay", "reply", "interaction")) and not any(token in name for token in ("url", "text", "content")):
                                number = _metric_number(value)
                                if number is not None:
                                    interaction_total += number
                                    interaction_found = True
                        video["actual_interactions"] = interaction_total if interaction_found else None
                video["play_reached"] = bool(video.get("kpi_views") is not None and video.get("actual_views") is not None and video["actual_views"] >= int(video["kpi_views"]))
                video["interaction_reached"] = bool(video.get("kpi_interactions") is not None and video.get("actual_interactions") is not None and video["actual_interactions"] >= int(video["kpi_interactions"]))
                video["video_status"] = "已达标" if (
                    video.get("kpi_views") is not None and video.get("kpi_interactions") is not None and
                    video.get("actual_views") is not None and video.get("actual_interactions") is not None and
                    video["actual_views"] >= int(video["kpi_views"]) and
                    video["actual_interactions"] >= int(video["kpi_interactions"])
                ) else "未达标"
                if not video.get("title") and video.get("url"):
                    video["title"] = video.get("url")
            data_completed = bool(videos) and all(v.get("video_status") == "已达标" for v in videos)
            has_published_video = any(v.get("url") for v in videos)
            if data_completed and deal.get("status") not in ("ended", "canceled"):
                cur.execute("UPDATE scrm_deal SET status='kpi_done', completed_ts=COALESCE(completed_ts,%s), updated_ts=%s WHERE id=%s",
                            (int(time.time()), int(time.time()), did))
                deal["status"] = "kpi_done"
                deal["completed_ts"] = deal.get("completed_ts") or int(time.time())
            elif has_published_video and deal.get("status") not in ("ended", "kpi_done", "canceled"):
                cur.execute("UPDATE scrm_deal SET status='published', updated_ts=%s WHERE id=%s", (int(time.time()), did))
                deal["status"] = "published"
            inquiry = None
            if deal.get("inquiry_id"):
                cur.execute("SELECT id, inquiry_no, status, contact, wechat, follow_detail "
                            "FROM scrm_inquiry WHERE id=%s", (deal["inquiry_id"],))
                inquiry = cur.fetchone()
            cur.execute("SELECT * FROM scrm_kpi_config ORDER BY platform")
            kpi_configs = cur.fetchall()
        # 详情页自动推进的已发布/KPI已完成状态需要持久化，列表页才能读取到同一状态。
        conn.commit()
    deal["data_completed"] = data_completed
    deal["display_status"] = ("已结束" if deal.get("status") == "ended" else ("已取消" if deal.get("status") == "canceled" else ("KPI已完成" if data_completed else ("已发布" if has_published_video and deal.get("status") == "published" else deal.get("status")))))
    return jsonify({"deal": deal, "videos": videos, "inquiry": inquiry,
                    "status_map": DEAL_STATUS_MAP, "team": TEAM,
                    "kpi_configs": kpi_configs, "kpi_platforms": KPI_PLATFORMS})


@bp.post("/api/deals/<int:did>/accounts")
def api_deal_accounts(did):
    body = request.get_json(silent=True) or {}
    accounts = body.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        return jsonify({"error": "请至少选择一个平台账户"}), 400
    now = int(time.time())
    created = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_deal WHERE id=%s", (did,))
            base = cur.fetchone()
            if not base:
                return jsonify({"error": "商单不存在"}), 404
            for item in accounts:
                platform = str(item.get("platform") or "").strip()[:32]
                account = str(item.get("account") or "").strip()[:128]
                if not platform or not account:
                    continue
                new_name = f"{base['name']}-{account}"
                cur.execute("""INSERT INTO scrm_deal
                    (inquiry_id,name,brand,product,media_company,owner,coop_types,platform_accounts,kpi,
                     amount,rebate,media_amount,expect_ts,status,note,created_ts,updated_ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'won',%s,%s,%s)""",
                    (base.get("inquiry_id"), new_name, base.get("brand"), base.get("product"), base.get("media_company"),
                     base.get("owner"), base.get("coop_types"), f"{account}({platform})", base.get("kpi"),
                     base.get("amount"), base.get("rebate"), base.get("media_amount"), base.get("expect_ts"),
                     base.get("note"), now, now))
                new_id = cur.lastrowid
                cur.execute("""INSERT INTO scrm_deal_video
                    (deal_id,platform,account,title,category,url,creator,created_ts)
                    VALUES (%s,%s,%s,'询单平台账户','商单','',%s,%s)""", (new_id, platform, account, "运营", now))
                created.append({"id": new_id, "name": new_name, "platform": platform, "account": account})
        conn.commit()
    return jsonify({"ok": True, "created": created})


@bp.post("/api/deals/<int:did>/update")
def api_deal_update(did):
    body = request.get_json(silent=True) or {}
    sets, params = [], []
    if body.get("status") in DEAL_STATUS_MAP:
        sets.append("status=%s"); params.append(body["status"])
        if body["status"] == "ended":
            sets.append("completed_ts=COALESCE(completed_ts,%s)"); params.append(int(time.time()))
    if "note" in body:
        sets.append("note=%s"); params.append(str(body.get("note") or "")[:500])
    for key, limit in (("name", 255), ("brand", 128), ("product", 255), ("media_company", 128),
                       ("owner", 64), ("coop_types", 255), ("platform_accounts", 500), ("kpi", 255)):
        if key in body:
            sets.append(f"{key}=%s"); params.append(str(body.get(key) or "")[:limit])
    for key, limit in (("order_no", 64), ("category", 64)):
        if key in body:
            sets.append(f"{key}=%s"); params.append(str(body.get(key) or "")[:limit])
    if "rebate" in body:
        sets.append("rebate=%s"); params.append(body.get("rebate") or None)
    if "media_amount" in body:
        try:
            media_amount = Decimal(str(body.get("media_amount"))) if body.get("media_amount") not in (None, "") else None
            if media_amount is not None and media_amount < 0:
                raise ValueError
        except (TypeError, ValueError, ArithmeticError):
            return jsonify({"error": "媒介金额必须为有效数字"}), 400
        sets.append("media_amount=%s"); params.append(media_amount)
    if "expect_ts" in body:
        try:
            expect_ts = int(body.get("expect_ts")) if body.get("expect_ts") not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "预计发布时间无效"}), 400
        sets.append("expect_ts=%s"); params.append(expect_ts)
    for key in ("kpi_views", "kpi_interactions"):
        if key in body:
            sets.append(f"{key}=%s"); params.append(body.get(key) or None)
    if "amount" in body:
        try:
            amount = Decimal(str(body.get("amount"))) if body.get("amount") not in (None, "") else None
            if amount is not None and amount < 0:
                raise ValueError
        except (TypeError, ValueError, ArithmeticError):
            return jsonify({"error": "商单金额必须为有效数字"}), 400
        sets.append("amount=%s"); params.append(amount)
    if "kpi_platform" in body:
        kpi_platform = str(body.get("kpi_platform") or "").strip()[:32] or None
        if kpi_platform and kpi_platform not in KPI_PLATFORMS:
            return jsonify({"error": "请选择有效KPI平台"}), 400
        sets.append("kpi_platform=%s"); params.append(kpi_platform)
    if "amount" in body or "kpi_platform" in body:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT amount, kpi_platform FROM scrm_deal WHERE id=%s", (did,))
                current = cur.fetchone()
                if not current:
                    return jsonify({"error": "商单不存在"}), 404
                calc_amount = amount if "amount" in body else current.get("amount")
                calc_platform = kpi_platform if "kpi_platform" in body else current.get("kpi_platform")
                cur.execute("SELECT cpme_ratio, cpe_ratio, original_base_value FROM scrm_kpi_config WHERE platform=%s", (calc_platform,))
                cfg = cur.fetchone() if calc_platform else None
                cur.execute("SELECT COUNT(*) c FROM scrm_deal_video WHERE deal_id=%s AND category='商单'", (did,))
                has_commercial_video = bool(cur.fetchone()["c"])
                if cfg and not has_commercial_video:
                    views = interactions = int(cfg["original_base_value"] or 0)
                    sets.extend(["kpi_views=%s", "kpi_interactions=%s"])
                    params.extend([views, interactions])
                elif cfg and calc_amount not in (None, ""):
                    views, interactions = _calc_kpi(calc_amount, cfg["cpme_ratio"], cfg["cpe_ratio"])
                    sets.extend(["kpi_views=%s", "kpi_interactions=%s"])
                    params.extend([views, interactions])
    if not sets:
        return jsonify({"error": "无可更新字段"}), 400
    sets.append("updated_ts=%s"); params.append(int(time.time())); params.append(did)
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute(f"UPDATE scrm_deal SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()
    if not n:
        return jsonify({"error": "商单不存在"}), 404
    return jsonify({"ok": True})


@bp.post("/api/deals/<int:did>/video")
def api_deal_video_add(did):
    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "视频链接必填"}), 400
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "视频链接需以 http:// 或 https:// 开头"}), 400
    now = int(time.time())
    synced = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, amount FROM scrm_deal WHERE id=%s", (did,))
            deal_row = cur.fetchone()
            if not deal_row:
                return jsonify({"error": "商单不存在"}), 404
            try:
                video_amount = Decimal(str(body.get("amount"))) if body.get("amount") not in (None, "") else deal_row.get("amount")
                if video_amount is not None and video_amount < 0:
                    raise ValueError
            except (TypeError, ValueError, ArithmeticError):
                return jsonify({"error": "视频金额必须为有效数字"}), 400
            publish_ts = _to_epoch(body.get("publish_ts"))
            cur.execute("SELECT id FROM scrm_deal_video WHERE deal_id=%s AND url=%s LIMIT 1",
                        (did, url[:500]))
            if cur.fetchone():
                return jsonify({"error": "该视频链接已添加,请勿重复"}), 400
            cur.execute("""
                INSERT INTO scrm_deal_video (deal_id, platform, account, title, url,
                                             category, amount, publish_ts, creator, created_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (did, str(body.get("platform") or "")[:32],
                  str(body.get("account") or "")[:128], str(body.get("title") or "")[:255],
                  url[:500], "商单", video_amount, publish_ts,
                  body.get("creator", "运营"), now))
            video_id = cur.lastrowid
            cur.execute("""SELECT D.amount,D.kpi_platform,C.cpme_ratio,C.cpe_ratio,C.original_base_value,
                                  (SELECT COUNT(*) FROM scrm_deal_video V WHERE V.deal_id=D.id AND V.category='商单') commercial_count
                           FROM scrm_deal D LEFT JOIN scrm_kpi_config C ON C.platform=D.kpi_platform
                           WHERE D.id=%s""", (did,))
            kpi = cur.fetchone()
            if kpi and kpi["kpi_platform"]:
                if not kpi["commercial_count"]:
                    views = interactions = int(kpi["original_base_value"] or 0)
                elif kpi["amount"] not in (None, "") and kpi["cpme_ratio"] and kpi["cpe_ratio"]:
                    views, interactions = _calc_kpi(kpi["amount"], kpi["cpme_ratio"], kpi["cpe_ratio"])
                else:
                    views = interactions = None
                cur.execute("UPDATE scrm_deal SET kpi_views=%s,kpi_interactions=%s WHERE id=%s", (views, interactions, did))
            cur.execute("UPDATE scrm_deal SET status=IF(status IN ('ended','kpi_done','canceled'),status,'published'), updated_ts=%s WHERE id=%s", (now, did))
            # 商单展示链接同时回写内容库对应平台的视频原链接和分类。
            table = next((t for t in TABLES if MAP[t]["platform"] == str(body.get("platform") or "")), None)
            if table:
                m = MAP[table]
                cur.execute(
                    f"SELECT id, CAST({m['ctitle']} AS CHAR) AS matched_title FROM `{m['ctable']}` WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s LIMIT 1",
                    (url[:500], url[:500]))
                matched = cur.fetchone()
                if matched:
                    if not str(body.get("title") or "").strip() and matched.get("matched_title"):
                        cur.execute("UPDATE scrm_deal_video SET title=%s WHERE id=%s", (str(matched["matched_title"])[:255], video_id))
                    synced = cur.execute(
                        f"UPDATE `{m['ctable']}` SET `original_url`=%s, `category`='商单' WHERE id=%s",
                        (url[:500], matched["id"]))
        conn.commit()
    return jsonify({"ok": True, "synced": bool(synced)})


@bp.post("/api/deals/video/update")
def api_deal_video_update():
    body = request.get_json(silent=True) or {}
    vid = None
    try:
        raw_vid = body.get("id") or body.get("video_id") or body.get("videoId")
        if raw_vid not in (None, "", "null", "undefined"):
            vid = int(raw_vid)
    except (TypeError, ValueError):
        vid = None
    url = str(body.get("url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "视频链接需以 http:// 或 https:// 开头"}), 400
    try:
        video_amount = Decimal(str(body.get("amount"))) if body.get("amount") not in (None, "") else None
        if video_amount is not None and video_amount < 0:
            raise ValueError
    except (TypeError, ValueError, ArithmeticError):
        return jsonify({"error": "视频金额必须为有效数字"}), 400
    publish_ts = _to_epoch(body.get("publish_ts"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 兼容旧页面或异常数据中缺失 id 的视频行，使用商单和原链接定位。
            if vid is None:
                try:
                    deal_id = int(body.get("deal_id"))
                except (TypeError, ValueError):
                    deal_id = None
                old_url = str(body.get("old_url") or "").strip()
                if deal_id and old_url:
                    cur.execute("SELECT id, deal_id FROM scrm_deal_video WHERE deal_id=%s AND url=%s LIMIT 1", (deal_id, old_url[:500]))
                    inferred = cur.fetchone()
                    if inferred:
                        vid = inferred["id"]
                # 兼容旧版页面未提交 deal_id/old_url 的情况：从同平台同账户的空链接占位行定位。
                if vid is None and not deal_id:
                    cur.execute("""SELECT id, deal_id FROM scrm_deal_video
                                  WHERE platform=%s AND account=%s AND (url='' OR url IS NULL)
                                  ORDER BY id DESC LIMIT 1""",
                                (str(body.get("platform") or "")[:32], str(body.get("account") or "")[:128]))
                    inferred = cur.fetchone()
                    if inferred:
                        vid = inferred["id"]
                        deal_id = inferred["deal_id"]
                # 页面完全未带旧记录信息时，按询单自动拆分出的平台账户反查所属商单。
                if vid is None and not deal_id and str(body.get("account") or "").strip():
                    account = str(body.get("account") or "").strip()[:128]
                    cur.execute("""SELECT id FROM scrm_deal
                                  WHERE platform_accounts LIKE %s
                                  ORDER BY updated_ts DESC, id DESC LIMIT 1""", (f"%{account}%",))
                    inferred_deal = cur.fetchone()
                    if inferred_deal:
                        deal_id = inferred_deal["id"]
            if vid is None:
                return jsonify({"error": "视频记录无效，请刷新页面后重试"}), 400
            cur.execute("SELECT deal_id FROM scrm_deal_video WHERE id=%s", (vid,))
            row = cur.fetchone()
            if not row:
                # 页面打开后占位行可能被同步任务清理，旧弹窗仍带着该编号。
                # 此时按商单重新创建一条视频记录，保证用户填写的链接不会丢失。
                try:
                    stale_deal_id = int(body.get("deal_id"))
                except (TypeError, ValueError):
                    stale_deal_id = None
                if not stale_deal_id:
                    return jsonify({"error": "视频记录不存在，请刷新页面后重试"}), 404
                cur.execute("SELECT id FROM scrm_deal WHERE id=%s", (stale_deal_id,))
                if not cur.fetchone():
                    return jsonify({"error": "商单不存在"}), 404
                cur.execute("SELECT id FROM scrm_deal_video WHERE deal_id=%s AND url=%s LIMIT 1", (stale_deal_id, url[:500]))
                if cur.fetchone():
                    return jsonify({"error": "该视频链接已存在"}), 400
                cur.execute("""INSERT INTO scrm_deal_video
                             (deal_id, platform, account, title, url, category, amount, publish_ts, creator, created_ts)
                             VALUES (%s,%s,%s,%s,%s,'商单',%s,%s,'运营',%s)""",
                            (stale_deal_id, str(body.get("platform") or "")[:32],
                             str(body.get("account") or "")[:128], str(body.get("title") or "")[:255],
                             url[:500], video_amount, publish_ts, int(time.time())))
                cur.execute("UPDATE scrm_deal SET status=IF(status IN ('ended','kpi_done','canceled'),status,'published'), updated_ts=%s WHERE id=%s",
                            (int(time.time()), stale_deal_id))
                conn.commit()
                return jsonify({"ok": True, "created": True})
            cur.execute("SELECT id FROM scrm_deal_video WHERE deal_id=%s AND url=%s AND id<>%s LIMIT 1",
                        (row["deal_id"], url[:500], vid))
            if cur.fetchone():
                return jsonify({"error": "该视频链接已存在"}), 400
            cur.execute("""UPDATE scrm_deal_video
                         SET platform=%s, account=%s, url=%s, amount=%s, publish_ts=%s, category='商单'
                         WHERE id=%s""",
                        (str(body.get("platform") or "")[:32], str(body.get("account") or "")[:128], url[:500], video_amount, publish_ts, vid))
            # 保存编辑时同步内容库信息，补齐标题、发布时间和商单分类。
            table = next((t for t in TABLES if MAP[t]["platform"] == str(body.get("platform") or "")), None)
            if table:
                m = MAP[table]
                cur.execute(
                    f"SELECT id, CAST({m['ctitle']} AS CHAR) AS matched_title, CAST({m['cpubtime']} AS CHAR) AS actual_publish_ts "
                    f"FROM `{m['ctable']}` WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s LIMIT 1",
                    (url[:500], url[:500]))
                matched = cur.fetchone()
                if matched:
                    if not body.get("title") and matched.get("matched_title"):
                        cur.execute("UPDATE scrm_deal_video SET title=%s WHERE id=%s", (str(matched["matched_title"])[:255], vid))
                    if publish_ts is None and matched.get("actual_publish_ts"):
                        cur.execute("UPDATE scrm_deal_video SET publish_ts=%s WHERE id=%s", (_to_epoch(matched.get("actual_publish_ts")), vid))
                    cur.execute(f"UPDATE `{m['ctable']}` SET `original_url`=%s, `category`='商单' WHERE id=%s", (url[:500], matched["id"]))
            cur.execute("UPDATE scrm_deal SET updated_ts=%s WHERE id=%s", (int(time.time()), row["deal_id"]))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/deals/video/delete")
def api_deal_video_delete():
    body = request.get_json(silent=True) or {}
    try:
        vid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 id"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scrm_deal_video WHERE id=%s", (vid,))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/media")
def api_media():
    q = (request.args.get("q") or "").strip()
    where, params = "", []
    if q:
        where = " WHERE name LIKE %s OR contact LIKE %s OR wechat LIKE %s OR party_a LIKE %s"
        params = [f"%{q}%"] * 4
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT M.*, (SELECT COUNT(*) FROM scrm_deal D WHERE D.media_company=M.name) deal_count
                FROM scrm_media M{where} ORDER BY M.updated_ts DESC
            """, params)
            rows = cur.fetchall()
    return jsonify({"rows": rows, "team": TEAM})


@bp.post("/api/media/save")
def api_media_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "媒介公司名称必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.get("id"):
                cur.execute("""
                    UPDATE scrm_media SET name=%s, party_a=%s, contact=%s, wechat=%s,
                          note=%s, updated_ts=%s WHERE id=%s
                """, (name[:128], str(body.get("party_a") or "")[:255],
                      str(body.get("contact") or "")[:64], str(body.get("wechat") or "")[:128],
                      str(body.get("note") or "")[:500], now, int(body["id"])))
            else:
                cur.execute("""
                    INSERT INTO scrm_media (name, party_a, contact, wechat, source,
                                            created_ts, updated_ts)
                    VALUES (%s,%s,%s,%s,'manual',%s,%s)
                """, (name[:128], str(body.get("party_a") or "")[:255],
                      str(body.get("contact") or "")[:64], str(body.get("wechat") or "")[:128],
                      now, now))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/media/delete")
def api_media_delete():
    body = request.get_json(silent=True) or {}
    try:
        mid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 id"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scrm_media WHERE id=%s", (mid,))
        conn.commit()
    return jsonify({"ok": True})


# ============================================================
# API: UP主管理 / Skill 绑定
# ============================================================
@bp.get("/api/creators")
def api_creators():
    simple = request.args.get("simple", "")
    if simple:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT '' AS platform, `昵称` AS name FROM `UP主管理`
                    WHERE `状态` != 'retired' ORDER BY `昵称`
                """)
                rows = cur.fetchall()
        return jsonify({"rows": rows})
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(100, int(request.args.get("size", 20))))
    clauses, params = [], []
    platform = request.args.get("platform", "")
    if platform:
        clauses.append("A.`平台`=%s")
        params.append(platform)
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("""(
            C.`昵称` LIKE %s OR C.`BIT窗口` LIKE %s OR C.`账号等级` LIKE %s
            OR C.`运营人员` LIKE %s OR C.`出镜人` LIKE %s OR C.`组别` LIKE %s
            OR C.`账号绑定公司` LIKE %s OR C.`商务微信` LIKE %s OR C.`商务邮箱` LIKE %s
        )""")
        params.extend([like] * 9)
    status = (request.args.get("status") or "").strip()
    if status:
        clauses.append("C.`状态`=%s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(DISTINCT C.`id`) c
                FROM `UP主管理` C
                LEFT JOIN `平台运营账号` A ON A.`UP主ID`=C.`id`
                {where}
            """, params)
            total = cur.fetchone()["c"]
            cur.execute(f"""
                SELECT C.`id` AS id, C.`昵称` AS name, C.`BIT窗口` AS bit_window,
                       C.`账号等级` AS account_level, C.`运营人员` AS operator,
                       C.`出镜人` AS presenter, C.`组别` AS team_group,
                       C.`账号绑定公司` AS bound_company, C.`商务微信` AS business_wechat,
                       C.`商务邮箱` AS business_email, C.`状态` AS status,
                       C.`备注` AS note, C.`updated_ts` AS updated_ts,
                       COUNT(DISTINCT A.`平台`) AS platform_count,
                       COUNT(A.`昵称`) AS account_count,
                       GROUP_CONCAT(DISTINCT A.`平台` ORDER BY A.`平台` SEPARATOR '、') AS platforms,
                       (SELECT GROUP_CONCAT(Sk.name SEPARATOR '、')
                          FROM scrm_skill Sk WHERE Sk.bind_up=C.`昵称`) bound_skills
                FROM `UP主管理` C
                LEFT JOIN `平台运营账号` A ON A.`UP主ID`=C.`id`
                {where}
                GROUP BY C.`id`, C.`昵称`, C.`BIT窗口`, C.`账号等级`, C.`运营人员`,
                         C.`出镜人`, C.`组别`, C.`账号绑定公司`, C.`商务微信`,
                         C.`商务邮箱`, C.`状态`, C.`备注`, C.`updated_ts`
                ORDER BY account_count DESC, C.`id`
                LIMIT %s OFFSET %s
            """, params + [size, (page - 1) * size])
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) c FROM `UP主管理`")
            kpi_total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(DISTINCT `组别`) c FROM `UP主管理` WHERE `组别` IS NOT NULL AND `组别` != ''")
            kpi_groups = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM scrm_skill WHERE bind_up IS NOT NULL AND bind_up != ''")
            kpi_bound = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM `UP主管理` WHERE `状态`='active'")
            kpi_active = cur.fetchone()["c"]
    return jsonify({"rows": rows, "total": total, "page": page, "size": size,
                    "statuses": CREATOR_STATUS, "status_map": CREATOR_STATUS_MAP,
                    "kpi": {"total": kpi_total, "groups": kpi_groups,
                            "bound": kpi_bound, "active": kpi_active}})


@bp.post("/api/creators/sync")
def api_creators_sync():
    with get_conn() as conn:
        with conn.cursor() as cur:
            added = _sync_creators(cur)
            updated = cur.execute("""
                UPDATE `平台运营账号` A
                JOIN `UP主管理` C ON C.`id` = A.`UP主ID`
                SET A.`Bit窗口` = C.`BIT窗口`
                WHERE COALESCE(C.`BIT窗口`, '') <> ''
            """)
        conn.commit()
    return jsonify({"ok": True, "added": added, "updated_accounts": updated})


@bp.post("/api/creators/save")
def api_creator_save():
    body = request.get_json(silent=True) or {}
    try:
        cid = int(body["id"]) if body.get("id") is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "非法 id"}), 400
    if cid is None and not str(body.get("name") or "").strip():
        return jsonify({"error": "昵称必填"}), 400
    status = body.get("status")
    if status and status not in CREATOR_STATUS_MAP:
        return jsonify({"error": "非法状态"}), 400
    sets, params = [], []
    fields = {
        "name": ("`昵称`", 128),
        "bit_window": ("`BIT窗口`", 255),
        "account_level": ("`账号等级`", 64),
        "operator": ("`运营人员`", 64),
        "presenter": ("`出镜人`", 64),
        "team_group": ("`组别`", 64),
        "bound_company": ("`账号绑定公司`", 128),
        "business_wechat": ("`商务微信`", 128),
        "business_email": ("`商务邮箱`", 128),
        "note": ("`备注`", 500),
    }
    for key, (col, limit) in fields.items():
        if key in body:
            val = str(body.get(key) or "").strip()[:limit]
            if key == "name" and not val:
                return jsonify({"error": "昵称必填"}), 400
            sets.append(f"{col}=%s"); params.append(val)
    if status:
        sets.append("`状态`=%s"); params.append(status)
    if not sets:
        return jsonify({"error": "无可更新字段"}), 400
    if cid is None:
        columns = [assignment.split("=", 1)[0] for assignment in sets]
        now = int(time.time())
        columns += ["created_ts", "updated_ts"]
        values = params + [now, now]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO `UP主管理` ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))})",
                        values,
                    )
                    new_id = cur.lastrowid
                conn.commit()
        except IntegrityError as exc:
            if exc.args[0] == 1062:
                return jsonify({"error": "该昵称已存在，请直接编辑已有 UP主"}), 409
            raise
        return jsonify({"ok": True, "id": new_id}), 201
    sets.append("updated_ts=%s"); params.append(int(time.time())); params.append(cid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            propagated = 0
            old_name = None
            if "name" in body:
                cur.execute("SELECT `昵称` FROM `UP主管理` WHERE `id`=%s", (cid,))
                row = cur.fetchone()
                old_name = row["昵称"] if row else None
            n = cur.execute(f"UPDATE `UP主管理` SET {', '.join(sets)} WHERE `id`=%s", params)
            if n and old_name and old_name != str(body.get("name") or "").strip():
                cur.execute("UPDATE scrm_skill SET bind_up=%s, updated_ts=%s WHERE bind_up=%s",
                            (str(body.get("name") or "").strip()[:128], int(time.time()), old_name))
            if n and "bit_window" in body:
                propagated = _propagate_creator_bit_window(cur, cid)
        conn.commit()
    if not n:
        return jsonify({"error": "UP主不存在"}), 404
    return jsonify({"ok": True, "updated_accounts": propagated})


@bp.post("/api/creators/bind")
def api_creator_bind():
    """UP主 ↔ Skill 绑定同步:更新 scrm_skill.bind_up"""
    body = request.get_json(silent=True) or {}
    try:
        sid = int(body.get("skill_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 skill_id"}), 400
    creator = str(body.get("creator") or "").strip()[:128]
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE scrm_skill SET bind_up=%s, updated_ts=%s WHERE id=%s",
                            (creator or None, int(time.time()), sid))
        conn.commit()
    if not n:
        return jsonify({"error": "Skill 不存在"}), 404
    return jsonify({"ok": True, "bound": creator or None})


# ============================================================
# API: 人设 Skill
# ============================================================
@bp.get("/api/skills")
def api_skills():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_skill ORDER BY updated_ts DESC")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.post("/api/skills/save")
def api_skill_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Skill 名称必填"}), 400
    persona = body.get("persona")
    if persona is not None and (not isinstance(persona, dict) or not isinstance(persona.get("sections"), dict)):
        return jsonify({"error": "人设结构无效"}), 400
    persona_text = json.dumps(persona, ensure_ascii=False) if persona is not None else None
    if persona_text and len(persona_text) > 2000000:
        return jsonify({"error": "人设内容过长"}), 400
    now = int(time.time())
    fields = ("name", "platform", "bind_up", "style", "example")
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.get("id"):
                sets = [f"{f}=%s" for f in fields]
                params = [str(body.get(f) or "")[:1000] for f in fields]
                params += [now, int(body["id"])]
                cur.execute(f"UPDATE scrm_skill SET {', '.join(sets)}, updated_ts=%s "
                            f"WHERE id=%s", params)
            else:
                cur.execute(
                    "INSERT INTO scrm_skill (name, platform, bind_up, style, example, status, "
                    "created_ts, updated_ts) VALUES (%s,%s,%s,%s,%s,'ready',%s,%s)",
                    (name[:64], str(body.get("platform") or "")[:32],
                     str(body.get("bind_up") or "")[:128],
                     str(body.get("style") or "")[:1000],
                     str(body.get("example") or "")[:1000], now, now))
            saved_id = int(body["id"]) if body.get("id") else cur.lastrowid
            if persona_text is not None:
                cur.execute("UPDATE scrm_skill SET persona=%s WHERE id=%s", (persona_text, saved_id))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/skills/test")
def api_skill_test():
    """按 Skill 人设风格测试生成回复"""
    body = request.get_json(silent=True) or {}
    content = str(body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "请输入要测试的评论"}), 400
    style = str(body.get("style") or "")
    if isinstance(body.get("persona"), dict):
        style += "\n" + json.dumps(body["persona"].get("sections", {}), ensure_ascii=False)
    bind = str(body.get("bind_up") or "UP主")
    sys_prompt = (
        f"你正在扮演「{bind}」的账号进行评论回复。人设风格:{style}。"
        "回复要求:10-50 字,自然口语化,符合人设,只输出回复正文,不要解释。"
    )
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": f"评论:{content[:500]}"}],
        "max_tokens": 200, "temperature": 0.9,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"ok": True, "reply": text[:300]})
    except Exception as e:
        return jsonify({"ok": False, "error": f"生成失败:{str(e)[:120]}"}), 502


@bp.post("/api/skills/delete")
def api_skill_delete():
    body = request.get_json(silent=True) or {}
    try:
        sid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 id"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scrm_skill WHERE id=%s", (sid,))
        conn.commit()
    return jsonify({"ok": True})


# ============================================================
# API: 平台运营账号
# ============================================================
ACCOUNT_STATUS = [
    ("active", "运营中", "#16a34a"),
    ("paused", "已暂停", "#d97706"),
    ("retired", "已停用", "#9ca3af"),
]
ACCOUNT_STATUS_MAP = {s[0]: s[1] for s in ACCOUNT_STATUS}
# 运营账号平台名 → 内容库 platform 名
ACCOUNT_PLATFORM_MAP = {
    "B站": "B站", "小红书": "小红书", "抖音": "抖音", "快手": "快手",
    "微博": "微博", "知乎": "知乎", "视频号": "视频号", "得物": "得物",
    "京东逛逛": "京东逛逛", "拼多多": "拼多多", "搜狐视频": "搜狐视频",
    "贴吧": "贴吧", "Reddit": "Reddit",
}


def _account_frag():
    return ("SELECT A.`平台` AS platform, A.`昵称` AS nickname, A.`UP主ID` AS creator_id, "
            "C.`昵称` AS creator_name, A.`ID` AS account_id, A.`粉丝` AS fans, "
            "A.`Bit窗口` AS bit_window, A.`状态` AS status, A.`备注` AS note, "
            "A.`cookie` AS cookie, "
            "CASE WHEN TRIM(COALESCE(A.`cookie`, ''))='' THEN 'missing' "
            "WHEN A.cookie_fingerprint=SHA2(A.`cookie`, 256) THEN A.cookie_status "
            "ELSE 'unknown' END AS cookie_status, "
            "CASE WHEN A.cookie_fingerprint=SHA2(COALESCE(A.`cookie`, ''), 256) "
            "THEN A.cookie_checked_at ELSE NULL END AS cookie_checked_at, "
            "CASE WHEN A.cookie_fingerprint=SHA2(COALESCE(A.`cookie`, ''), 256) "
            "THEN A.cookie_check_message ELSE '' END AS cookie_check_message "
            "FROM `平台运营账号` A LEFT JOIN `UP主管理` C ON C.`id`=A.`UP主ID`")


def _account_stats():
    """账号→内容库匹配:按 (内容库 platform, upname) 统计视频/评论/负面/意向
    视频侧 SQL 聚合,评论侧全量聚合一次内存匹配(与 api_videos 同思路,避免 UNION 歧义)"""
    return f"""
        SELECT A.platform, A.nickname, A.creator_id, A.creator_name, A.account_id, A.fans,
               A.bit_window, A.status, A.note, A.cookie_status, A.cookie_checked_at, A.cookie_check_message,
               COUNT(DISTINCT V.content_id) video_count,
               MAX(V.last_update_ts) latest_update_ts,
               SUM(CASE WHEN V.last_update_ts >= UNIX_TIMESTAMP(DATE_FORMAT(CURRENT_DATE, '%%Y-%%m-01')) THEN 1 ELSE 0 END) month_video_count
        FROM ({_account_frag()}) A
        LEFT JOIN {_content_union()} ON V.platform = A.platform AND V.upname = A.nickname
        GROUP BY A.platform, A.nickname, A.creator_id, A.creator_name, A.account_id,
                 A.fans, A.bit_window, A.status, A.note, A.cookie_status, A.cookie_checked_at, A.cookie_check_message
    """


@bp.get("/api/accounts")
def api_accounts():
    q = (request.args.get("q") or "").strip()
    platform = (request.args.get("platform") or "").strip()
    status = (request.args.get("status") or "").strip()
    cookie_status = (request.args.get("cookie_status") or "").strip()
    if cookie_status and cookie_status not in ("valid", "expired", "missing", "unknown"):
        return jsonify({"error": "非法 Cookie 状态"}), 400
    bit_window = (request.args.get("bit_window") or "").strip()
    if bit_window and bit_window != "present":
        return jsonify({"error": "非法比特窗口筛选"}), 400
    page = max(1, int(request.args.get("page", 1) or 1))
    size = min(100, max(1, int(request.args.get("size", 20) or 20)))
    where, params = [], []
    if q:
        where.append("(A.nickname LIKE %s OR A.account_id LIKE %s OR A.creator_name LIKE %s)")
        params.extend([f"%{q}%"] * 3)
    if platform:
        where.append("A.platform = %s")
        params.append(platform)
    if status:
        where.append("A.status = %s")
        params.append(status)
    if cookie_status:
        where.append("A.cookie_status = %s")
        params.append(cookie_status)
    if bit_window == "present":
        where.append("COALESCE(TRIM(A.bit_window), '') <> ''")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * size
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM ({_account_frag()}) A{where_sql}", params)
            total = cur.fetchone()["c"]
            # 先分页取账号基础资料，避免为整张账号表计算视频统计。
            cur.execute(f"""
                SELECT A.* FROM ({_account_frag()}) A
                {where_sql}
                ORDER BY A.fans DESC
                LIMIT %s OFFSET %s
            """, params + [size, offset])
            rows = cur.fetchall()
            # 只聚合当前页账号对应的视频；评论指标不在账号表统计。
            for r in rows:
                r.update(video_count=0, month_video_count=0)
            if rows:
                pair_sql = " OR ".join(["(V.platform=%s AND V.upname=%s)"] * len(rows))
                pair_params = [v for r in rows for v in (r["platform"], r["nickname"])]
                cur.execute(f"""
                    SELECT V.platform, V.upname, V.content_id, V.last_update_ts
                    FROM {_content_union()}
                    WHERE {pair_sql}
                    GROUP BY V.platform, V.upname, V.content_id, V.last_update_ts
                """, pair_params)
                agg = {}
                month_start = int(time.time()) - (int(time.strftime("%d")) - 1) * 86400
                for v in cur.fetchall():
                    key = (v["platform"], v["upname"])
                    a = agg.setdefault(key, {"video_ids": set(), "month": 0})
                    a["video_ids"].add(str(v["content_id"]))
                    if v.get("last_update_ts") and int(v["last_update_ts"]) >= month_start:
                        a["month"] += 1
                for r in rows:
                    a = agg.get((r["platform"], r["nickname"]))
                    if a:
                        r.update(video_count=len(a["video_ids"]), month_video_count=a["month"])
            # 平台列表(用于筛选下拉)
            cur.execute("SELECT DISTINCT `平台` FROM `平台运营账号` ORDER BY `平台`")
            platforms = [r["平台"] for r in cur.fetchall()]
            cur.execute("SELECT `id`, `昵称` AS name FROM `UP主管理` ORDER BY `昵称`")
            creators = cur.fetchall()
    return jsonify({"rows": rows, "total": total, "page": page, "size": size,
                    "platforms": platforms, "team": TEAM,
                    "creators": creators,
                    "status_map": ACCOUNT_STATUS_MAP})


@bp.post("/api/accounts/check-cookie")
def api_account_check_cookie():
    from account_cookie_health import check_account
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body.get("platform") or not body.get("nickname"):
        return jsonify({"error": "平台与昵称必填"}), 400
    result = check_account(str(body["platform"]), str(body["nickname"]))
    if result is None:
        return jsonify({"error": "账号不存在"}), 404
    return jsonify(result)


@bp.post("/api/accounts/update-cookie")
def api_account_update_cookie():
    from account_cookie_sync import fetch_account_cookie
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求格式错误"}), 400
    platform = str(body.get("platform") or "").strip()
    nickname = str(body.get("nickname") or "").strip()
    if not platform or not nickname:
        return jsonify({"error": "平台与昵称必填"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `Bit窗口` AS bit_window FROM `平台运营账号` "
                        "WHERE `平台`=%s AND `昵称`=%s", (platform, nickname))
            account = cur.fetchone()
    if not account:
        return jsonify({"error": "账号不存在"}), 404
    try:
        cookie, ua = fetch_account_cookie(platform, account["bit_window"])
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "读取窗口失败或超时，请检查比特浏览器后重试"}), 502
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `Bit窗口` AS bit_window, `凭据` AS credentials "
                        "FROM `平台运营账号` WHERE `平台`=%s AND `昵称`=%s FOR UPDATE",
                        (platform, nickname))
            current = cur.fetchone()
            if not current or current["bit_window"] != account["bit_window"]:
                return jsonify({"error": "账号或绑定窗口已改变，请刷新后重试"}), 409
            credentials = current.get("credentials")
            try:
                credentials = json.loads(credentials or "{}")
            except (ValueError, TypeError):
                credentials = None
            if isinstance(credentials, dict) and ua:
                credentials["user_agent"] = ua
                cur.execute("UPDATE `平台运营账号` SET `cookie`=%s, `凭据`=%s "
                            "WHERE `平台`=%s AND `昵称`=%s",
                            (cookie, json.dumps(credentials, ensure_ascii=False), platform, nickname))
            else:
                cur.execute("UPDATE `平台运营账号` SET `cookie`=%s WHERE `平台`=%s AND `昵称`=%s",
                            (cookie, platform, nickname))
        conn.commit()
    from account_cookie_health import check_account
    result = check_account(platform, nickname)
    return jsonify({"ok": True, **(result or {})})


@bp.post("/api/accounts/save")
def api_account_save():
    body = request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "").strip()
    nickname = str(body.get("nickname") or "").strip()
    if not platform or not nickname:
        return jsonify({"error": "平台与昵称必填"}), 400
    vals = dict(
        creator_id=body.get("creator_id"),
        account_id=str(body.get("account_id") or "").strip()[:255],
        fans=int(body.get("fans") or 0),
        bit_window=str(body.get("bit_window") or "").strip()[:255],
        status=str(body.get("status") or "active").strip()[:16],
        note=str(body.get("note") or "").strip()[:500],
    )
    if vals["status"] not in ACCOUNT_STATUS_MAP:
        return jsonify({"error": "非法状态"}), 400
    # 粉丝量解析:支持 1.2w / 3万 / 5000 等写法
    raw_fans = body.get("fans")
    if isinstance(raw_fans, str):
        mf = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([wW万]?)", raw_fans.strip())
        if mf:
            mult = {"": 1, "w": 10000, "W": 10000, "万": 10000}[mf[2]]
            vals["fans"] = int(float(mf[1]) * mult)
    old_platform = str(body.get("old_platform") or "").strip()
    old_nickname = str(body.get("old_nickname") or "").strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            creator_id = None
            try:
                creator_id = int(vals["creator_id"]) if vals["creator_id"] not in (None, "") else None
            except (TypeError, ValueError):
                return jsonify({"error": "非法 UP主"}), 400
            if creator_id:
                cur.execute("SELECT COUNT(*) c FROM `UP主管理` WHERE `id`=%s", (creator_id,))
                if not cur.fetchone()["c"]:
                    return jsonify({"error": "UP主不存在"}), 400
            if old_platform and old_nickname:
                # 编辑:按原 平台+昵称 定位
                n = cur.execute("""
                    UPDATE `平台运营账号` SET `平台`=%s, `昵称`=%s, `UP主ID`=%s,
                        `ID`=%s, `粉丝`=%s, `Bit窗口`=%s, `状态`=%s, `备注`=%s
                    WHERE `平台`=%s AND `昵称`=%s
                """, (platform, nickname, creator_id, vals["account_id"], vals["fans"],
                      vals["bit_window"], vals["status"], vals["note"],
                      old_platform, old_nickname))
                if not n:
                    return jsonify({"error": "账号不存在"}), 404
            else:
                cur.execute("SELECT COUNT(*) c FROM `平台运营账号` WHERE `平台`=%s AND `昵称`=%s",
                            (platform, nickname))
                if cur.fetchone()["c"]:
                    return jsonify({"error": "该平台下已存在同名账号"}), 400
                cur.execute("""
                    INSERT INTO `平台运营账号`
                        (`平台`,`昵称`,`UP主ID`,`ID`,`粉丝`,`Bit窗口`,`状态`,`备注`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (platform, nickname, creator_id, vals["account_id"], vals["fans"],
                      vals["bit_window"], vals["status"], vals["note"]))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/accounts/delete")
def api_account_delete():
    body = request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "").strip()
    nickname = str(body.get("nickname") or "").strip()
    if not platform or not nickname:
        return jsonify({"error": "平台与昵称必填"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM `平台运营账号` WHERE `平台`=%s AND `昵称`=%s",
                        (platform, nickname))
        conn.commit()
    return jsonify({"ok": True})


from persona_skill import register_persona_routes
register_persona_routes(bp, {t: MAP[t] for t in TABLES})
