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
import hashlib
import secrets
import html
import ipaddress
import socket
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from functools import wraps
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for, g

import requests
from pymysql.err import IntegrityError

from db import get_conn
from config import (
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_INQUIRY_TABLE,
    FEISHU_CATEGORY_APP_TOKEN, FEISHU_CATEGORY_TABLE,
)
import config as app_config
from analyzer import mark_analyzed, ADVANCED_NEGATIVE_TYPES
from comment_paging import thread_roots_sql
from reply_context import resolve_comment_context
from reply_service_client import evaluate_comment, generate_reply
from scrm_components.feishu import FeishuBitableClient, cell_money, cell_text, cell_timestamp
from scrm_components.comment_queries import (
    comment_filters as _apply_comment_filters,
    comment_fragment as _comment_frag,
    comments_union as _union,
    content_fragment as _content_frag,
    contents_union as _content_union,
)
from scrm_components.comment_service import CommentReadService, THREAD_PAGE_SIZE, with_platform as _with_platform
from scrm_components.platforms import (
    BRAND_STATUS as PLATFORM_BRAND_STATUS,
    BRAND_STATUS_MAP as PLATFORM_BRAND_STATUS_MAP,
    COMMENT_TABLES_ENABLED, KPI_PLATFORMS as PLATFORM_KPI_PLATFORMS,
    LEAD_STATUS as PLATFORM_LEAD_STATUS,
    LEAD_STATUS_MAP as PLATFORM_LEAD_STATUS_MAP,
    PLATFORM_MAP, PLATFORM_OF, TEAM as PLATFORM_TEAM,
)

logger = logging.getLogger("scrm")
bp = Blueprint("scrm", __name__, url_prefix="/scrm")

# ---------- 平台 / 表映射 ----------
# 映射定义、状态标签和人员名单集中在 scrm_components.platforms，避免路由层重复维护。
CREATOR_TABLE = "`UP主管理`"

# 兼容既有内部引用；映射的唯一运行时来源已迁至 scrm_components.platforms。
MAP = PLATFORM_MAP
TABLES = COMMENT_TABLES_ENABLED
LEAD_STATUS = PLATFORM_LEAD_STATUS
LEAD_STATUS_MAP = PLATFORM_LEAD_STATUS_MAP
BRAND_STATUS = PLATFORM_BRAND_STATUS
BRAND_STATUS_MAP = PLATFORM_BRAND_STATUS_MAP
TEAM = PLATFORM_TEAM
KPI_PLATFORMS = PLATFORM_KPI_PLATFORMS

# 权限资源目录：前端按此目录展示，后端只接受目录内的权限键，避免任意资源写入。
PERMISSION_CATALOG = {
    "page": [
        ("overview", "监控总览"), ("comments", "评论列表"), ("comment_detail", "评论详情"),
        ("videos", "视频列表"), ("alerts", "告警中心"), ("workbench", "智能分析工作台"),
        ("reply", "回复编辑页"), ("auto_tasks", "自动操作任务"), ("skills", "人设 Skill"),
        ("kb", "知识库"), ("kb_table", "知识库表格"), ("kb_ingest", "知识导入"),
        ("creators", "UP主管理"), ("accounts", "平台运营账号"), ("account_management", "账号总览"),
        ("leads_overview", "线索总览"), ("leads", "线索列表"), ("lead_detail", "线索详情"),
        ("lead_workflow", "线索工作流"), ("lead_alerts", "线索异常告警"),
        ("brands", "品牌聚合"), ("brand_detail", "品牌详情"), ("brand_funnel", "品牌转化漏斗"),
        ("inquiries", "商务询单"), ("deals", "商单管理"), ("deal_detail", "商单详情"),
        ("media", "合作媒介"), ("business_wechat", "商务微信"),
        ("settings", "设置管理"), ("categories", "品类管理"), ("brand_management", "品牌管理"),
        ("permission-management", "权限管理"),
    ],
    "field": [
        ("comments.content", "评论 · 内容"), ("comments.nickname", "评论 · 评论人"),
        ("comments.sentiment", "评论 · 情绪/意图"), ("comments.reply", "评论 · AI 回复建议"),
        ("comments.time", "评论 · 发布时间"), ("comments.status", "评论 · 处理状态"),
        ("videos.title", "视频 · 标题"), ("videos.url", "视频 · 链接"), ("videos.metrics", "视频 · 播放及互动数据"),
        ("videos.category", "视频 · 分类"), ("videos.product", "视频 · 产品名称"),
        ("leads.source", "线索 · 来源评论"), ("leads.intent", "线索 · 意向说明"),
        ("leads.owner", "线索 · 负责人"), ("leads.status", "线索 · 跟进状态"),
        ("leads.follow", "线索 · 跟进记录"), ("leads.due", "线索 · 下次跟进时间"),
        ("brands.name", "品牌 · 名称"), ("brands.contact", "品牌 · 联系人"),
        ("brands.phone", "品牌 · 联系电话"), ("brands.wechat", "品牌 · 微信"),
        ("brands.owner", "品牌 · 商务负责人"), ("brands.background", "品牌 · 背景与备注"),
        ("inquiries.number", "询单 · 编号"), ("inquiries.name", "询单 · 名称"),
        ("inquiries.brand", "询单 · 品牌/产品/品类"), ("inquiries.sales", "询单 · 商务组员"),
        ("inquiries.contact", "询单 · 联系人/微信"), ("inquiries.amount", "询单 · 商单金额/返点/媒介金额"),
        ("inquiries.kpi", "询单 · KPI"), ("inquiries.accounts", "询单 · 平台账号"),
        ("inquiries.follow", "询单 · 跟进详情"), ("inquiries.status", "询单 · 合作状态"),
        ("deals.order_no", "商单 · 订单编号"), ("deals.name", "商单 · 名称"),
        ("deals.brand", "商单 · 品牌/产品/型号/品类"), ("deals.owner", "商单 · 负责人"),
        ("deals.amount", "商单 · 金额/返点/媒介金额"), ("deals.kpi", "商单 · KPI 目标"),
        ("deals.date", "商单 · 预计/实际发布时间"), ("deals.accounts", "商单 · 平台账号"),
        ("deals.videos", "商单 · 发布视频及金额"), ("deals.status", "商单 · 状态与备注"),
        ("media.name", "媒介 · 公司名称"), ("media.contact", "媒介 · 联系人"),
        ("media.phone", "媒介 · 联系电话"), ("media.wechat", "媒介 · 微信"),
        ("media.party_a", "媒介 · 甲方主体"), ("media.owner", "媒介 · 负责人"),
        ("media.cooperation", "媒介 · 合作频率/次数/状态"), ("media.background", "媒介 · 背景与备注"),
        ("creators.nickname", "UP主 · 昵称"), ("creators.window", "UP主 · Bit 浏览器窗口"),
        ("creators.level", "UP主 · 账号等级"), ("creators.operator", "UP主 · 运营人员/组别/出镜人"),
        ("creators.contact", "UP主 · 商务微信/邮箱"), ("creators.company", "UP主 · 绑定公司"),
        ("creators.status", "UP主 · 状态/备注"), ("accounts.platform", "运营账号 · 平台/昵称/账号 ID"),
        ("accounts.cookie", "运营账号 · Cookie/用户代理"), ("accounts.window", "运营账号 · Bit 浏览器窗口"),
        ("accounts.fans", "运营账号 · 粉丝及数据"), ("accounts.status", "运营账号 · 状态/备注"),
        ("skills.persona", "Skill · 人设参数"), ("skills.binding", "Skill · 绑定 UP主"),
        ("skills.version", "Skill · 发布版本"), ("settings.wechat", "设置 · 商务微信映射"),
        ("settings.category", "设置 · 品类"), ("settings.brand", "设置 · 品牌别名"),
        ("settings.kpi", "设置 · 平台 KPI 配置"), ("users.name", "系统账户 · 账户名称"),
        ("kb.document", "知识库 · 文档名称与内容"), ("kb.source", "知识库 · 来源与同步信息"),
        ("auto_tasks.task", "自动任务 · 平台、内容与执行结果"),
        ("users.password", "系统账户 · 密码"), ("users.role", "系统账户 · 角色"),
        ("roles.name", "角色 · 角色名称及说明"), ("roles.permission", "角色 · 权限配置"),
    ],
    "action": [
        ("comments.view", "评论 · 查看详情"), ("comments.analyze", "评论 · 发起分析"),
        ("comments.reply", "评论 · 编辑回复"), ("comments.review", "评论 · 审核/标注"),
        ("comments.export", "评论 · 导出"), ("videos.view", "视频 · 查看"),
        ("videos.review_product", "视频 · 审核产品名称"), ("videos.identify_product", "视频 · 自动识别产品"),
        ("alerts.handle", "告警 · 处理"), ("workbench.calibrate", "分析 · 人工校准"),
        ("leads.create", "线索 · 新增"), ("leads.edit", "线索 · 编辑"), ("leads.follow", "线索 · 添加跟进"),
        ("leads.export", "线索 · 导出"), ("brands.edit", "品牌 · 编辑"), ("brands.follow", "品牌 · 添加跟进"),
        ("inquiries.create", "询单 · 新增"), ("inquiries.edit", "询单 · 编辑"),
        ("inquiries.delete", "询单 · 删除"), ("inquiries.sync", "询单 · 飞书同步"),
        ("inquiries.send_wechat", "询单 · 发送微信"), ("deals.create", "商单 · 新增/拆分"),
        ("deals.edit", "商单 · 编辑"), ("deals.delete", "商单 · 删除"),
        ("deals.video", "商单 · 管理发布视频"), ("deals.sync_product", "商单 · 同步产品参数"),
        ("media.create", "媒介 · 新增"), ("media.edit", "媒介 · 编辑"), ("media.delete", "媒介 · 删除"),
        ("creators.sync", "UP主 · 同步"), ("creators.create", "UP主 · 新增"),
        ("creators.edit", "UP主 · 编辑"), ("creators.bind_skill", "UP主 · 绑定 Skill"),
        ("accounts.create", "运营账号 · 新增"), ("accounts.edit", "运营账号 · 编辑"),
        ("accounts.delete", "运营账号 · 删除"), ("accounts.check_cookie", "运营账号 · 检查 Cookie"),
        ("accounts.update_cookie", "运营账号 · 更新 Cookie"), ("skills.create", "Skill · 新增"),
        ("skills.edit", "Skill · 编辑"), ("skills.publish", "Skill · 发布"), ("skills.test", "Skill · 测试"),
        ("skills.delete", "Skill · 删除"), ("settings.edit_wechat", "设置 · 编辑商务微信"),
        ("kb.import", "知识库 · 导入资料"), ("kb.manage", "知识库 · 编辑/删除资料"),
        ("auto_tasks.retry", "自动任务 · 重试执行"),
        ("settings.edit_category", "设置 · 编辑品类"), ("settings.sync_category", "设置 · 同步品类"),
        ("settings.edit_brand", "设置 · 编辑品牌"), ("settings.edit_kpi", "设置 · 编辑 KPI"),
        ("users.manage", "系统账户 · 新增/编辑/启停"), ("roles.manage", "角色 · 新增/编辑"),
        ("roles.configure", "角色 · 配置权限"),
    ],
}


def _normalize_password(password):
    """兼容中文输入法产生的全角字母、数字；存储与校验使用同一规范。"""
    return unicodedata.normalize("NFKC", str(password or ""))


def _password_digest(password, salt):
    return hashlib.sha256(f"{salt}:{_normalize_password(password)}".encode("utf-8")).hexdigest()


def _valid_account_name(name):
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,16}", name or ""))


def _valid_password(password):
    return bool(re.fullmatch(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,32}", _normalize_password(password)))


def _media_initial_password():
    """媒介账户的初始密码：MJ（媒介拼音首字母）+ 六位随机数字。"""
    return f"MJ{secrets.randbelow(1_000_000):06d}"


def _scrm_login_url():
    """优先使用部署配置；由网页触发时可自动使用当前站点地址。"""
    configured = str(getattr(app_config, "SCRM_LOGIN_URL", "") or "").strip()
    if configured:
        return configured
    try:
        return urljoin(request.url_root, "scrm/login")
    except RuntimeError:
        return ""


def _queue_media_account_notification(cur, password, recipient_open_ids, now):
    """通知进入事务出站箱，只有提交成功后才会发送。"""
    payload = json.dumps({
        "login_url": _scrm_login_url(),
        "account": "媒介",
        "password": password,
        "recipient_open_ids": recipient_open_ids or [],
    }, ensure_ascii=False)
    cur.execute("""INSERT INTO scrm_notification_outbox (event_type,payload,created_ts)
                   VALUES ('media_account_created',%s,%s)""", (payload, now))


def _send_media_account_feishu_message(payload):
    """将凭据定向发送给商单绑定的商务人员，而非发送到群聊。"""
    recipients = [str(x).strip() for x in (payload.get("recipient_open_ids") or []) if str(x).strip()]
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and recipients):
        raise RuntimeError("商单未绑定可用的飞书商务人员")
    token_response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10,
    )
    token_response.raise_for_status()
    token_body = token_response.json()
    if token_body.get("code") != 0 or not token_body.get("tenant_access_token"):
        raise RuntimeError(f"获取飞书 token 失败: {token_body.get('msg') or token_body.get('code')}")
    login_url = str(payload.get("login_url") or "").strip() or "请联系管理员获取网站登录地址"
    text = "\n".join((
        "很高兴跟您开启新的合作～",
        f"合作查询地址：{login_url}",
        f"账户：{payload.get('account') or '媒介'}",
        f"密码：{payload.get('password') or ''}",
    ))
    for open_id in recipients:
        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token_body['tenant_access_token']}"},
            json={"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书消息发送失败: {body.get('msg') or body.get('code')}")


def _dispatch_pending_media_notifications(limit=20):
    """提交后投递；失败只记录，不会影响商单/账户创建。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,payload FROM scrm_notification_outbox
                           WHERE event_type='media_account_created' AND sent_ts IS NULL
                           ORDER BY id LIMIT %s""", (limit,))
            rows = cur.fetchall()
    for row in rows:
        try:
            payload = json.loads(row.get("payload") or "{}")
            _send_media_account_feishu_message(payload)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE scrm_notification_outbox
                                   SET sent_ts=%s,attempts=attempts+1,last_error=NULL
                                   WHERE id=%s AND sent_ts IS NULL""", (int(time.time()), row["id"]))
                conn.commit()
        except Exception as exc:
            logger.warning("媒介账户飞书通知发送失败: %s", exc)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE scrm_notification_outbox
                                   SET attempts=attempts+1,last_error=%s WHERE id=%s AND sent_ts IS NULL""",
                                (str(exc)[:500], row["id"]))
                conn.commit()


def _ensure_media_system_user(cur, recipient_open_ids, now):
    """为首个成功创建的商单补齐默认媒介账户；重复调用不会重置既有密码。"""
    cur.execute("SELECT id FROM scrm_role WHERE name='媒介' LIMIT 1")
    role = cur.fetchone()
    if not role:
        cur.execute("""INSERT INTO scrm_role (name,description,status,built_in,created_ts,updated_ts)
                       VALUES ('媒介','商单自动创建的默认媒介角色','active',1,%s,%s)""", (now, now))
        role_id = cur.lastrowid
    else:
        role_id = role["id"]

    cur.execute("SELECT id FROM scrm_user WHERE name='媒介' LIMIT 1")
    user = cur.fetchone()
    if user:
        # 名称唯一，因此后续商单复用同一媒介账户；若曾被手动移除角色则自动补回。
        cur.execute("""INSERT IGNORE INTO scrm_user_role (user_id,role_id,created_ts)
                       VALUES (%s,%s,%s)""", (user["id"], role_id, now))
        return user["id"], False

    password = _media_initial_password()
    salt = os.urandom(16).hex()
    cur.execute("""INSERT INTO scrm_user
                   (name,password_hash,password_salt,initial_password,initial_password_created_ts,created_ts,updated_ts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                ("媒介", _password_digest(password, salt), salt, password, now, now, now))
    user_id = cur.lastrowid
    cur.execute("INSERT INTO scrm_user_role (user_id,role_id,created_ts) VALUES (%s,%s,%s)",
                (user_id, role_id, now))
    _queue_media_account_notification(cur, password, recipient_open_ids, now)
    return user_id, True


@bp.before_request
def require_scrm_login():
    """保护 SCRM 全部页面与接口；登录、首次初始化接口除外。"""
    public_endpoints = {"scrm.page_login", "scrm.api_auth_login", "scrm.api_auth_setup"}
    if request.endpoint in public_endpoints:
        return None
    user_id = session.get("scrm_user_id")
    if user_id:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,name,status FROM scrm_user WHERE id=%s", (user_id,))
                user = cur.fetchone()
        if user and user["status"] == "active":
            g.scrm_user = user
            return None
    session.clear()
    if request.path.startswith("/scrm/api/"):
        return jsonify({"error": "登录已失效，请重新登录", "login_required": True}), 401
    return redirect(url_for("scrm.page_login", next=request.full_path))


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


def _product_display_name(brand, product, model):
    """合并品牌、产品和型号，重复值只保留一次。"""
    parts, seen = [], set()
    for value in (brand, product, model):
        text = str(value or "").strip()
        key = re.sub(r"\s+", "", text).casefold()
        if text and key not in seen:
            parts.append(text)
            seen.add(key)
    return " ".join(parts)[:512]


def _sync_content_product_name(cur, platform, url, product_name):
    """将商单的产品名称回写到对应内容库视频。"""
    table = next((t for t in TABLES if MAP[t]["platform"] == (platform or "")), None)
    if not table or not url:
        return 0
    m = MAP[table]
    return cur.execute(
        f"UPDATE `{m['ctable']}` SET `product_name`=%s "
        f"WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s",
        (product_name or None, str(url)[:500], str(url)[:500]),
    )


def _sync_deal_product_names(cur, deal_id):
    """商单产品资料变更后同步到已关联的视频内容。"""
    cur.execute("SELECT brand,product,model FROM scrm_deal WHERE id=%s", (deal_id,))
    deal = cur.fetchone()
    if not deal:
        return 0
    product_name = _product_display_name(deal.get("brand"), deal.get("product"), deal.get("model"))
    cur.execute("SELECT platform,url FROM scrm_deal_video WHERE deal_id=%s AND category='商单'", (deal_id,))
    return sum(_sync_content_product_name(cur, row.get("platform"), row.get("url"), product_name)
               for row in cur.fetchall())


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


def _sync_deal_videos_by_actual_date(cur, deal_id, actual_date):
    """按商单实际发布时间，为空链接的视频账户匹配内容库中的同平台同账户视频。"""
    if not actual_date:
        return 0
    cur.execute("SELECT id, platform, account FROM scrm_deal_video WHERE deal_id=%s AND (url IS NULL OR url='')", (deal_id,))
    placeholders = cur.fetchall()
    synced = 0
    for video in placeholders:
        platform = (video.get("platform") or "").strip()
        account = (video.get("account") or "").strip()
        if not platform or not account:
            continue
        # 内容 UNION 已将各平台发布时间统一为秒级时间戳，按自然日匹配。
        cur.execute(f"""SELECT src_table, url, title, publish_time FROM {_content_union()}
                      WHERE platform=%s
                        AND REPLACE(REPLACE(LOWER(upname),'-',''),' ','') = REPLACE(REPLACE(LOWER(%s),'-',''),' ','')
                        AND publish_time >= UNIX_TIMESTAMP(%s)
                        AND publish_time < UNIX_TIMESTAMP(DATE_ADD(%s, INTERVAL 1 DAY))
                        AND url IS NOT NULL AND url<>''
                      ORDER BY publish_time ASC LIMIT 1""", (platform, account, actual_date, actual_date))
        matched = cur.fetchone()
        if not matched:
            continue
        cur.execute("""UPDATE scrm_deal_video SET url=%s, title=COALESCE(NULLIF(title,''),%s),
                      publish_ts=COALESCE(publish_ts,%s), category='商单', auto_matched=1 WHERE id=%s""",
                    (str(matched.get("url"))[:500], str(matched.get("title") or "")[:255],
                     _to_epoch(matched.get("publish_time")), video["id"]))
        # 自动匹配成功后同步内容库分类，避免商单视频与内容库显示不一致。
        matched_table = matched.get("src_table")
        if matched_table in MAP:
            cm = MAP[matched_table]
            cur.execute(
                f"UPDATE `{cm['ctable']}` SET `category`='商单' "
                f"WHERE `original_url`=%s OR CAST({cm['curl']} AS CHAR)=%s",
                (str(matched.get("url"))[:500], str(matched.get("url"))[:500]),
            )
            _sync_deal_product_names(cur, deal_id)
        synced += 1
    return synced


def _restore_content_category(cur, platform, url):
    """取消自动匹配时，将内容库原视频恢复为原创分类。"""
    table = next((t for t in TABLES if MAP[t]["platform"] == (platform or "")), None)
    if not table or not url:
        return
    m = MAP[table]
    cur.execute(f"SELECT id FROM `{m['ctable']}` WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s LIMIT 1", (url, url))
    matched = cur.fetchone()
    if matched:
        cur.execute(f"UPDATE `{m['ctable']}` SET `category`='原创' WHERE id=%s", (matched["id"],))


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
    # 权限管理：角色、系统账户和角色权限。权限粒度统一为页面 / 字段 / 操作。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_role` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(64) NOT NULL,
          `description` VARCHAR(255) DEFAULT NULL,
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `built_in` TINYINT(1) NOT NULL DEFAULT 0,
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`), UNIQUE KEY `uk_scrm_role_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SCRM角色'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_user` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(32) NOT NULL COMMENT '中文账户名称',
          `password_hash` VARCHAR(128) NOT NULL,
          `password_salt` VARCHAR(64) NOT NULL,
          `initial_password` VARCHAR(32) DEFAULT NULL COMMENT '仅供账户管理页查看的当前初始密码',
          `initial_password_created_ts` BIGINT DEFAULT NULL,
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`), UNIQUE KEY `uk_scrm_user_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SCRM系统账户'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_user_role` (
          `user_id` BIGINT NOT NULL,
          `role_id` BIGINT NOT NULL,
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`user_id`,`role_id`), KEY `idx_surm_role` (`role_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统账户角色关联'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_notification_outbox` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `event_type` VARCHAR(64) NOT NULL,
          `payload` JSON NOT NULL,
          `created_ts` BIGINT NOT NULL,
          `sent_ts` BIGINT DEFAULT NULL,
          `attempts` INT NOT NULL DEFAULT 0,
          `last_error` VARCHAR(500) DEFAULT NULL,
          PRIMARY KEY (`id`), KEY `idx_notification_pending` (`sent_ts`,`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书通知出站箱'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_role_permission` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `role_id` BIGINT NOT NULL,
          `permission_type` VARCHAR(16) NOT NULL COMMENT 'page/field/action',
          `resource` VARCHAR(128) NOT NULL,
          `permission_key` VARCHAR(128) NOT NULL,
          `allowed` TINYINT(1) NOT NULL DEFAULT 1,
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_role_permission` (`role_id`,`permission_type`,`resource`,`permission_key`),
          KEY `idx_role_permission` (`role_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色权限明细'
    """)
    now = int(time.time())
    cur.execute("""INSERT IGNORE INTO scrm_role (name,description,status,built_in,created_ts,updated_ts)
                   VALUES ('系统管理员','拥有全部页面、字段与操作权限','active',1,%s,%s)""", (now, now))
    cur.execute("""INSERT IGNORE INTO scrm_role (name,description,status,built_in,created_ts,updated_ts)
                   VALUES ('媒介','商单自动创建账户的默认角色','active',1,%s,%s)""", (now, now))
    for col, ddl in (
        ("initial_password", "ALTER TABLE `scrm_user` ADD COLUMN `initial_password` VARCHAR(32) DEFAULT NULL COMMENT '仅供账户管理页查看的当前初始密码' AFTER `password_salt`"),
        ("initial_password_created_ts", "ALTER TABLE `scrm_user` ADD COLUMN `initial_password_created_ts` BIGINT DEFAULT NULL AFTER `initial_password`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_user' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    # 内置管理员始终拥有权限目录中的所有项；新增加的页面/字段/操作会在启动时自动补齐。
    cur.execute("SELECT id FROM scrm_role WHERE name='系统管理员' LIMIT 1")
    admin_role = cur.fetchone()
    if admin_role:
        for permission_type, items in PERMISSION_CATALOG.items():
            for permission_key, _ in items:
                cur.execute("""INSERT IGNORE INTO scrm_role_permission
                               (role_id,permission_type,resource,permission_key,allowed,created_ts,updated_ts)
                               VALUES (%s,%s,%s,%s,1,%s,%s)""",
                            (admin_role["id"], permission_type, permission_key.split(".")[0], permission_key, now, now))
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
        CREATE TABLE IF NOT EXISTS `scrm_brand_manage` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(128) NOT NULL,
          `alias` VARCHAR(500) DEFAULT NULL COMMENT '品牌别名,逗号分隔',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_brand_manage_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='品牌管理'
    """)
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
    cur.execute("SHOW COLUMNS FROM scrm_skill LIKE 'up_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE scrm_skill ADD COLUMN up_id BIGINT NULL COMMENT '关联UP主管理.id' AFTER bind_up")
        cur.execute("ALTER TABLE scrm_skill ADD KEY idx_skill_up (up_id)")
    # 名称只用于兼容旧数据；运行关联一律使用 UP主管理 主键。
    cur.execute("""UPDATE scrm_skill S JOIN `UP主管理` C ON C.`昵称`=S.bind_up
                   SET S.up_id=C.id WHERE S.up_id IS NULL AND S.bind_up IS NOT NULL AND S.bind_up<>''""")
    # Skill ↔ UP主 1对多绑定：一个 Skill 可同时服务多个 UP 主。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_skill_binding` (
          `skill_id` BIGINT NOT NULL COMMENT 'scrm_skill.id',
          `up_id` BIGINT NOT NULL COMMENT 'UP主管理.id',
          `created_ts` BIGINT NOT NULL,
          PRIMARY KEY (`skill_id`, `up_id`),
          KEY `idx_sb_up` (`up_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Skill与UP主绑定关系(1对多)'
    """)
    # 存量迁移：把旧单列 up_id 平移进绑定表。解绑时会把 scrm_skill.up_id 置 NULL，
    # 因此 WHERE up_id IS NOT NULL 保证已解绑的旧值不会被重启回填。
    cur.execute("""INSERT IGNORE INTO scrm_skill_binding (skill_id, up_id, created_ts)
                   SELECT id, up_id, updated_ts FROM scrm_skill WHERE up_id IS NOT NULL""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_inquiry` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `fs_record_id` VARCHAR(32) NOT NULL COMMENT '飞书记录ID',
          `inquiry_no` VARCHAR(32) DEFAULT NULL COMMENT '询单ID',
          `name` VARCHAR(255) DEFAULT NULL COMMENT '询单名称',
          `inquiry_ts` BIGINT DEFAULT NULL COMMENT '询单时间',
          `sales` VARCHAR(64) DEFAULT NULL COMMENT '商务组员',
          `sales_open_ids` VARCHAR(1000) DEFAULT NULL COMMENT '商务组员飞书open_id(JSON)',
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
          `status_source` VARCHAR(16) NOT NULL DEFAULT 'feishu' COMMENT '状态来源:feishu/manual',
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
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_inquiry' AND COLUMN_NAME='sales_open_ids'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_inquiry` ADD COLUMN `sales_open_ids` VARCHAR(1000) DEFAULT NULL COMMENT '商务组员飞书open_id(JSON)' AFTER `sales`")
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_inquiry' AND COLUMN_NAME='status_source'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_inquiry` ADD COLUMN `status_source` VARCHAR(16) NOT NULL DEFAULT 'feishu' COMMENT '状态来源:feishu/manual' AFTER `status`")
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
        ("deleted_ts", "ALTER TABLE `scrm_inquiry` ADD COLUMN `deleted_ts` BIGINT DEFAULT NULL COMMENT '删除时间(软删除)' AFTER `synced_ts`"),
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
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_deal_video' AND COLUMN_NAME='auto_matched'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_deal_video` ADD COLUMN `auto_matched` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '按账户日期自动匹配' AFTER `publish_ts`")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_media` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(128) NOT NULL COMMENT '媒介公司(=品牌名)',
          `is_brand` VARCHAR(32) DEFAULT NULL COMMENT '是否品牌方',
          `cooperation_frequency` VARCHAR(64) DEFAULT NULL COMMENT '媒介合作频率',
          `cooperation_count` INT DEFAULT NULL COMMENT '合作次数',
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
    for col, ddl in (
        ("is_brand", "ALTER TABLE `scrm_media` ADD COLUMN `is_brand` VARCHAR(32) DEFAULT NULL COMMENT '是否品牌方' AFTER `name`"),
        ("cooperation_frequency", "ALTER TABLE `scrm_media` ADD COLUMN `cooperation_frequency` VARCHAR(64) DEFAULT NULL COMMENT '媒介合作频率' AFTER `is_brand`"),
        ("cooperation_count", "ALTER TABLE `scrm_media` ADD COLUMN `cooperation_count` INT DEFAULT NULL COMMENT '合作次数' AFTER `cooperation_frequency`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_media' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_business_wechat` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `wechat` VARCHAR(128) NOT NULL COMMENT '商务号/微信账户',
          `member` VARCHAR(64) NOT NULL COMMENT '商务组员',
          `feishu_mobile` VARCHAR(32) DEFAULT NULL COMMENT '飞书手机号',
          `feishu_email` VARCHAR(128) DEFAULT NULL COMMENT '飞书邮箱',
          `feishu_open_id` VARCHAR(128) DEFAULT NULL COMMENT '飞书open_id缓存',
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          `completed_ts` BIGINT DEFAULT NULL COMMENT '完成时间',
          PRIMARY KEY (`id`), UNIQUE KEY `uk_wechat` (`wechat`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商务号与组员映射'
    """)
    for col, ddl in (
        ("feishu_mobile", "ALTER TABLE `scrm_business_wechat` ADD COLUMN `feishu_mobile` VARCHAR(32) DEFAULT NULL COMMENT '飞书手机号' AFTER `member`"),
        ("feishu_email", "ALTER TABLE `scrm_business_wechat` ADD COLUMN `feishu_email` VARCHAR(128) DEFAULT NULL COMMENT '飞书邮箱' AFTER `feishu_mobile`"),
        ("feishu_open_id", "ALTER TABLE `scrm_business_wechat` ADD COLUMN `feishu_open_id` VARCHAR(128) DEFAULT NULL COMMENT '飞书open_id缓存' AFTER `feishu_email`"),
    ):
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_business_wechat' AND COLUMN_NAME=%s", (col,))
        if cur.fetchone()["c"] == 0:
            cur.execute(ddl)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `scrm_category` (
          `id` BIGINT NOT NULL AUTO_INCREMENT,
          `name` VARCHAR(64) NOT NULL,
          `parent_id` BIGINT DEFAULT NULL COMMENT '一级分类ID；为空表示一级分类',
          `status` VARCHAR(16) NOT NULL DEFAULT 'active',
          `created_ts` BIGINT NOT NULL,
          `updated_ts` BIGINT NOT NULL,
          PRIMARY KEY (`id`), UNIQUE KEY `uk_category_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商单品类'
    """)
    cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scrm_category' AND COLUMN_NAME='parent_id'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE `scrm_category` ADD COLUMN `parent_id` BIGINT DEFAULT NULL COMMENT '一级分类ID；为空表示一级分类' AFTER `name`")
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
        ("model", "ALTER TABLE `scrm_deal` ADD COLUMN `model` VARCHAR(255) DEFAULT NULL COMMENT '产品型号' AFTER `product`"),
        ("category", "ALTER TABLE `scrm_deal` ADD COLUMN `category` VARCHAR(64) DEFAULT NULL COMMENT '品类' AFTER `product`"),
        ("rebate", "ALTER TABLE `scrm_deal` ADD COLUMN `rebate` DECIMAL(12,2) DEFAULT NULL COMMENT '返点' AFTER `category`"),
        ("kpi_views", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_views` BIGINT DEFAULT NULL COMMENT 'KPI播放' AFTER `kpi`"),
        ("kpi_interactions", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_interactions` BIGINT DEFAULT NULL COMMENT 'KPI互动' AFTER `kpi_views`"),
        ("amount", "ALTER TABLE `scrm_deal` ADD COLUMN `amount` DECIMAL(12,2) DEFAULT NULL COMMENT '商单金额' AFTER `rebate`"),
        ("media_amount", "ALTER TABLE `scrm_deal` ADD COLUMN `media_amount` DECIMAL(12,2) DEFAULT NULL COMMENT '媒介金额' AFTER `amount`"),
        ("kpi_platform", "ALTER TABLE `scrm_deal` ADD COLUMN `kpi_platform` VARCHAR(32) DEFAULT NULL COMMENT 'KPI平台' AFTER `kpi_interactions`"),
        ("completed_ts", "ALTER TABLE `scrm_deal` ADD COLUMN `completed_ts` BIGINT DEFAULT NULL COMMENT '完成时间' AFTER `updated_ts`"),
        ("actual_publish_date", "ALTER TABLE `scrm_deal` ADD COLUMN `actual_publish_date` DATE DEFAULT NULL COMMENT '实际发布时间(精确到日)' AFTER `expect_ts`"),
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
        ("主页链接", "ALTER TABLE `平台运营账号` ADD COLUMN `主页链接` VARCHAR(1000) DEFAULT NULL "
                    "COMMENT '账号主页链接' AFTER `ID`"),
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
                cur.execute("SELECT id FROM scrm_deal")
                for deal in cur.fetchall():
                    _sync_deal_product_names(cur, deal["id"])
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


def _cell_text(v):
    return cell_text(v)


def _cell_ts(v):
    return cell_timestamp(v)


def _cell_money(v):
    return cell_money(v)


def _feishu_open_ids(value):
    """从飞书多维表格的人员字段中提取 open_id，兼容单选和多选结构。"""
    people = value if isinstance(value, list) else [value]
    ids = []
    for person in people:
        if not isinstance(person, dict):
            continue
        identifier = person.get("open_id") or person.get("id") or person.get("user_id")
        if identifier and str(identifier).startswith("ou_"):
            ids.append(str(identifier))
    return list(dict.fromkeys(ids))


def _resolve_sales_open_ids(cur, sales, explicit_ids=None):
    """仅以询单数据库中的商务人员姓名，在飞书可见通讯录里严格匹配。"""
    if not sales or not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        return []
    try:
        token_resp = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                   json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
        token = token_resp.json().get("tenant_access_token")
        if not token:
            return []
        matches, page_token = [], None
        while True:
            params = {"department_id": "0", "user_id_type": "open_id", "page_size": 50, "fetch_child": "true"}
            if page_token:
                params["page_token"] = page_token
            response = requests.get("https://open.feishu.cn/open-apis/contact/v3/users/find_by_department",
                                    headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
            data = response.json()
            if data.get("code") != 0:
                logger.warning("读取飞书通讯录失败: %s", data.get("msg") or data.get("code"))
                return []
            body = data.get("data") or {}
            matches.extend(str(u.get("open_id")) for u in (body.get("items") or [])
                           if str(u.get("name") or "").strip() == str(sales).strip() and str(u.get("open_id") or "").startswith("ou_"))
            if not body.get("has_more"):
                break
            page_token = body.get("page_token")
            if not page_token:
                break
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            logger.warning("飞书通讯录中存在 %s 位同名商务人员 %s，跳过密码通知", len(matches), sales)
    except Exception as exc:
        logger.warning("按姓名匹配飞书 open_id 失败: %s", exc)
    return []


def _fetch_fs_inquiries():
    """拉取飞书商单询单表全部记录"""
    client = FeishuBitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET)
    return client.list_records(FEISHU_APP_TOKEN, FEISHU_INQUIRY_TABLE)


def _fetch_fs_category_records():
    """读取飞书品类标签表的全部记录。字段：一级分类、产品类型标签。"""
    client = FeishuBitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET)
    client.session.trust_env = False
    return client.list_records(FEISHU_CATEGORY_APP_TOKEN, FEISHU_CATEGORY_TABLE)


def sync_feishu_categories():
    """将飞书品类标签表全量同步为一级分类/产品类型。"""
    records = _fetch_fs_category_records()
    roots, children = set(), {}
    for rec in records:
        fields = rec.get("fields") or {}
        root = _cell_text(fields.get("一级分类"))[:64]
        product = _cell_text(fields.get("产品类型标签"))[:64]
        if not root and not product:
            continue
        if root:
            roots.add(root)
        if root and product:
            children.setdefault(root, set()).add(product)
    now = int(time.time())
    created = updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for root in sorted(roots):
                cur.execute("SELECT id FROM scrm_category WHERE name=%s", (root,))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE scrm_category SET parent_id=NULL,status='active',updated_ts=%s WHERE id=%s", (now, row['id']))
                    updated += 1
                else:
                    cur.execute("INSERT INTO scrm_category (name,parent_id,created_ts,updated_ts) VALUES (%s,NULL,%s,%s)", (root, now, now))
                    created += 1
            for root, products in children.items():
                cur.execute("SELECT id FROM scrm_category WHERE name=%s AND parent_id IS NULL", (root,))
                parent = cur.fetchone()
                if not parent:
                    continue
                for product in sorted(products):
                    cur.execute("SELECT id FROM scrm_category WHERE name=%s", (product,))
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE scrm_category SET parent_id=%s,status='active',updated_ts=%s WHERE id=%s", (parent['id'], now, row['id']))
                        updated += 1
                    else:
                        cur.execute("INSERT INTO scrm_category (name,parent_id,created_ts,updated_ts) VALUES (%s,%s,%s,%s)", (product, parent['id'], now, now))
                        created += 1
        conn.commit()
    result = {"total": len(records), "roots": len(roots), "products": sum(len(v) for v in children.values()), "created": created, "updated": updated}
    logger.info("飞书品类标签同步完成: %s", result)
    return result


def _upsert_inquiries(cur, records):
    """按飞书 record_id 幂等写入 scrm_inquiry"""
    now = int(time.time())
    new_n = upd_n = 0
    for rec in records:
        rid = rec.get("record_id") or _cell_text((rec.get("fields") or {}).get("记录ID"))
        if not rid:
            continue
        f = rec.get("fields") or {}
        sales_open_ids = _feishu_open_ids(f.get("商务组员"))
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
            json.dumps(sales_open_ids, ensure_ascii=False) if sales_open_ids else None,
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
              (fs_record_id, inquiry_no, name, inquiry_ts, sales, sales_open_ids, media_company,
               brand, product, category, competitor_rule, expect_ts, coop_types,
               unit_count, platform_accounts, extras, follow_detail, kpi, remark,
               status, contact, wechat, amount, rebate, media_amount, synced_ts, created_ts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              inquiry_no=VALUES(inquiry_no), name=VALUES(name), inquiry_ts=VALUES(inquiry_ts),
              sales=VALUES(sales), sales_open_ids=VALUES(sales_open_ids), media_company=VALUES(media_company), brand=VALUES(brand),
              product=VALUES(product), category=VALUES(category), competitor_rule=VALUES(competitor_rule),
              expect_ts=VALUES(expect_ts), coop_types=VALUES(coop_types), unit_count=VALUES(unit_count),
              platform_accounts=VALUES(platform_accounts), extras=VALUES(extras),
              follow_detail=VALUES(follow_detail), kpi=VALUES(kpi), remark=VALUES(remark),
              status=IF(status_source='manual',status,VALUES(status)), contact=VALUES(contact), wechat=VALUES(wechat),
              amount=VALUES(amount), rebate=VALUES(rebate), media_amount=VALUES(media_amount),
              synced_ts=VALUES(synced_ts)
        """, row)
        if n == 1:
            new_n += 1
        elif n == 2:
            upd_n += 1
    return new_n, upd_n


def _ensure_won_artifacts(cur, inq):
    """询单合作成功 → 自动补建商单、合作媒介和默认媒介账户（均幂等）。"""
    now = int(time.time())
    # 1) 商单
    cur.execute("SELECT id FROM scrm_deal WHERE inquiry_id=%s LIMIT 1", (inq["id"],))
    deal = cur.fetchone()
    created_deal = not deal
    if not deal:
        cur.execute("""
            INSERT INTO scrm_deal (inquiry_id, name, brand, product, media_company, owner,
                                   coop_types, platform_accounts, kpi, amount, rebate, media_amount, expect_ts,
                                   status, created_ts, updated_ts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'won',%s,%s)
        """, (inq["id"], inq.get("name") or f"商单-{inq['inquiry_no'] or inq['id']}",
              inq.get("brand"), inq.get("product"), inq.get("media_company"), inq.get("sales"),
              inq.get("coop_types"), inq.get("platform_accounts"), inq.get("kpi"),
              inq.get("amount"), inq.get("rebate"), inq.get("media_amount"), inq.get("expect_ts"), now, now))
        cur.execute("SELECT id FROM scrm_deal WHERE inquiry_id=%s LIMIT 1", (inq["id"],))
        deal = cur.fetchone()
    deal_id = deal["id"] if deal else None
    if deal_id:
        # 商单创建与历史补数均在同一事务内补齐媒介账户；已有账户不会重置密码或重复通知。
        recipient_open_ids = _resolve_sales_open_ids(cur, inq.get("sales"))
        _ensure_media_system_user(cur, recipient_open_ids, now)
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
    # 询单联动只允许合作成功记录进入合作媒介；清理历史上未关联商单的自动记录。
    cur.execute("DELETE FROM scrm_media WHERE source='auto' AND (src_deal_id IS NULL OR src_deal_id=0)")
    cur.execute("SELECT * FROM scrm_inquiry WHERE status='合作成功' AND deleted_ts IS NULL")
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
    _dispatch_pending_media_notifications()
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
    return render_template(f"scrm/{tpl}", active=active, current_user=getattr(g, "scrm_user", None), **kw)


@bp.get("/login")
def page_login():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM scrm_user")
            needs_setup = cur.fetchone()["c"] == 0
    return render_template("scrm/login.html", needs_setup=needs_setup, next_url=request.args.get("next") or "/scrm/")


@bp.post("/api/auth/setup")
def api_auth_setup():
    """仅首个账户可调用，自动赋予内置系统管理员角色。"""
    body = request.get_json(silent=True) or {}
    name, password = str(body.get("name") or "").strip(), str(body.get("password") or "")
    if not _valid_account_name(name):
        return jsonify({"error": "账户名称须为 2–16 个中文字符"}), 400
    if not _valid_password(password):
        return jsonify({"error": "密码须为 8–32 位字母和数字，且两者均需包含"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM scrm_user")
            if cur.fetchone()["c"]:
                return jsonify({"error": "系统已完成初始化，请使用账户密码登录"}), 403
            cur.execute("SELECT id FROM scrm_role WHERE name='系统管理员' LIMIT 1")
            admin = cur.fetchone()
            if not admin:
                return jsonify({"error": "系统管理员角色未初始化，请重启服务后重试"}), 500
            salt = os.urandom(16).hex()
            try:
                cur.execute("INSERT INTO scrm_user (name,password_hash,password_salt,created_ts,updated_ts) VALUES (%s,%s,%s,%s,%s)",
                            (name, _password_digest(password, salt), salt, now, now))
            except IntegrityError:
                return jsonify({"error": "账户名称已存在"}), 400
            uid = cur.lastrowid
            cur.execute("INSERT INTO scrm_user_role (user_id,role_id,created_ts) VALUES (%s,%s,%s)", (uid, admin["id"], now))
        conn.commit()
    session.clear()
    session["scrm_user_id"] = uid
    return jsonify({"ok": True, "redirect": "/scrm/"})


@bp.post("/api/auth/login")
def api_auth_login():
    body = request.get_json(silent=True) or {}
    name, password = str(body.get("name") or "").strip(), _normalize_password(body.get("password") or "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,password_hash,password_salt,status FROM scrm_user WHERE name=%s", (name,))
            user = cur.fetchone()
    if not user or user["status"] != "active" or _password_digest(password, user.get("password_salt") or "") != user.get("password_hash"):
        return jsonify({"error": "账户名称或密码错误"}), 401
    session.clear()
    session["scrm_user_id"] = user["id"]
    return jsonify({"ok": True, "redirect": str(body.get("next") or "/scrm/")})


@bp.post("/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("scrm.page_login"))


@bp.get("/")
def page_overview():
    return _page("overview.html", "overview")


@bp.get("/comments")
def page_comments():
    return _page("comments.html", "comments")


@bp.get("/videos")
def page_videos():
    return _page("videos.html", "videos", video_mode="all", page_title="视频列表", page_desc="九平台视频 / 笔记 / 帖子聚合展示,点击标题直达原内容")


@bp.get("/original-videos")
def page_original_videos():
    return _page("videos.html", "original_videos", video_mode="original", page_title="原创推荐视频", page_desc="原创分类且产品名称已审核通过的视频")


@bp.get("/deal-videos")
def page_deal_videos():
    return _page("videos.html", "deal_videos", video_mode="deal", page_title="商单执行视频", page_desc="已关联商单且商单尚未结束或取消的视频")


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


@bp.get("/brand-management")
def page_brand_management():
    return _page("brand_management.html", "brand_management")


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


@bp.get("/permission-management")
def page_permission_management():
    return _page("permission_management.html", "permission_management")


# ============================================================
# API: 监控总览
# ============================================================
@bp.get("/api/permission-catalog")
def api_permission_catalog():
    return jsonify({"catalog": {kind: [{"key": k, "label": label} for k, label in rows]
                                for kind, rows in PERMISSION_CATALOG.items()}})


@bp.get("/api/roles")
def api_roles():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT R.*, COUNT(P.id) AS permission_count
                           FROM scrm_role R LEFT JOIN scrm_role_permission P ON P.role_id=R.id AND P.allowed=1
                           GROUP BY R.id ORDER BY R.built_in DESC, R.updated_ts DESC, R.id DESC""")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.post("/api/roles/save")
def api_roles_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:64]
    description = str(body.get("description") or "").strip()[:255] or None
    if not name:
        return jsonify({"error": "角色名称必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                if body.get("id"):
                    cur.execute("UPDATE scrm_role SET name=%s,description=%s,updated_ts=%s WHERE id=%s", (name, description, now, int(body["id"])))
                else:
                    cur.execute("INSERT INTO scrm_role (name,description,created_ts,updated_ts) VALUES (%s,%s,%s,%s)", (name, description, now, now))
            except IntegrityError:
                return jsonify({"error": "角色名称已存在"}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/roles/<int:rid>/permissions")
def api_role_permissions(rid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,built_in FROM scrm_role WHERE id=%s", (rid,))
            role = cur.fetchone()
            if not role:
                return jsonify({"error": "角色不存在"}), 404
            cur.execute("SELECT permission_type,resource,permission_key,allowed FROM scrm_role_permission WHERE role_id=%s", (rid,))
            permissions = cur.fetchall()
    return jsonify({"role": role, "permissions": permissions})


@bp.post("/api/roles/<int:rid>/permissions")
def api_role_permissions_save(rid):
    body = request.get_json(silent=True) or {}
    items = body.get("permissions") or []
    allowed = {(kind, key) for kind, pairs in PERMISSION_CATALOG.items() for key, _ in pairs}
    selected = set()
    for item in items:
        kind, key = str(item.get("type") or ""), str(item.get("key") or "")
        if (kind, key) not in allowed:
            return jsonify({"error": "存在无效权限项"}), 400
        selected.add((kind, key))
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scrm_role WHERE id=%s", (rid,))
            if not cur.fetchone():
                return jsonify({"error": "角色不存在"}), 404
            cur.execute("DELETE FROM scrm_role_permission WHERE role_id=%s", (rid,))
            for kind, key in selected:
                cur.execute("""INSERT INTO scrm_role_permission (role_id,permission_type,resource,permission_key,allowed,created_ts,updated_ts)
                               VALUES (%s,%s,%s,%s,1,%s,%s)""", (rid, kind, key.split(".")[0], key, now, now))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/system-users")
def api_system_users():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT U.id,U.name,U.status,U.created_ts,U.updated_ts,
                           CASE WHEN U.initial_password IS NULL OR U.initial_password='' THEN 0 ELSE 1 END AS has_initial_password,
                           GROUP_CONCAT(R.name ORDER BY R.name SEPARATOR '、') AS role_names,
                           GROUP_CONCAT(R.id ORDER BY R.id SEPARATOR ',') AS role_ids
                           FROM scrm_user U LEFT JOIN scrm_user_role UR ON UR.user_id=U.id
                           LEFT JOIN scrm_role R ON R.id=UR.role_id
                           GROUP BY U.id ORDER BY U.updated_ts DESC,U.id DESC""")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.get("/api/system-users/<int:uid>/initial-password")
def api_system_user_initial_password(uid):
    """按需返回初始密码，避免将密码随账户列表一并传输。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT initial_password FROM scrm_user WHERE id=%s", (uid,))
            user = cur.fetchone()
    if not user:
        return jsonify({"error": "账户不存在"}), 404
    if not user.get("initial_password"):
        return jsonify({"error": "该账户未保存可查看的初始密码"}), 404
    return jsonify({"password": user["initial_password"]})


@bp.post("/api/system-users/save")
def api_system_users_save():
    body = request.get_json(silent=True) or {}
    name, password = str(body.get("name") or "").strip(), str(body.get("password") or "")
    role_ids = body.get("role_ids") or []
    try:
        role_ids = sorted({int(x) for x in role_ids})
    except (TypeError, ValueError):
        return jsonify({"error": "角色数据无效"}), 400
    if not _valid_account_name(name):
        return jsonify({"error": "账户名称须为 2–16 个中文字符"}), 400
    if not body.get("id") and not _valid_password(password):
        return jsonify({"error": "密码须为 8–32 位字母和数字，且两者均需包含"}), 400
    if password and not _valid_password(password):
        return jsonify({"error": "密码须为 8–32 位字母和数字，且两者均需包含"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if role_ids:
                marks = ",".join(["%s"] * len(role_ids))
                cur.execute(f"SELECT COUNT(*) c FROM scrm_role WHERE id IN ({marks})", role_ids)
                if cur.fetchone()["c"] != len(role_ids):
                    return jsonify({"error": "选择的角色不存在"}), 400
            try:
                if body.get("id"):
                    uid = int(body["id"])
                    cur.execute("SELECT id FROM scrm_user WHERE id=%s", (uid,))
                    if not cur.fetchone():
                        return jsonify({"error": "账户不存在"}), 404
                    if password:
                        salt = os.urandom(16).hex()
                        cur.execute("UPDATE scrm_user SET name=%s,password_hash=%s,password_salt=%s,updated_ts=%s WHERE id=%s", (name, _password_digest(password, salt), salt, now, uid))
                    else:
                        cur.execute("UPDATE scrm_user SET name=%s,updated_ts=%s WHERE id=%s", (name, now, uid))
                else:
                    salt = os.urandom(16).hex()
                    cur.execute("INSERT INTO scrm_user (name,password_hash,password_salt,created_ts,updated_ts) VALUES (%s,%s,%s,%s,%s)", (name, _password_digest(password, salt), salt, now, now))
                    uid = cur.lastrowid
                cur.execute("DELETE FROM scrm_user_role WHERE user_id=%s", (uid,))
                for role_id in role_ids:
                    cur.execute("INSERT INTO scrm_user_role (user_id,role_id,created_ts) VALUES (%s,%s,%s)", (uid, role_id, now))
            except IntegrityError:
                return jsonify({"error": "账户名称已存在"}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/system-users/status")
def api_system_users_status():
    body = request.get_json(silent=True) or {}
    try:
        uid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "账户无效"}), 400
    status = "disabled" if body.get("status") == "disabled" else "active"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE scrm_user SET status=%s,updated_ts=%s WHERE id=%s", (status, int(time.time()), uid))
            if cur.rowcount == 0:
                return jsonify({"error": "账户不存在"}), 404
        conn.commit()
    return jsonify({"ok": True})


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
_COMMENT_READ_SLOTS = threading.BoundedSemaphore(2)


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
            rows, more = CommentReadService(cur).load_thread_page(table, root_pk, request.args, offset)
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
                part, more = CommentReadService(cur).load_thread_page(root['src_table'], root['root_pk'], request.args)
                rows.extend(part)
                threads.append(dict(root, offset=0, has_more=more, batch_size=THREAD_PAGE_SIZE))
    return jsonify(rows=rows, total=total, page=page, size=size, threads=threads)


@bp.get("/api/comment/<table>/<int:rid>")
@_table_required
def api_comment_detail(table, rid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = CommentReadService(cur).load_detail(table, rid)
    if not row:
        return jsonify({"error": "评论不存在"}), 404
    return jsonify(row)


@bp.post("/api/comment/<table>/<int:rid>/analyze")
@_table_required
def api_comment_analyze(table, rid):
    """手动触发一次分析与回复决策；本地不再直连模型。"""
    context = resolve_comment_context(table, rid)
    if not context:
        return jsonify({"error": "评论不存在"}), 404
    generated = evaluate_comment(context, "manual_regenerate")
    if not generated.get("available"):
        return jsonify({"ok": False, "error": generated.get("error", "回复服务不可用")}), 502
    analysis = generated["analysis"]
    is_neg, has_intent = analysis["is_negative"], analysis["has_purchase_intent"]
    ctype = analysis.get("comment_type") or ""
    # 高级负向类型:无条件标注负向 + 高级负向,并立即推送飞书
    high_neg = ctype in ADVANCED_NEGATIVE_TYPES
    if high_neg:
        is_neg = True
    reply = (generated.get("reply") or "")[:300] if generated.get("action") == "reply" else ""
    human_review = generated.get("action") == "human_review"
    mark_analyzed(table, rid, is_neg, has_intent, reply, ctype,
                  high_negative=high_neg, human_review=human_review)
    # 人工编辑过的文案优先于本次模型生成结果。返回数据库中的最终有效回复，
    # 防止前端在重新生成后把人工内容临时覆盖掉。
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT CAST(`reply_suggestion` AS CHAR) AS reply, IFNULL(`reply_manual_override`,0) AS manual_reply_override "
                f"FROM `{table}` WHERE `id`=%s",
                (rid,),
            )
            effective_reply = cur.fetchone() or {}
    manual_reply_override = bool(effective_reply.get("manual_reply_override"))
    reply = effective_reply.get("reply") or ""
    if high_neg:
        from analyzer import send_feishu_high_alert
        send_feishu_high_alert({
            "table": table, "id": rid,
            # resolve_comment_context returns the comment body as `comment`.
            # Using `content` here made manually re-analysed high-risk alerts
            # arrive in Feishu without their original text.
            "nickname": "", "content": context.get("comment") or "",
            "type": ctype,
        })
    return jsonify({"ok": True, "negative": is_neg, "high_negative": high_neg, "human_review": human_review, "intent": has_intent, "reply": reply,
                    "manual_reply_override": manual_reply_override,
                    "analysis": analysis,
                    "reply_service": {k: generated.get(k) for k in ("available", "action", "error", "human_reason")}})


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
    mode = (request.args.get("mode") or "").strip()
    if mode == "original":
        clauses.extend(["V.category = '原创'", "COALESCE(V.product_reviewed, 0) = 1"])
    elif mode == "deal":
        clauses.extend([
            "V.category = '商单'",
            "EXISTS (SELECT 1 FROM scrm_deal_video DV JOIN scrm_deal D ON D.id=DV.deal_id "
            "WHERE DV.url=V.url AND DV.category='商单' AND D.status NOT IN ('ended','canceled'))",
        ])
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
    kpi_status_filter = (request.args.get("kpi_status") or "").strip()
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) c FROM {_content_union()}{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"SELECT src_table, platform, content_id, title, url, cover_url, product_name, product_reviewed, upname, add_ts, "
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
            commercial_names = {}
            deal_links = {}
            urls = [str(row.get("url") or "")[:500] for row in rows if row.get("url")]
            if urls:
                placeholders = ",".join(["%s"] * len(urls))
                cur.execute(f"""SELECT DV.url,D.brand,D.product,D.model
                              ,D.id deal_id,D.name deal_name
                              FROM scrm_deal_video DV JOIN scrm_deal D ON D.id=DV.deal_id
                              WHERE DV.url IN ({placeholders})
                              ORDER BY D.updated_ts DESC""", urls)
                matches = cur.fetchall()
                commercial_names = {
                    str(row.get("url") or ""): _product_display_name(row.get("brand"), row.get("product"), row.get("model"))
                    for row in matches if row.get("deal_id")
                }
                for row in matches:
                    deal_links.setdefault(str(row.get("url") or ""), {"id": row.get("deal_id"), "name": row.get("deal_name")})
    for r in rows:
        link = deal_links.get(str(r.get("url") or ""), {})
        r["deal_id"] = link.get("id")
        r["deal_name"] = link.get("name")
        if r.get("category") == "商单":
            r["product_name"] = commercial_names.get(str(r.get("url") or "")) or r.get("product_name")
            r["product_reviewed"] = 1
        st = cmap.get((r["src_table"], str(r["content_id"])))
        r["neg_count"] = int(st["neg"] or 0) if st else 0
        r["intent_count"] = int(st["intent"] or 0) if st else 0
        r["kpi_views_target"] = None
        r["kpi_interactions_target"] = None
        r["kpi_play_reached"] = False
        r["kpi_interaction_reached"] = False
        r["kpi_status"] = "—"
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT DV.amount,DV.category,D.kpi_platform,C.cpme_ratio,C.cpe_ratio,C.original_base_value
                              FROM scrm_deal_video DV JOIN scrm_deal D ON D.id=DV.deal_id
                              LEFT JOIN scrm_kpi_config C ON C.platform=COALESCE(D.kpi_platform,DV.platform)
                              WHERE DV.url=%s LIMIT 1""", (r.get("url") or "",))
                k = cur.fetchone()
        if (r.get("category") or "原创") != "商单":
            # 原创目标直接使用设置管理中对应平台的原创基础值，不依赖商单关联记录。
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT original_base_value, cpme_ratio, cpe_ratio FROM scrm_kpi_config WHERE platform=%s LIMIT 1", (r.get("platform"),))
                    original_cfg = cur.fetchone()
            if original_cfg:
                r["kpi_views_target"], r["kpi_interactions_target"] = _calc_kpi(
                    original_cfg.get("original_base_value"), original_cfg.get("cpme_ratio"), original_cfg.get("cpe_ratio"))
        elif k and k.get("amount") is not None and k.get("cpme_ratio") and k.get("cpe_ratio"):
                r["kpi_views_target"], r["kpi_interactions_target"] = _calc_kpi(k["amount"], k["cpme_ratio"], k["cpe_ratio"])
        if r["kpi_views_target"] is not None:
            interaction = sum(_metric_number(r.get(x)) or 0 for x in ("like_count", "collect_count", "share_count", "comment_count"))
            r["actual_interactions"] = interaction
            r["kpi_play_reached"] = r["kpi_views_target"] is not None and _metric_number(r.get("view_count")) is not None and _metric_number(r.get("view_count")) >= int(r["kpi_views_target"])
            r["kpi_interaction_reached"] = r["kpi_interactions_target"] is not None and interaction >= int(r["kpi_interactions_target"])
            r["kpi_status"] = "已达标" if r["kpi_play_reached"] and r["kpi_interaction_reached"] else "未达标"
    if kpi_status_filter in ("reached", "unreached"):
        want = "已达标" if kpi_status_filter == "reached" else "未达标"
        rows = [r for r in rows if r.get("kpi_status") == want]
        total = len(rows)
    return jsonify({"rows": rows, "total": total, "page": page, "size": size})


@bp.post("/api/videos/product-review")
def api_video_product_review():
    """批量审核原创视频的 OCR 品牌/产品名称；商单名称始终以商单数据为准。"""
    items = (request.get_json(silent=True) or {}).get("items") or []
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"error": "请选择 1 至 100 条原创视频"}), 400
    requested = {(str(item.get("src_table") or ""), str(item.get("content_id") or "")) for item in items}
    requested &= {(table, content_id) for table, content_id in requested if table in MAP and content_id}
    reviewed = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table, content_id in requested:
                m = MAP[table]
                reviewed += cur.execute(
                    f"UPDATE `{m['ctable']}` SET product_reviewed=1 "
                    f"WHERE CAST(`{m['cid']}` AS CHAR)=%s "
                    f"AND COALESCE(NULLIF(category,''),'原创')='原创' "
                    f"AND product_name IS NOT NULL AND product_name<>''", (content_id,))
        conn.commit()
    return jsonify({"ok": True, "reviewed": reviewed})


@bp.post("/api/videos/product-name")
def api_video_product_name_update():
    """保存列表行内编辑的原创产品名称；修改后须重新人工审核。"""
    body = request.get_json(silent=True) or {}
    changes = body.get("changes") or []
    if not isinstance(changes, list) or not changes or len(changes) > 100:
        return jsonify({"error": "请提交 1 至 100 条产品名称修改"}), 400
    updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for change in changes:
                table = str(change.get("src_table") or "")
                content_id = str(change.get("content_id") or "")
                product_name = str(change.get("product_name") or "").strip()[:512]
                if table not in MAP or not content_id:
                    continue
                m = MAP[table]
                updated += cur.execute(
                    f"UPDATE `{m['ctable']}` SET product_name=%s, product_reviewed=0, "
                    f"product_checked_ts=IF(%s='',NULL,product_checked_ts) "
                    f"WHERE CAST(`{m['cid']}` AS CHAR)=%s "
                    f"AND COALESCE(NULLIF(category,''),'原创')='原创'",
                    (product_name or None, product_name, content_id))
        conn.commit()
    return jsonify({"ok": True, "updated": updated})


def _title_product_match(title, candidates):
    """仅在标题或 OCR 文本命中品牌库时回填，避免猜测。"""
    normalized = re.sub(r"\s+", "", str(title or "")).casefold()
    for candidate in sorted(candidates, key=lambda item: len(item[1]), reverse=True):
        name = candidate[1]
        tokens = [re.sub(r"\s+", "", str(v or "")).casefold() for v in candidate[0]]
        if name and any(token and len(token) >= 2 and token in normalized for token in tokens):
            return name
    return ""


def _cover_ocr_text(cover_url):
    """安全获取封面并用本地 RapidOCR 取字；限制图片大小，拒绝内网地址。"""
    url = str(cover_url or "").strip()
    if not url:
        return ""
    try:
        for _ in range(3):
            url = _public_product_url(url)
            response = requests.get(url, headers={"User-Agent": "SCRM-Product-OCR/1.0"},
                                    timeout=(5, 15), allow_redirects=False, stream=True)
            if 300 <= response.status_code < 400 and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                response.close()
                continue
            response.raise_for_status()
            if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                response.close()
                return ""
            data = b""
            for chunk in response.iter_content(64 * 1024):
                data += chunk
                if len(data) > 6 * 1024 * 1024:
                    response.close()
                    return ""
            response.close()
            from param_ingest import ocr_image_bytes
            return ocr_image_bytes(data, max_height=3000).strip()[:4000]
    except Exception:
        logger.warning("原创视频封面 OCR 失败", exc_info=True)
    return ""


def _identify_original_products(items):
    """用标题和封面 OCR 文本识别品牌库；无命中也记录本次检查。"""
    requested = {(str(item.get("src_table") or ""), str(item.get("content_id") or "")) for item in items}
    requested &= {(table, content_id) for table, content_id in requested if table in MAP and content_id}
    updated = 0
    checked = 0
    # OCR 涉及网络和本地推理，不能在数据库事务中执行，否则会阻塞视频列表查询。
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name,alias FROM scrm_brand_manage WHERE name IS NOT NULL AND name<>''")
            candidates = []
            for row in cur.fetchall():
                name = str(row.get("name") or "").strip()
                # “未知”等占位项不能作为识别结果；别名支持逗号、斜杠及换行分隔。
                if not name or name in {"未知", "其他", "无"}:
                    continue
                aliases = [x.strip() for x in re.split(r"[,，/、;；\n]+", str(row.get("alias") or "")) if x.strip()]
                candidates.append(((name, *aliases), name))
    for table, content_id in requested:
        m = MAP[table]
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT CAST({m['ctitle']} AS CHAR) title, CAST({m.get('ccover', 'NULL')} AS CHAR) cover_url, category "
                            f"FROM `{m['ctable']}` WHERE CAST(`{m['cid']}` AS CHAR)=%s LIMIT 1", (content_id,))
                row = cur.fetchone()
        if not row or (row.get("category") or "原创") == "商单":
            continue
        ocr_text = _cover_ocr_text(row.get("cover_url"))
        product_name = _title_product_match(f"{row.get('title') or ''}\n{ocr_text}", candidates)
        with get_conn() as conn:
            with conn.cursor() as cur:
                if product_name:
                    updated += cur.execute(f"UPDATE `{m['ctable']}` SET product_name=%s WHERE CAST(`{m['cid']}` AS CHAR)=%s",
                                          (product_name, content_id))
                cur.execute(f"UPDATE `{m['ctable']}` SET product_checked_ts=%s WHERE CAST(`{m['cid']}` AS CHAR)=%s",
                            (int(time.time()), content_id))
            conn.commit()
        checked += 1
    return {"updated": updated, "checked": checked}


def _pending_original_product_items(limit=10):
    """取尚未 OCR 的原创内容；每条只处理一次，避免重复下载封面。"""
    pending = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                if len(pending) >= limit:
                    break
                m = MAP[table]
                remaining = limit - len(pending)
                cur.execute(f"""SELECT CAST(`{m['cid']}` AS CHAR) AS content_id
                                FROM `{m['ctable']}`
                                WHERE COALESCE(NULLIF(category,''),'原创')='原创'
                                  AND (product_name IS NULL OR product_name='')
                                  AND product_checked_ts IS NULL
                                ORDER BY add_ts DESC LIMIT %s""", (remaining,))
                pending.extend({"src_table": table, "content_id": row["content_id"]} for row in cur.fetchall())
    return pending


_PRODUCT_WORKER_STARTED = False
_PRODUCT_WORKER_LOCK = threading.Lock()


def start_product_recognition_worker():
    """后台自动识别历史缺失项及后续新入库的原创视频。"""
    global _PRODUCT_WORKER_STARTED
    with _PRODUCT_WORKER_LOCK:
        if _PRODUCT_WORKER_STARTED:
            return
        _PRODUCT_WORKER_STARTED = True

    def run():
        while True:
            try:
                items = _pending_original_product_items(limit=10)
                if items:
                    result = _identify_original_products(items)
                    logger.info("原创视频产品自动识别完成: 检查 %s 条，回填 %s 条", result["checked"], result["updated"])
                    continue
            except Exception:
                logger.exception("原创视频产品自动识别任务失败")
            time.sleep(60)

    threading.Thread(target=run, name="product-ocr", daemon=True).start()


@bp.post("/api/videos/identify-original-products")
def api_identify_original_products():
    """保留内部接口；常规识别由后台任务自动执行。"""
    items = (request.get_json(silent=True) or {}).get("items") or []
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"error": "视频标识无效或单次超过 100 条"}), 400
    return jsonify({"ok": True, **_identify_original_products(items)})


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
    # 高级别 = 数据库高级负向标注(highneg=1) 或 命中高风险关键词
    if level == "high":
        like = " OR ".join(["content LIKE %s"] * len(HIGH_WORDS))
        clauses_extra.append(f"(IFNULL(highneg,0)=1 OR ({like}))")
        p_extra.extend(f"%{w}%" for w in HIGH_WORDS)
    elif level == "normal":
        like = " AND ".join(["content NOT LIKE %s"] * len(HIGH_WORDS))
        clauses_extra.append(f"(IFNULL(highneg,0)=0 AND ({like}))")
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
        high = bool(r.get("highneg")) or any(w in (r["content"] or "") for w in HIGH_WORDS)
        r["level"] = "high" if high else "normal"
        ctype = (r.get("comment_type") or "").strip()
        r["alert_type"] = (f"高级负向·{ctype}" if high and ctype else "高级负向告警" if high else "负面情感告警")
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
    cur.execute("SELECT id, name, status, brand, product, amount FROM scrm_deal "
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
    amounts = [float(deal["amount"]) for deal in deals if deal.get("amount") not in (None, "")]
    stats = {"deal_count": len(deals), "video_count": len(valid), "matched_count": len(keys),
             "comment_count": 0, "intent_count": 0, "negative_count": 0,
             "deal_amount_total": round(sum(amounts), 2),
             "deal_amount_avg": round(sum(amounts) / len(amounts), 2) if amounts else None}
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
                f"SELECT U.src_table, U.src_pk, U.comment_id, U.parent_comment_id, U.nickname, U.content, U.ctime, U.neg, U.intent, "
                f"U.comment_type, U.highneg, U.analyzed, U.handled, U.reviewed "
                f"FROM {_union()} WHERE (U.src_table,U.content_id) IN ({ph}) "
                f"ORDER BY U.ctime DESC LIMIT 300", args)
            samples = _with_platform(cur.fetchall())
            # 为品牌详情页补充评论树层级，最多展示 12 层；评论内容仍按时间倒序排列。
            comment_map = {(r.get("src_table"), str(r.get("comment_id"))): r for r in samples if r.get("comment_id") is not None}
            for r in samples:
                depth, parent = 0, r.get("parent_comment_id")
                seen = set()
                while parent not in (None, "", 0, "0") and depth < 11:
                    marker = (r.get("src_table"), str(parent))
                    if marker in seen:
                        break
                    seen.add(marker)
                    parent_row = comment_map.get(marker)
                    depth += 1
                    if not parent_row:
                        break
                    parent = parent_row.get("parent_comment_id")
                r["depth"] = depth
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
            for k in ("deal_count", "video_count", "matched_count", "comment_count", "intent_count",
                      "negative_count", "deal_amount_total", "deal_amount_avg"):
                    b[k] = sc[k]
            brands = [b for b in brands if b.get("deal_count", 0) > 0]
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
            brand.update({k: sc[k] for k in ("deal_count", "video_count", "matched_count", "comment_count",
                                             "intent_count", "negative_count", "deal_amount_total", "deal_amount_avg")})
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
            cur.execute("SELECT status, COUNT(*) c FROM scrm_inquiry WHERE deleted_ts IS NULL GROUP BY status")
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
    clauses.insert(0, "deleted_ts IS NULL")
    where = " WHERE " + " AND ".join(clauses)
    follow_cutoff = int(time.time()) - 5 * 86400
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 二核后 5 天仍未明确合作或拒绝，自动结束跟进并标记为无意向。
            cur.execute("""UPDATE scrm_inquiry
                          SET status='无意向', synced_ts=%s, follow_stage_ts=%s
                          WHERE follow_stage='second' AND status IN ('跟进中','有意向')
                            AND follow_stage_ts IS NOT NULL AND follow_stage_ts < %s""",
                        (int(time.time()), int(time.time()), follow_cutoff))
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
    mobile, email = str(body.get("feishu_mobile") or "").strip()[:32], str(body.get("feishu_email") or "").strip()[:128]
    if not wechat or not member:
        return jsonify({"error": "微信号和商务组员必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                if body.get("id"):
                    cur.execute("UPDATE scrm_business_wechat SET wechat=%s,member=%s,feishu_mobile=%s,feishu_email=%s,feishu_open_id=NULL,updated_ts=%s WHERE id=%s",
                                (wechat, member, mobile or None, email or None, now, int(body["id"])))
                else:
                    cur.execute("""INSERT INTO scrm_business_wechat (wechat,member,feishu_mobile,feishu_email,created_ts,updated_ts)
                                 VALUES (%s,%s,%s,%s,%s,%s)
                                 ON DUPLICATE KEY UPDATE member=VALUES(member),feishu_mobile=VALUES(feishu_mobile),
                                   feishu_email=VALUES(feishu_email),feishu_open_id=NULL,updated_ts=VALUES(updated_ts)""",
                                (wechat, member, mobile or None, email or None, now, now))
            except Exception as exc:
                return jsonify({"error": str(exc)[:160]}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/categories")
def api_categories():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.*, p.name AS parent_name
                         FROM scrm_category c LEFT JOIN scrm_category p ON p.id=c.parent_id
                         ORDER BY c.parent_id IS NOT NULL, c.parent_id, c.status DESC, c.name""")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.get("/api/brand-management")
def api_brand_management():
    q = (request.args.get("q") or "").strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if q:
                like = f"%{q}%"
                cur.execute("SELECT * FROM scrm_brand_manage WHERE name LIKE %s OR alias LIKE %s ORDER BY updated_ts DESC, id DESC", (like, like))
            else:
                cur.execute("SELECT * FROM scrm_brand_manage ORDER BY updated_ts DESC, id DESC")
            rows = cur.fetchall()
    return jsonify({"rows": rows})


@bp.post("/api/brand-management/save")
def api_brand_management_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:128]
    alias = str(body.get("alias") or "").strip()[:500] or None
    if not name:
        return jsonify({"error": "品牌名称必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                if body.get("id"):
                    cur.execute("UPDATE scrm_brand_manage SET name=%s,alias=%s,updated_ts=%s WHERE id=%s",
                                (name, alias, now, int(body["id"])))
                else:
                    cur.execute("INSERT INTO scrm_brand_manage (name,alias,created_ts,updated_ts) VALUES (%s,%s,%s,%s)",
                                (name, alias, now, now))
            except IntegrityError:
                return jsonify({"error": "品牌名称已存在"}), 400
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/brand-management/delete")
def api_brand_management_delete():
    body = request.get_json(silent=True) or {}
    try:
        bid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法品牌 id"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scrm_brand_manage WHERE id=%s", (bid,))
            if cur.rowcount == 0:
                return jsonify({"error": "品牌不存在"}), 404
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/categories/save")
def api_categories_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:64]
    parent_id = body.get("parent_id")
    try:
        parent_id = int(parent_id) if parent_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return jsonify({"error": "一级分类无效"}), 400
    if not name:
        return jsonify({"error": "分类名称必填"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if parent_id:
                cur.execute("SELECT id FROM scrm_category WHERE id=%s AND parent_id IS NULL", (parent_id,))
                if not cur.fetchone():
                    return jsonify({"error": "一级分类不存在"}), 400
            if body.get("id"):
                cid = int(body["id"])
                if parent_id == cid:
                    return jsonify({"error": "不能将分类设置为自身的一级分类"}), 400
                cur.execute("UPDATE scrm_category SET name=%s, parent_id=%s, status='active', updated_ts=%s WHERE id=%s",
                            (name, parent_id, now, cid))
            else:
                cur.execute("""INSERT INTO scrm_category (name,parent_id,created_ts,updated_ts) VALUES (%s,%s,%s,%s)
                             ON DUPLICATE KEY UPDATE status='active',updated_ts=VALUES(updated_ts)""", (name, parent_id, now, now))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/categories/sync-feishu")
def api_categories_sync_feishu():
    try:
        return jsonify(sync_feishu_categories())
    except Exception as exc:
        logger.exception("飞书品类标签同步失败")
        return jsonify({"error": str(exc)[:300]}), 502


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
            now = int(time.time())
            n = cur.execute("UPDATE scrm_inquiry SET status=%s,status_source='manual', synced_ts=%s, follow_stage_ts=%s WHERE id=%s",
                            (status, now, now, iid))
            if n:
                cur.execute("SELECT * FROM scrm_inquiry WHERE id=%s", (iid,))
                if status == "合作成功":
                    _ensure_won_artifacts(cur, cur.fetchone())
        conn.commit()
    if not n:
        return jsonify({"error": "询单不存在"}), 404
    _dispatch_pending_media_notifications()
    return jsonify({"ok": True})


@bp.post("/api/inquiries/<int:iid>/delete")
def api_inquiry_delete(iid):
    """删除询单，并清理该询单自动联动生成的商单、视频和合作媒介。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scrm_inquiry WHERE id=%s", (iid,))
            if not cur.fetchone():
                # 删除操作保持幂等，避免重复点击或网络重试时误报 404。
                return jsonify({"ok": True, "already_deleted": True})
            cur.execute("SELECT id FROM scrm_deal WHERE inquiry_id=%s", (iid,))
            deal_ids = [int(r["id"]) for r in cur.fetchall()]
            if deal_ids:
                ph = ",".join(["%s"] * len(deal_ids))
                cur.execute(f"SELECT platform, url, auto_matched FROM scrm_deal_video WHERE deal_id IN ({ph})", deal_ids)
                for video in cur.fetchall():
                    if video.get("auto_matched"):
                        _restore_content_category(cur, video.get("platform"), video.get("url"))
                cur.execute(f"DELETE FROM scrm_deal_video WHERE deal_id IN ({ph})", deal_ids)
                cur.execute(f"DELETE FROM scrm_media WHERE source='auto' AND src_deal_id IN ({ph})", deal_ids)
                cur.execute(f"DELETE FROM scrm_deal WHERE id IN ({ph})", deal_ids)
            # 保留飞书记录的 tombstone，后续同步时不会把用户删除的询单重新插回来。
            cur.execute("UPDATE scrm_inquiry SET deleted_ts=%s, synced_ts=%s WHERE id=%s",
                        (int(time.time()), int(time.time()), iid))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/inquiries/batch-delete")
def api_inquiries_batch_delete():
    body = request.get_json(silent=True) or {}
    raw_ids = body.get("ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"error": "ids必须为数组"}), 400
    try:
        inquiry_ids = sorted({int(x) for x in raw_ids if int(x) > 0})
    except (TypeError, ValueError):
        return jsonify({"error": "询单ID无效"}), 400
    if not inquiry_ids:
        return jsonify({"error": "请至少选择一条询单"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(inquiry_ids))
            cur.execute(f"SELECT id FROM scrm_deal WHERE inquiry_id IN ({ph})", inquiry_ids)
            deal_ids = [int(r["id"]) for r in cur.fetchall()]
            if deal_ids:
                dph = ",".join(["%s"] * len(deal_ids))
                cur.execute(f"SELECT platform, url, auto_matched FROM scrm_deal_video WHERE deal_id IN ({dph})", deal_ids)
                for video in cur.fetchall():
                    if video.get("auto_matched"):
                        _restore_content_category(cur, video.get("platform"), video.get("url"))
                cur.execute(f"DELETE FROM scrm_deal_video WHERE deal_id IN ({dph})", deal_ids)
                cur.execute(f"DELETE FROM scrm_media WHERE source='auto' AND src_deal_id IN ({dph})", deal_ids)
                cur.execute(f"DELETE FROM scrm_deal WHERE id IN ({dph})", deal_ids)
            now = int(time.time())
            cur.execute(f"UPDATE scrm_inquiry SET deleted_ts=%s, synced_ts=%s WHERE id IN ({ph})", [now, now] + inquiry_ids)
            deleted = cur.rowcount
        conn.commit()
    return jsonify({"ok": True, "deleted": deleted})


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
    _dispatch_pending_media_notifications()
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
                       (SELECT MAX(V.publish_ts) FROM scrm_deal_video V WHERE V.deal_id=D.id AND V.url IS NOT NULL AND V.url<>'' AND V.publish_ts IS NOT NULL) latest_publish_ts,
                       (SELECT COUNT(*) FROM scrm_deal_video V WHERE V.deal_id=D.id AND V.url IS NOT NULL AND V.url<>'' AND V.url LIKE 'http%%') published_video_count
                FROM scrm_deal D{where}
                ORDER BY D.updated_ts DESC LIMIT %s OFFSET %s
            """, params + [size, (page - 1) * size])
            rows = cur.fetchall()
    for row in rows:
        if row.get("actual_publish_date") and hasattr(row["actual_publish_date"], "isoformat"):
            row["actual_publish_date"] = row["actual_publish_date"].isoformat()
        row["display_expect_ts"] = row.get("latest_publish_ts")
        row["display_actual_publish_date"] = row.get("actual_publish_date") or (
            datetime.fromtimestamp(int(row["latest_publish_ts"])).strftime("%Y-%m-%d")
            if row.get("latest_publish_ts") else ""
        )
        if not row.get("actual_publish_date") and row.get("latest_publish_ts"):
            row["actual_publish_date"] = row["display_actual_publish_date"]
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
                    video["kpi_views"], video["kpi_interactions"] = _calc_kpi(
                        kpi_cfg["original_base_value"], kpi_cfg["cpme_ratio"], kpi_cfg["cpe_ratio"])
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
            latest_publish_ts = max((v.get("publish_ts") or 0 for v in videos if v.get("url")), default=0)
            if deal.get("actual_publish_date") and hasattr(deal["actual_publish_date"], "isoformat"):
                deal["actual_publish_date"] = deal["actual_publish_date"].isoformat()
            if not deal.get("actual_publish_date") and latest_publish_ts:
                deal["actual_publish_date"] = datetime.fromtimestamp(int(latest_publish_ts)).strftime("%Y-%m-%d")
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
            cur.execute("SELECT id,name FROM scrm_category WHERE parent_id IS NULL AND status='active' ORDER BY name")
            categories = cur.fetchall()
            selling_point_templates, _ = _deal_selling_point_rows(cur, did, deal)
            historical_original_videos = []
            historical_brands = [item.strip() for item in re.split(r"[,，、/;；\s]+", str(deal.get("brand") or "")) if item.strip()]
            if historical_brands:
                brand_where = " OR ".join(["product_name LIKE %s"] * len(historical_brands))
                cur.execute(f"""SELECT platform,content_id,title,url,upname,product_name,view_count,publish_time
                                FROM {_content_union()}
                                WHERE category='原创' AND product_reviewed=1 AND ({brand_where})
                                ORDER BY publish_time DESC, add_ts DESC LIMIT 100""",
                            [f"%{brand}%" for brand in historical_brands])
                historical_original_videos = cur.fetchall()
            # 商单展示视频对应的评论，保留父子关系并计算最多 12 层层级。
            deal_comments = []
            content_keys = []
            video_urls = [str(v.get("url") or "")[:500] for v in videos if v.get("url")]
            if video_urls:
                vph = ",".join(["%s"] * len(video_urls))
                cur.execute(f"SELECT src_table,content_id FROM {_content_union()} WHERE url IN ({vph})", video_urls)
                content_keys = [(x["src_table"], str(x["content_id"])) for x in cur.fetchall()]
            if content_keys:
                kph = ",".join(["(%s,%s)"] * len(content_keys))
                kargs = [v for key in content_keys for v in key]
                cur.execute(
                    f"SELECT U.src_table,U.src_pk,U.comment_id,U.parent_comment_id,U.nickname,U.content,U.ctime,U.neg,U.intent,"
                    f"U.comment_type,U.highneg,U.analyzed,U.handled,U.reviewed FROM {_union()} "
                    f"WHERE (U.src_table,U.content_id) IN ({kph}) ORDER BY U.ctime DESC LIMIT 300", kargs)
                deal_comments = _with_platform(cur.fetchall())
                cmap = {(x.get("src_table"), str(x.get("comment_id"))): x for x in deal_comments if x.get("comment_id") is not None}
                for x in deal_comments:
                    depth, parent, seen = 0, x.get("parent_comment_id"), set()
                    while parent not in (None, "", 0, "0") and depth < 11:
                        marker = (x.get("src_table"), str(parent))
                        if marker in seen:
                            break
                        seen.add(marker)
                        parent_row = cmap.get(marker)
                        depth += 1
                        if not parent_row:
                            break
                        parent = parent_row.get("parent_comment_id")
                    x["depth"] = depth
        # 详情页自动推进的已发布/KPI已完成状态需要持久化，列表页才能读取到同一状态。
        conn.commit()
    deal["data_completed"] = data_completed
    deal["display_expect_ts"] = latest_publish_ts or None
    deal["display_status"] = ("已结束" if deal.get("status") == "ended" else ("已取消" if deal.get("status") == "canceled" else ("KPI已完成" if data_completed else ("已发布" if has_published_video and deal.get("status") == "published" else deal.get("status")))))
    return jsonify({"deal": deal, "videos": videos, "inquiry": inquiry,
                    "status_map": DEAL_STATUS_MAP, "team": TEAM,
                    "kpi_configs": kpi_configs, "kpi_platforms": KPI_PLATFORMS,
                    "categories": categories, "selling_point_templates": selling_point_templates,
                    "historical_original_videos": historical_original_videos,
                    "historical_brands": historical_brands,
                    "comment_samples": deal_comments})


@bp.get("/api/deals/<int:did>/selling-point-templates")
def api_deal_selling_point_templates(did):
    """返回当前商单品类的全部痛点，作为卖点模板编辑数据。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT brand,product,model,category FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
            if not deal:
                return jsonify({"error": "商单不存在"}), 404
            rows, context = _deal_selling_point_rows(cur, did, deal)
    return jsonify({**context, "rows": rows})


@bp.post("/api/deals/<int:did>/selling-point-templates")
def api_deal_save_selling_point_templates(did):
    """保存类目卖点模板，并将这些痛点同步为当前商品的人工匹配。"""
    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "卖点模板数据格式不正确"}), 400
    if len(rows) > 200:
        return jsonify({"error": "一次最多保存 200 条痛点"}), 400
    normalized, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            return jsonify({"error": "卖点模板存在无效行"}), 400
        pain_id = str(row.get("pain_point_id") or "").strip()[:64]
        if not pain_id or pain_id in seen:
            return jsonify({"error": "痛点编号不能为空且不能重复"}), 400
        try:
            stars = int(row.get("star_rating"))
        except (TypeError, ValueError):
            return jsonify({"error": "痛点星级必须为 1-5 的整数"}), 400
        if not 1 <= stars <= 5:
            return jsonify({"error": "痛点星级必须为 1-5"}), 400
        seen.add(pain_id)
        normalized.append((pain_id, stars, str(row.get("reply_guidance") or "").strip()[:5000]))

    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT brand,product,model,category FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
            if not deal:
                return jsonify({"error": "商单不存在"}), 404
            _, context = _deal_selling_point_rows(cur, did, deal)
            category = context["category"]
            if not category:
                return jsonify({"error": "未找到该产品对应的痛点，请先维护产品或品类痛点"}), 400
            if normalized:
                ids = [row[0] for row in normalized]
                placeholders = ",".join(["%s"] * len(ids))
                cur.execute(f"SELECT pain_point_id FROM category_pain_points WHERE category=%s "
                            f"AND pain_point_id IN ({placeholders})", [category, *ids])
                valid_ids = {row["pain_point_id"] for row in cur.fetchall()}
                if valid_ids != set(ids):
                    return jsonify({"error": "存在不属于当前品类的痛点，无法保存"}), 400
                model_id = context["model_id"]
                note = f"商单#{did} 卖点模板同步"
                for pain_id, stars, guidance in normalized:
                    # 星级和标准回复均保存到产品匹配表，不影响同品类的其他商品。
                    cur.execute("""INSERT INTO model_pain_point_matches
                                   (model_id,pain_point_id,matched_params,star_rating,standard_reply,match_source,note,state,created_ts,updated_ts)
                                   VALUES (%s,%s,%s,%s,%s,'manual',%s,'usable',%s,%s)
                                   ON DUPLICATE KEY UPDATE star_rating=VALUES(star_rating),
                                       standard_reply=VALUES(standard_reply),match_source='manual',note=VALUES(note),
                                       state='usable',updated_ts=VALUES(updated_ts)""",
                                (model_id, pain_id, json.dumps([], ensure_ascii=False), stars, guidance or None, note, now, now))
            else:
                model_id = context["model_id"]
        conn.commit()
    return jsonify({"ok": True, "model_id": model_id, "synced_count": len(normalized)})


def _deal_model_id(brand, product):
    """生成稳定的知识库型号主键；显示名称仍保留原始产品名称。"""
    raw = f"{brand or ''}-{product or ''}".upper()
    normalized = re.sub(r"[^A-Z0-9]+", "-", raw).strip("-")[:64]
    return normalized or ("DEAL-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12].upper())


def _deal_selling_point_rows(cur, did, deal):
    """以产品为主体取卖点；品类仅用于寻找该产品的候选痛点，不作为保存归属。"""
    product = (deal.get("product") or "").strip()
    declared_category = (deal.get("category") or "").strip()
    model_name = (deal.get("model") or "").strip()
    product_key = model_name or product
    model_id = _deal_model_id(deal.get("brand"), product_key)
    model_category = ""
    # 若商单填写了型号，优先复用型号事实表中的产品主键，保证同步进入真正的产品匹配记录。
    if product_key:
        cur.execute("SELECT model_id,model_name,aliases,category FROM model_facts WHERE state='usable'")
        for fact in cur.fetchall():
            aliases = fact.get("aliases") or []
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except ValueError:
                    aliases = []
            names = [fact.get("model_id"), fact.get("model_name"), *(aliases if isinstance(aliases, list) else [])]
            if any(product_key.casefold() == str(name or "").strip().casefold() for name in names):
                model_id = fact["model_id"]
                model_category = (fact.get("category") or "").strip()
                break
    # 品类库可能使用具体产品名（如“扫地机器人”）而商单使用上级分类（如“清洁电器”）。
    candidates = [x for x in (product, model_category, declared_category) if x]
    category = ""
    for candidate in candidates:
        cur.execute("SELECT 1 FROM category_pain_points WHERE category=%s LIMIT 1", (candidate,))
        if cur.fetchone():
            category = candidate
            break
    if not category:
        return [], {"category": "", "model_id": model_id, "product": product}
    cur.execute("""SELECT P.pain_point_id,P.category,P.pain_point,
                          COALESCE(M.star_rating,P.star_rating) AS star_rating,
                          COALESCE(M.standard_reply,P.reply_guidance) AS reply_guidance,
                          P.state, M.match_id IS NOT NULL AS matched
                   FROM category_pain_points P
                   LEFT JOIN model_pain_point_matches M
                     ON M.pain_point_id=P.pain_point_id AND M.model_id=%s AND M.state='usable'
                   WHERE P.category=%s
                   ORDER BY COALESCE(M.star_rating,P.star_rating) DESC,P.priority DESC,P.pain_point_id""",
                (model_id, category))
    return cur.fetchall(), {"category": category, "model_id": model_id, "product": product}


def _text_from_html(value):
    """轻量清洗页面片段；参数页可能没有固定的 DOM 结构。"""
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _public_product_url(raw_url):
    """仅允许公开网页，防止参数查询接口被用于访问内网。"""
    raw = html.unescape(str(raw_url or "").strip()).replace("\\_", "_")
    # 允许直接粘贴 Markdown 链接、富文本残留或带说明文字的 URL。
    markdown_url = re.search(r"\]\((https?://[^)\s]+)\)", raw, re.I)
    plain_url = re.search(r"https?://[^\s<>\"']+", raw, re.I)
    url = (markdown_url.group(1) if markdown_url else (plain_url.group(0) if plain_url else raw)).rstrip("，。；;、")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请输入公开的 HTTP/HTTPS 产品页面链接")
    if parsed.port not in (None, 80, 443):
        raise ValueError("产品链接仅支持标准 Web 端口")
    host = parsed.hostname.rstrip(".").lower()
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError("无法解析产品链接的域名")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("不允许查询内网或本机地址")
    return url


def _product_specs_from_page(page):
    """从 JSON-LD、参数表和定义列表提取可供人工复核的产品参数。"""
    def add(label, value, result):
        label, value = _text_from_html(label), _text_from_html(value)
        if not label or not value or len(label) > 64 or len(value) > 255:
            return
        if not any(item["label"] == label for item in result):
            result.append({"label": label, "value": value})

    specs, product_data = [], {}
    for block in re.findall(r"<script[^>]+type=[\"']application/ld\+json[^>]*>(.*?)</script>", page, re.I | re.S):
        try:
            data = json.loads(html.unescape(block).strip())
        except (ValueError, TypeError):
            continue
        queue = data if isinstance(data, list) else [data]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
            kinds = item.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if "Product" not in kinds:
                continue
            product_data = item
            for prop in item.get("additionalProperty") or []:
                if isinstance(prop, dict):
                    add(prop.get("name"), prop.get("value"), specs)
            break

    for label, value in re.findall(r"<tr[^>]*>\s*<(?:th|td)[^>]*>(.*?)</(?:th|td)>\s*<td[^>]*>(.*?)</td>\s*</tr>", page, re.I | re.S):
        add(label, value, specs)
    for label, value in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.I | re.S):
        add(label, value, specs)

    title = _text_from_html(next(iter(re.findall(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)), ""))
    name = _text_from_html(product_data.get("name") or product_data.get("model") or title)
    brand = product_data.get("brand") or ""
    if isinstance(brand, dict):
        brand = brand.get("name") or ""
    return {"name": name[:128], "brand": _text_from_html(str(brand))[:32], "specs": specs[:40]}


def _preview_product_link(raw_url):
    """带重定向逐跳检查地下载一页产品资料，最大读取 2MB。"""
    url = _public_product_url(raw_url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SCRM-Product-Preview/1.0)",
               "Accept": "text/html,application/xhtml+xml"}
    for _ in range(4):
        response = requests.get(url, headers=headers, timeout=(5, 15), allow_redirects=False, stream=True)
        if 300 <= response.status_code < 400 and response.headers.get("Location"):
            response.close()
            url = _public_product_url(urljoin(url, response.headers["Location"]))
            continue
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            response.close()
            raise ValueError("该链接不是可解析的网页产品页")
        content = response.raw.read(2 * 1024 * 1024 + 1)
        response.close()
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("产品页面过大，无法查询")
        encoding = response.encoding or "utf-8"
        return url, content.decode(encoding, errors="replace")
    raise ValueError("产品链接重定向次数过多")


def _spec_key(label, used):
    known = {"产品型号": "model", "型号": "model", "额定功率": "rated_power", "额定电压": "rated_voltage",
             "额定频率": "rated_frequency", "容量": "capacity", "净容量": "capacity", "尺寸": "dimensions",
             "外形尺寸": "dimensions", "能效等级": "energy_efficiency", "耗水量": "water_consumption",
             "噪音": "noise", "噪声": "noise", "重量": "weight", "颜色": "color"}
    base = known.get(label) or (label if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", label) else "spec")
    index, key = 1, base
    while key in used:
        index += 1
        key = f"{base}_{index}"
    return key


def _product_specs_from_script(content, max_bytes=1024 * 1024):
    """解析上传的产品参数脚本；仅按文本读取，绝不执行其中代码。"""
    content = str(content or "").strip()
    if not content:
        raise ValueError("上传的参数脚本为空")
    if len(content.encode("utf-8")) > max_bytes:
        raise ValueError(f"参数资料不能超过 {max_bytes // (1024 * 1024)}MB")
    if "<html" in content.lower() or "<table" in content.lower() or "application/ld+json" in content.lower():
        return _product_specs_from_page(content)

    specs, name, brand = [], "", ""
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if isinstance(data, dict):
        name = str(data.get("model") or data.get("model_name") or data.get("产品型号") or data.get("name") or "")
        raw_brand = data.get("brand") or data.get("品牌") or ""
        brand = str(raw_brand.get("name") if isinstance(raw_brand, dict) else raw_brand)
        candidate = data.get("specs") or data.get("parameters") or data.get("参数") or data
        if isinstance(candidate, dict):
            for label, value in candidate.items():
                if isinstance(value, dict):
                    value = value.get("value") or value.get("值")
                if isinstance(value, (str, int, float, bool)):
                    specs.append({"label": str(label).strip(), "value": str(value).strip()})
    if not specs:
        for line in content.splitlines():
            line = line.strip().strip("|")
            if not line or set(line) <= {"-", "|", ":", "：", " "}:
                continue
            pieces = [x.strip() for x in re.split(r"\s*[：:]\s*|\s*\|\s*|\t+", line, maxsplit=1)]
            if len(pieces) == 2 and pieces[0] and pieces[1]:
                specs.append({"label": pieces[0][:64], "value": pieces[1][:255]})
    seen, clean_specs = set(), []
    for item in specs:
        label, value = item["label"].strip(), item["value"].strip()
        if label and value and label not in seen and len(label) <= 64 and len(value) <= 255:
            clean_specs.append({"label": label, "value": value})
            seen.add(label)
    return {"name": name[:128], "brand": brand[:32], "specs": clean_specs[:40]}


def _feishu_doc_raw_content(raw_url):
    """通过应用授权读取飞书 Wiki/Docx 的原文，不经由用户提供的 URL 直连。"""
    url = str(raw_url or "").strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "feishu.cn" or host.endswith(".feishu.cn")):
        raise ValueError("请输入飞书 Wiki 或文档链接")
    wiki_match = re.search(r"/wiki/([A-Za-z0-9]+)", parsed.path)
    docx_match = re.search(r"/docx/([A-Za-z0-9]+)", parsed.path)
    if not wiki_match and not docx_match:
        raise ValueError("未识别到飞书 Wiki 或文档编号")
    client = FeishuBitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET)
    try:
        token = client.tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        document_id = docx_match.group(1) if docx_match else None
        node_type, document_title = ("docx" if document_id else ""), ""
        spreadsheet_token, sheet_id = "", ""
        if not document_id:
            response = requests.get("https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
                                    headers=headers, params={"token": wiki_match.group(1)}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise ValueError(f"飞书文档读取失败：{payload.get('msg') or '请确认应用已获文档权限'}")
            node = (payload.get("data") or {}).get("node") or {}
            node_type = node.get("obj_type") or ""
            document_title = str(node.get("title") or "").strip()
            if not node.get("obj_token"):
                raise ValueError("未读取到飞书文档编号")
            if node_type == "sheet":
                spreadsheet_token = node["obj_token"]
                sheet_id = (parse_qs(parsed.query).get("sheet") or [""])[0]
            else:
                document_id = node["obj_token"]
        if node_type == "sheet":
            if not sheet_id:
                response = requests.get(f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
                                        headers=headers, timeout=20)
                response.raise_for_status()
                payload = response.json()
                sheets = (payload.get("data") or {}).get("sheets") or []
                sheet_id = (sheets[0] or {}).get("sheet_id") if sheets else ""
            if not sheet_id:
                raise ValueError("飞书表格中未找到可读取的工作表")
            cell_range = quote(f"{sheet_id}!A1:ZZ500", safe="")
            response = requests.get(f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{cell_range}",
                                    headers=headers, timeout=20)
            # Wiki 链接的 sheet 参数可能是旧工作表标识；失败时回退到首个可访问表。
            first_payload = response.json()
            if first_payload.get("code") != 0:
                response = requests.get(f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
                                        headers=headers, timeout=20)
                response.raise_for_status()
                sheets_payload = response.json()
                sheets = (sheets_payload.get("data") or {}).get("sheets") or []
                fallback_sheet_id = (sheets[0] or {}).get("sheet_id") if sheets else ""
                if not fallback_sheet_id:
                    raise ValueError(f"飞书表格读取失败：{first_payload.get('msg') or '未找到可访问工作表'}")
                cell_range = quote(f"{fallback_sheet_id}!A1:ZZ500", safe="")
                response = requests.get(f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{cell_range}",
                                        headers=headers, timeout=20)
        elif node_type == "doc":
            response = requests.get(f"https://open.feishu.cn/open-apis/doc/v2/documents/{document_id}/raw_content",
                                    headers=headers, timeout=20)
        elif node_type == "docx":
            response = requests.get(f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content",
                                    headers=headers, timeout=20)
        else:
            raise ValueError(f"当前飞书链接类型为 {node_type or '未知'}，暂不支持读取")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError(f"飞书文档读取失败：{payload.get('msg') or '请确认应用已获文档权限'}")
        data = payload.get("data") or {}
        if node_type == "sheet":
            values = ((data.get("valueRange") or {}).get("values") or [])
            # 表格大纲常把“字段：”与值拆在相邻单元格；只保留这一类结构，
            # 避免空白单元格和整段脚本被误当成一个参数值。
            def cell_text(cell):
                if isinstance(cell, str):
                    return cell.strip()
                if isinstance(cell, list):
                    return "".join(cell_text(item) for item in cell).strip()
                if isinstance(cell, dict):
                    return str(cell.get("text") or cell.get("name") or "").strip()
                return "" if cell is None else str(cell).strip()

            lines = []
            for row in values:
                cells = [cell_text(cell) for cell in row]
                cells = [cell for cell in cells if cell and cell != "None"]
                for index, cell in enumerate(cells):
                    match = re.match(r"^(.{1,64}?)[：:]\s*(.*)$", cell)
                    if not match:
                        continue
                    label, value = match.group(1).strip(), match.group(2).strip()
                    if not value and index + 1 < len(cells):
                        value = cells[index + 1]
                    if label and value:
                        lines.append(f"{label}：{value}")
            content = "\n".join(lines).strip()
        else:
            content = (data.get("content") or "").strip()
        if not content:
            raise ValueError("飞书文档为空，或应用无权读取正文")
        return content, document_title
    except requests.RequestException:
        logger.warning("飞书文档读取请求失败", exc_info=True)
        raise ValueError("飞书文档读取失败，请稍后重试")


def _preview_specs_response(deal, parsed, source_note):
    used, preview_specs = set(), []
    for item in parsed["specs"]:
        candidate = str(item.get("key") or "")
        key = candidate if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate) and candidate not in used else _spec_key(item["label"], used)
        used.add(key)
        preview_specs.append({"key": key, "label": item["label"], "value": item["value"],
                              "condition": item.get("condition") or "资料识别参数"})
    return jsonify({"source_note": source_note, "brand": parsed["brand"] or deal.get("brand") or "",
                    "model_name": parsed["name"] or deal.get("product") or "",
                    "category": deal.get("category") or "其他", "specs": preview_specs,
                    "selling_points": parsed.get("selling_points") or [],
                    "warning": "未识别到参数，请手动补充后再同步" if not preview_specs else ""})


def _model_name_from_document_title(title):
    """从“品牌+型号单测/测评”类文档标题中取型号，避免将整段标题写入型号。"""
    title = str(title or "")
    match = re.search(r"[—\-——]\s*([^—\-——]{2,80}?)\s*(?:单测|测评|评测|大纲)", title)
    return match.group(1).strip(" -—") if match else ""


def _refine_feishu_product_material(content):
    """从测评大纲中提取可复核的产品事实与卖点，排除视频制作字段。"""
    raw = _product_specs_from_script(content, max_bytes=5 * 1024 * 1024)
    merged = "\n".join(f"{item['label']}：{item['value']}" for item in raw["specs"])
    facts, selling_points = [], []

    def add(key, label, value, evidence):
        if not any(item["key"] == key for item in facts):
            facts.append({"key": key, "label": label, "value": value, "condition": "飞书测评大纲提炼"})
            selling_points.append({"title": label, "evidence": evidence})

    match = re.search(r"洗够\s*(\d+)\s*次.*?取衣后\s*(\d+)\s*分钟.*?自动(?:启动)?自清洁", merged, re.S)
    if match:
        add("smart_self_cleaning", "智能自清洁", f"累计洗够 {match.group(1)} 次后，取衣 {match.group(2)} 分钟自动启动自清洁",
            "文档明确描述了触发次数与自动清洁时机")
    if re.search(r"冷水.*?除菌", merged, re.S):
        bra = re.search(r"无痕\s*bra.*?(不脱胶.*?不变形|不变形.*?不脱胶)", merged, re.I | re.S)
        value = "文档称冷水可除菌"
        if bra:
            value += "；无痕 Bra 实测不脱胶、不变形"
        add("cold_water_sterilization", "冷水除菌与护衣", value, "冷水除菌主张及无痕 Bra 实测描述")
    blood = re.search(r"血渍.*?(洗得干干净净.*?不留痕|不留痕.*?洗得干干净净)", merged, re.S)
    if blood:
        add("blood_stain_wash", "血渍洗涤表现", "实测血渍洗净、不留痕", "文档中的实测结果描述")
    dry = re.search(r"体感干爽标准：\s*(-?\d+(?:\.\d+)?%?)", merged)
    if dry:
        add("dryness_feel_standard", "体感干爽标准", dry.group(1), "文档标注的体感干爽指标")
    if re.search(r"水面.*?纹丝不动", merged, re.S):
        add("running_stability", "运行稳定性", "洗烘过程中杯中水面几乎纹丝不动", "文档中的运行观察")
    return {"name": raw.get("name") or "", "brand": raw.get("brand") or "", "specs": facts,
            "selling_points": selling_points}


@bp.post("/api/deals/<int:did>/product-params-preview")
def api_deal_product_params_preview(did):
    body = request.get_json(silent=True) or {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT brand,product,category FROM scrm_deal WHERE id=%s", (did,))
                deal = cur.fetchone()
        if not deal:
            return jsonify({"error": "商单不存在"}), 404
        source_url, page = _preview_product_link(body.get("source_url"))
        parsed = _product_specs_from_page(page)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.RequestException:
        logger.warning("产品参数链接查询失败", exc_info=True)
        return jsonify({"error": "链接查询失败，请确认页面可公开访问"}), 502
    # 部分厂商产品页由前端动态加载参数。若该链接已核验入库，则回显已存的
    # 同源参数供确认，避免动态页面空壳导致用户看不到可同步内容。
    if not parsed["specs"]:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT brand,model_name,specs FROM model_facts WHERE source LIKE %s ORDER BY verified_at DESC LIMIT 1",
                            (f"%{source_url}%",))
                cached = cur.fetchone()
        if cached:
            try:
                saved_specs = json.loads(cached.get("specs") or "{}")
            except (TypeError, ValueError):
                saved_specs = {}
            if isinstance(saved_specs, dict):
                parsed = {"brand": cached.get("brand") or "", "name": cached.get("model_name") or "", "specs": [
                    {"label": str(item.get("label") or key), "value": str(item.get("value") or "")}
                    for key, item in saved_specs.items() if isinstance(item, dict) and item.get("value") not in (None, "")
                ]}
    return _preview_specs_response(deal, parsed, source_url)


@bp.post("/api/deals/<int:did>/product-params-script-preview")
def api_deal_product_params_script_preview(did):
    body = request.get_json(silent=True) or {}
    filename = re.sub(r"[\\/:*?\"<>|]", "_", str(body.get("filename") or "参数脚本"))[:128]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT brand,product,category FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
    if not deal:
        return jsonify({"error": "商单不存在"}), 404
    try:
        parsed = _product_specs_from_script(body.get("content"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _preview_specs_response(deal, parsed, f"上传参数脚本：{filename}")


@bp.post("/api/deals/<int:did>/feishu-doc-preview")
def api_deal_feishu_doc_preview(did):
    body = request.get_json(silent=True) or {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT brand,product,category FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
    if not deal:
        return jsonify({"error": "商单不存在"}), 404
    try:
        content, document_title = _feishu_doc_raw_content(body.get("source_url"))
        parsed = _refine_feishu_product_material(content)
        parsed["name"] = parsed.get("name") or _model_name_from_document_title(document_title)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _preview_specs_response(deal, parsed, f"飞书文档：{str(body.get('source_url') or '').strip()}")


@bp.post("/api/deals/<int:did>/sync-product-params")
def api_deal_sync_product_params(did):
    """将商单中的产品资料审核后写入 L1 型号事实表（按型号编号幂等更新）。"""
    body = request.get_json(silent=True) or {}
    specs = body.get("specs")
    if not isinstance(specs, dict) or not specs:
        return jsonify({"error": "请至少填写一项核心参数"}), 400
    invalid_specs = [key for key, item in specs.items()
                     if not str(key).strip() or not isinstance(item, dict)
                     or item.get("value") in (None, "")]
    if invalid_specs:
        return jsonify({"error": "每项参数均需包含参数键和参数值"}), 400

    def text(key, limit, fallback=""):
        return str(body.get(key) or fallback).strip()[:limit]

    today = datetime.now().date().isoformat()
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,brand,product,category FROM scrm_deal WHERE id=%s", (did,))
            deal = cur.fetchone()
            if not deal:
                return jsonify({"error": "商单不存在"}), 404

            brand = text("brand", 32, deal.get("brand") or "")
            model_name = text("model_name", 128, deal.get("product") or "")
            category = text("category", 16, deal.get("category") or "其他")
            model_id = text("model_id", 64, _deal_model_id(brand, model_name))
            if not brand or not model_name or not model_id:
                return jsonify({"error": "品牌、产品型号和型号编号均不能为空"}), 400

            aliases = body.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [x.strip() for x in re.split(r"[,，;；\n]+", aliases) if x.strip()]
            if not isinstance(aliases, list):
                return jsonify({"error": "别名格式不正确"}), 400
            aliases = [str(x).strip()[:128] for x in aliases if str(x).strip()]
            source_note = text("source_note", 500)
            if not source_note:
                return jsonify({"error": "请填写参数来源（如官网链接、说明书或商单附件）"}), 400
            source = {
                "type": "商单参数同步",
                "deal_id": did,
                "deal_name": deal.get("name") or "",
                "reference": source_note,
                "synced_at": now,
            }
            values = (
                brand, model_name, json.dumps(aliases, ensure_ascii=False), category,
                json.dumps(specs, ensure_ascii=False), json.dumps([], ensure_ascii=False),
                json.dumps(source, ensure_ascii=False), today, model_id,
            )
            cur.execute("SELECT model_id FROM model_facts WHERE model_id=%s", (model_id,))
            exists = bool(cur.fetchone())
            if exists:
                cur.execute("""UPDATE model_facts SET brand=%s, model_name=%s, aliases=%s,
                               category=%s, specs=%s, tested_by=%s, source=%s,
                               confidence='mid', verified_at=%s, state='usable'
                               WHERE model_id=%s""", values)
            else:
                cur.execute("""INSERT INTO model_facts
                               (brand,model_name,aliases,category,specs,known_issues,tested_by,
                                source,confidence,verified_at,state,model_id)
                               VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,'mid',%s,'usable',%s)""", values)
        conn.commit()
    # 产品参数已提交后再做匹配，避免同步函数读到未提交的 specs。
    from knowledge import sync_model_pain_point_matches
    pain_point_match_count = sync_model_pain_point_matches(model_id)
    return jsonify({"ok": True, "action": "updated" if exists else "created",
                    "model_id": model_id, "pain_point_match_count": pain_point_match_count})


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
                    (inquiry_id,name,brand,product,model,media_company,owner,coop_types,platform_accounts,kpi,
                     amount,rebate,media_amount,expect_ts,status,note,created_ts,updated_ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'won',%s,%s,%s)""",
                    (base.get("inquiry_id"), new_name, base.get("brand"), base.get("product"), base.get("model"), base.get("media_company"),
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
    for key, limit in (("name", 255), ("brand", 128), ("product", 255), ("model", 255), ("media_company", 128),
                       ("owner", 64), ("coop_types", 255), ("platform_accounts", 500), ("kpi", 255)):
        if key in body:
            sets.append(f"{key}=%s"); params.append(str(body.get(key) or "")[:limit])
    for key, limit in (("order_no", 64), ("category", 64)):
        if key in body:
            value = str(body.get(key) or "").strip()[:limit]
            if key == "category" and value:
                with get_conn() as check_conn:
                    with check_conn.cursor() as check_cur:
                        check_cur.execute("SELECT id FROM scrm_category WHERE name=%s AND parent_id IS NULL AND status='active'", (value,))
                        if not check_cur.fetchone():
                            return jsonify({"error": "品类必须选择品类管理中的一级分类"}), 400
            sets.append(f"{key}=%s"); params.append(value)
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
    actual_publish_date = body.get("actual_publish_date") if "actual_publish_date" in body else None
    if "actual_publish_date" in body:
        if actual_publish_date in (None, ""):
            actual_publish_date = None
        else:
            actual_publish_date = str(actual_publish_date).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", actual_publish_date):
                return jsonify({"error": "实际发布时间必须为YYYY-MM-DD"}), 400
        sets.append("actual_publish_date=%s"); params.append(actual_publish_date)
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
                    views, interactions = _calc_kpi(cfg["original_base_value"], cfg["cpme_ratio"], cfg["cpe_ratio"])
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
            if n and any(key in body for key in ("brand", "product", "model")):
                _sync_deal_product_names(cur, did)
            synced_videos = _sync_deal_videos_by_actual_date(cur, did, actual_publish_date) if n and "actual_publish_date" in body else 0
        conn.commit()
    if not n:
        return jsonify({"error": "商单不存在"}), 404
    return jsonify({"ok": True, "synced_videos": synced_videos if "actual_publish_date" in body else 0})


@bp.post("/api/deals/<int:did>/delete")
def api_deal_delete(did):
    """删除商单及其展示视频；自动匹配的视频恢复为原创分类。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scrm_deal WHERE id=%s", (did,))
            if not cur.fetchone():
                return jsonify({"error": "商单不存在"}), 404
            cur.execute("SELECT platform, url, auto_matched FROM scrm_deal_video WHERE deal_id=%s", (did,))
            for video in cur.fetchall():
                if video.get("auto_matched"):
                    _restore_content_category(cur, video.get("platform"), video.get("url"))
            cur.execute("DELETE FROM scrm_deal_video WHERE deal_id=%s", (did,))
            cur.execute("DELETE FROM scrm_deal WHERE id=%s", (did,))
        conn.commit()
    return jsonify({"ok": True})


@bp.post("/api/deals/<int:did>/video")
def api_deal_video_add(did):
    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "视频链接需以 http:// 或 https:// 开头"}), 400
    now = int(time.time())
    synced = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, amount, brand, product, model FROM scrm_deal WHERE id=%s", (did,))
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
            if url:
                cur.execute("SELECT id FROM scrm_deal_video WHERE deal_id=%s AND url=%s LIMIT 1",
                            (did, url[:500]))
            if url and cur.fetchone():
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
                    views, interactions = _calc_kpi(kpi["original_base_value"], kpi["cpme_ratio"], kpi["cpe_ratio"])
                elif kpi["amount"] not in (None, "") and kpi["cpme_ratio"] and kpi["cpe_ratio"]:
                    views, interactions = _calc_kpi(kpi["amount"], kpi["cpme_ratio"], kpi["cpe_ratio"])
                else:
                    views = interactions = None
                cur.execute("UPDATE scrm_deal SET kpi_views=%s,kpi_interactions=%s WHERE id=%s", (views, interactions, did))
            if url:
                cur.execute("UPDATE scrm_deal SET status=IF(status IN ('ended','kpi_done','canceled'),status,'published'), updated_ts=%s WHERE id=%s", (now, did))
            else:
                cur.execute("UPDATE scrm_deal SET updated_ts=%s WHERE id=%s", (now, did))
            # 商单展示链接同时回写内容库对应平台的视频原链接和分类。
            table = next((t for t in TABLES if MAP[t]["platform"] == str(body.get("platform") or "")), None)
            if table and url:
                m = MAP[table]
                cur.execute(
                    f"SELECT id, CAST({m['ctitle']} AS CHAR) AS matched_title FROM `{m['ctable']}` WHERE `original_url`=%s OR CAST({m['curl']} AS CHAR)=%s LIMIT 1",
                    (url[:500], url[:500]))
                matched = cur.fetchone()
                if matched:
                    if not str(body.get("title") or "").strip() and matched.get("matched_title"):
                        cur.execute("UPDATE scrm_deal_video SET title=%s WHERE id=%s", (str(matched["matched_title"])[:255], video_id))
                    synced = cur.execute(
                        f"UPDATE `{m['ctable']}` SET `original_url`=%s, `category`='商单', `product_name`=%s WHERE id=%s",
                        (url[:500], _product_display_name(deal_row.get("brand"), deal_row.get("product"), deal_row.get("model")), matched["id"]))
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
    if url and not url.lower().startswith(("http://", "https://")):
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
            cur.execute("SELECT deal_id, platform, url, auto_matched FROM scrm_deal_video WHERE id=%s", (vid,))
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
            if url:
                cur.execute("SELECT id FROM scrm_deal_video WHERE deal_id=%s AND url=%s AND id<>%s LIMIT 1",
                            (row["deal_id"], url[:500], vid))
            if url and cur.fetchone():
                return jsonify({"error": "该视频链接已存在"}), 400
            link_changed = str(row.get("url") or "") != url
            restored_original = bool(row.get("auto_matched") and link_changed)
            if restored_original:
                _restore_content_category(cur, row.get("platform"), row.get("url"))
            cur.execute("""UPDATE scrm_deal_video
                         SET platform=%s, account=%s, url=%s, amount=%s, publish_ts=%s,
                             category=%s, auto_matched=%s WHERE id=%s""",
                        (str(body.get("platform") or "")[:32], str(body.get("account") or "")[:128], url[:500], video_amount, publish_ts,
                         "原创" if restored_original else "商单", 0 if link_changed else int(bool(row.get("auto_matched"))), vid))
            # 保存编辑时同步内容库信息，补齐标题、发布时间和商单分类。
            table = next((t for t in TABLES if MAP[t]["platform"] == str(body.get("platform") or "")), None)
            if table and url:
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
                    cur.execute("SELECT brand,product,model FROM scrm_deal WHERE id=%s", (row["deal_id"],))
                    deal = cur.fetchone() or {}
                    cur.execute(f"UPDATE `{m['ctable']}` SET `original_url`=%s, `category`='商单', `product_name`=%s WHERE id=%s",
                                (url[:500], _product_display_name(deal.get("brand"), deal.get("product"), deal.get("model")), matched["id"]))
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
            cur.execute("SELECT platform, url, auto_matched FROM scrm_deal_video WHERE id=%s", (vid,))
            video = cur.fetchone()
            if video and video.get("auto_matched"):
                _restore_content_category(cur, video.get("platform"), video.get("url"))
            cur.execute("DELETE FROM scrm_deal_video WHERE id=%s", (vid,))
        conn.commit()
    return jsonify({"ok": True})


@bp.get("/api/media")
def api_media():
    q = (request.args.get("q") or "").strip()
    is_brand = (request.args.get("is_brand") or "").strip()
    cooperation_frequency = (request.args.get("cooperation_frequency") or "").strip()
    where_parts, params = [], []
    if q:
        where_parts.append("(name LIKE %s OR contact LIKE %s OR wechat LIKE %s OR party_a LIKE %s)")
        params.extend([f"%{q}%"] * 4)
    if is_brand:
        where_parts.append("is_brand=%s")
        params.append(is_brand)
    if cooperation_frequency:
        where_parts.append("cooperation_frequency=%s")
        params.append(cooperation_frequency)
    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT M.*, (SELECT COUNT(*) FROM scrm_deal D WHERE D.media_company=M.name) deal_count
                FROM scrm_media M{where} ORDER BY M.updated_ts DESC
            """, params)
            rows = cur.fetchall()
            cur.execute("SELECT DISTINCT cooperation_frequency FROM scrm_media WHERE cooperation_frequency IS NOT NULL AND cooperation_frequency<>'' ORDER BY cooperation_frequency")
            frequencies = [r["cooperation_frequency"] for r in cur.fetchall()]
    return jsonify({"rows": rows, "team": TEAM, "frequencies": frequencies})


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
                    UPDATE scrm_media SET name=%s, is_brand=%s, cooperation_frequency=%s,
                          cooperation_count=%s, party_a=%s, contact=%s, wechat=%s,
                          note=%s, updated_ts=%s WHERE id=%s
                """, (name[:128], str(body.get("is_brand") or "")[:32] or None,
                      str(body.get("cooperation_frequency") or "")[:64] or None,
                      int(body["cooperation_count"]) if str(body.get("cooperation_count") or "").strip().isdigit() else None,
                      str(body.get("party_a") or "")[:255],
                      str(body.get("contact") or "")[:64], str(body.get("wechat") or "")[:128],
                      str(body.get("note") or "")[:500], now, int(body["id"])))
            else:
                cur.execute("""
                    INSERT INTO scrm_media (name, is_brand, cooperation_frequency, cooperation_count,
                                            party_a, contact, wechat, source, created_ts, updated_ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',%s,%s)
                """, (name[:128], str(body.get("is_brand") or "")[:32] or None,
                      str(body.get("cooperation_frequency") or "")[:64] or None,
                      int(body["cooperation_count"]) if str(body.get("cooperation_count") or "").strip().isdigit() else None,
                      str(body.get("party_a") or "")[:255],
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
                    SELECT `id`, '' AS platform, `昵称` AS name FROM `UP主管理`
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
                          FROM scrm_skill_binding SB
                          JOIN scrm_skill Sk ON Sk.id=SB.skill_id
                          WHERE SB.up_id=C.`id`) bound_skills
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
            cur.execute("SELECT COUNT(DISTINCT skill_id) c FROM scrm_skill_binding")
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
    """UP主 ↔ Skill 绑定（1对多）：一个 Skill 可绑定多个 UP主，绑定关系存 scrm_skill_binding。
    - body {skill_id, up_id}                  → 建立绑定
    - body {skill_id, up_id, unbind: true}    → 解除该对绑定
    - body {skill_id, up_id: null}            → 解除该 Skill 全部绑定（兼容旧前端）
    scrm_skill.up_id/bind_up 仅作旧视图镜像：首次绑定时写入，全解绑时清空。"""
    body = request.get_json(silent=True) or {}
    try:
        sid = int(body.get("skill_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 skill_id"}), 400
    unbind = bool(body.get("unbind"))
    if body.get("up_id") in (None, "") and not unbind:
        unbind = True  # 旧协议：up_id 为空即解绑
    try:
        up_id = int(body.get("up_id")) if body.get("up_id") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "非法 UP主 id"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            creator = None
            if up_id is not None:
                cur.execute("SELECT `昵称` FROM `UP主管理` WHERE `id`=%s AND `状态`!='retired'", (up_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "UP主不存在或已停用"}), 400
                creator = row["昵称"]
            n = cur.execute("SELECT id FROM scrm_skill WHERE id=%s", (sid,))
            if not n:
                return jsonify({"error": "Skill 不存在"}), 404
            if unbind:
                if up_id is None:
                    cur.execute("DELETE FROM scrm_skill_binding WHERE skill_id=%s", (sid,))
                    cur.execute("UPDATE scrm_skill SET up_id=NULL, bind_up=NULL, updated_ts=%s WHERE id=%s", (now, sid))
                else:
                    cur.execute("DELETE FROM scrm_skill_binding WHERE skill_id=%s AND up_id=%s", (sid, up_id))
                    # 若清掉的正是旧镜像 up_id，则把镜像换成剩余绑定的第一个；没有剩余则清空
                    cur.execute("SELECT up_id FROM scrm_skill_binding WHERE skill_id=%s ORDER BY created_ts, up_id LIMIT 1", (sid,))
                    remain = cur.fetchone()
                    if remain:
                        cur.execute("UPDATE scrm_skill SET up_id=%s, updated_ts=%s WHERE id=%s AND up_id IS NULL", (remain["up_id"], now, sid))
                    else:
                        cur.execute("UPDATE scrm_skill SET up_id=NULL, updated_ts=%s WHERE id=%s", (now, sid))
            else:
                if up_id is None:
                    return jsonify({"error": "缺少 UP主 id"}), 400
                cur.execute("INSERT IGNORE INTO scrm_skill_binding (skill_id, up_id, created_ts) VALUES (%s,%s,%s)", (sid, up_id, now))
                # 旧视图镜像：仅在该 Skill 尚无 up_id 时回填，避免覆盖
                cur.execute("""UPDATE scrm_skill S JOIN (SELECT up_id FROM scrm_skill_binding
                               WHERE skill_id=%s ORDER BY created_ts, up_id LIMIT 1) B
                               SET S.up_id=B.up_id, S.bind_up=%s, S.updated_ts=%s
                               WHERE S.id=%s AND S.up_id IS NULL""", (sid, creator, now, sid))
                cur.execute("UPDATE scrm_skill SET updated_ts=%s WHERE id=%s", (now, sid))
        conn.commit()
    return jsonify({"ok": True, "bound": creator or None, "up_id": up_id, "unbind": unbind})


# ============================================================
# API: 人设 Skill
# ============================================================
@bp.get("/api/skills")
def api_skills():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_skill ORDER BY updated_ts DESC")
            rows = cur.fetchall()
            cur.execute("""SELECT SB.skill_id, SB.up_id, U.`昵称` AS name
                           FROM scrm_skill_binding SB
                           JOIN `UP主管理` U ON U.id=SB.up_id
                           ORDER BY SB.created_ts, SB.up_id""")
            binds = {}
            for b in cur.fetchall():
                item = binds.setdefault(b["skill_id"], {"ids": [], "names": []})
                item["ids"].append(b["up_id"])
                item["names"].append(b["name"])
    for row in rows:
        item = binds.get(row["id"], {"ids": [], "names": []})
        row["bound_up_ids"] = item["ids"]
        row["bound_ups"] = "、".join(item["names"]) or None
    return jsonify({"rows": rows})


@bp.post("/api/skills/save")
def api_skill_save():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Skill 名称必填"}), 400
    persona = body.get("persona")
    if not isinstance(persona, dict) or not isinstance(persona.get("params"), dict):
        return jsonify({"error": "人设结构无效"}), 400
    persona_text = json.dumps(persona, ensure_ascii=False) if persona is not None else None
    if persona_text and len(persona_text) > 2000000:
        return jsonify({"error": "人设内容过长"}), 400
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.get("id"):
                cur.execute("""UPDATE scrm_skill
                               SET name=%s, platform='', style='', example='', persona=%s, updated_ts=%s
                               WHERE id=%s""", (name[:64], persona_text, now, int(body["id"])))
                if not cur.rowcount:
                    return jsonify({"error": "Skill 不存在"}), 404
                saved_id = int(body["id"])
            else:
                cur.execute(
                    "INSERT INTO scrm_skill (name, platform, bind_up, up_id, style, example, status, "
                    "created_ts, updated_ts) VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s)",
                    (name[:64], '', None, None, '', '', now, now))
                saved_id = cur.lastrowid
                cur.execute("UPDATE scrm_skill SET persona=%s WHERE id=%s", (persona_text, saved_id))
        conn.commit()
    return jsonify({"ok": True, "id": saved_id})


def _validate_persona_params(params):
    if not isinstance(params, dict):
        return "请先填写参数化人设"
    for field in ("length", "emotion", "delay_minutes"):
        value = params.get(field)
        if not isinstance(value, dict):
            return f"缺少 {field}"
        try:
            low, high = int(value.get("min")), int(value.get("max"))
        except (TypeError, ValueError):
            return f"{field} 必须是数字区间"
        if low < 0 or high < low:
            return f"{field} 区间无效"
        if field == "emotion" and high > 10:
            return "emotion 必须在 0-10 之间"
    try:
        rate = float(params.get("reply_rate_default"))
    except (TypeError, ValueError):
        return "reply_rate_default 必须是数字"
    if not 0 <= rate <= 1:
        return "reply_rate_default 必须在 0-1 之间"
    if not str(params.get("address") or "").strip():
        return "缺少观众称呼"
    if not isinstance(params.get("punctuation"), dict):
        return "缺少标点规则"
    if not isinstance(params.get("never"), list) or not params["never"]:
        return "至少填写一条绝不做什么"
    # 可选：字数漂移 {rate, min, max}，rate 概率允许回复超出常规句长、落入漂移区间
    drift = params.get("length_drift")
    if drift is not None:
        if not isinstance(drift, dict):
            return "length_drift 必须是对象"
        try:
            d_rate = float(drift.get("rate", 0))
            d_min, d_max = int(drift.get("min")), int(drift.get("max"))
        except (TypeError, ValueError):
            return "length_drift.rate/min/max 必须是数字"
        if not 0 <= d_rate <= 1 or d_min < 0 or d_max < d_min:
            return "length_drift 配置无效(rate 0-1, min<=max)"
    return None


@bp.post("/api/skills/publish")
def api_skill_publish():
    body = request.get_json(silent=True) or {}
    try:
        sid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "非法 Skill id"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrm_skill WHERE id=%s", (sid,))
            skill = cur.fetchone()
            if not skill:
                return jsonify({"error": "Skill 不存在"}), 404
            cur.execute("SELECT up_id FROM scrm_skill_binding WHERE skill_id=%s ORDER BY created_ts, up_id", (sid,))
            targets = [r["up_id"] for r in cur.fetchall()]
            if not targets and skill.get("up_id"):
                targets = [skill["up_id"]]
            if not targets:
                return jsonify({"error": "请先绑定至少一个 UP主后再发布"}), 400
            try:
                persona = json.loads(skill.get("persona") or "{}")
            except (TypeError, ValueError):
                return jsonify({"error": "人设草稿格式无效"}), 400
            params = persona.get("params")
            error = _validate_persona_params(params)
            if error:
                return jsonify({"error": error}), 400
            versions = []
            for up_id in targets:
                # 1对多：给每个绑定的 UP 主各写一份 persona_pack，版本号按各自历史递增
                cur.execute("SELECT version FROM persona_pack WHERE up_id=%s", (up_id,))
                prior = cur.fetchone()
                number = int(str(prior["version"])[1:]) + 1 if prior and str(prior["version"]).startswith("v") and str(prior["version"])[1:].isdigit() else 1
                version = f"v{number}"
                versions.append(version)
            fingerprint = {
                "length": params["length"], "emotion": params["emotion"],
                "filler_whitelist": params.get("filler_whitelist", []),
                "punctuation": params["punctuation"], "never": params["never"],
                "length_drift": params.get("length_drift"),
            }
            now = int(time.time())
            for idx, up_id in enumerate(targets):
                version = versions[idx]
                cur.execute("""INSERT INTO persona_pack (up_id,params,fingerprint,few_shot_slots,version,updated_ts)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON DUPLICATE KEY UPDATE params=VALUES(params),fingerprint=VALUES(fingerprint),
                                   few_shot_slots=VALUES(few_shot_slots),version=VALUES(version),updated_ts=VALUES(updated_ts)""",
                            (up_id, json.dumps(params, ensure_ascii=False), json.dumps(fingerprint, ensure_ascii=False),
                             min(3, max(0, int(persona.get("few_shot_slots", 3)))), version, now))
                cur.execute("UPDATE `UP主管理` SET `Skill版本`=%s,`Skill更新时间`=%s,updated_ts=%s WHERE id=%s",
                            (version, now, now, up_id))
            cur.execute("UPDATE scrm_skill SET status='published',updated_ts=%s WHERE id=%s", (now, sid))
        conn.commit()
    return jsonify({"ok": True, "version": versions[0] if versions else None, "published_ups": len(versions)})


@bp.post("/api/skills/test")
def api_skill_test():
    """通过独立 reply-service 预览已发布 Skill 的回复。"""
    body = request.get_json(silent=True) or {}
    content = str(body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "请输入要测试的评论"}), 400
    try:
        sid = int(body.get("skill_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "请选择有效 Skill"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM scrm_skill WHERE id=%s", (sid,))
            skill = cur.fetchone()
            if skill:
                cur.execute("SELECT up_id FROM scrm_skill_binding WHERE skill_id=%s ORDER BY created_ts, up_id LIMIT 1", (sid,))
                row = cur.fetchone()
                test_up_id = row["up_id"] if row else None
    if not skill or not test_up_id:
        return jsonify({"error": "Skill 尚未绑定有效 UP主"}), 400
    if skill.get("status") != "published":
        return jsonify({"error": "请先发布 Skill，再进行运行态测试"}), 400
    result = generate_reply({"comment_id": f"skill-test:{sid}", "comment": content[:2000],
                             "up_id": test_up_id, "platform_account_id": None,
                             "src_table": "skill_test", "src_pk": sid}, "preview")
    if not result.get("available"):
        return jsonify({"ok": False, "error": result.get("error", "回复服务不可用")}), 502
    if result.get("action") != "reply":
        return jsonify({"ok": False, "error": result.get("human_reason") or "该评论不适合自动回复"}), 422
    return jsonify({"ok": True, "reply": result.get("reply", "")[:300], "skill_version": result.get("skill_version")})


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
            "A.`主页链接` AS homepage_url, "
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
        SELECT A.platform, A.nickname, A.creator_id, A.creator_name, A.account_id, A.fans, A.homepage_url,
               A.bit_window, A.status, A.note, A.cookie_status, A.cookie_checked_at, A.cookie_check_message,
               COUNT(DISTINCT V.content_id) video_count,
               MAX(V.last_update_ts) latest_update_ts,
               SUM(CASE WHEN V.last_update_ts >= UNIX_TIMESTAMP(DATE_FORMAT(CURRENT_DATE, '%%Y-%%m-01')) THEN 1 ELSE 0 END) month_video_count
        FROM ({_account_frag()}) A
        LEFT JOIN {_content_union()} ON V.platform = A.platform AND V.upname = A.nickname
        GROUP BY A.platform, A.nickname, A.creator_id, A.creator_name, A.account_id,
                 A.fans, A.homepage_url, A.bit_window, A.status, A.note, A.cookie_status, A.cookie_checked_at, A.cookie_check_message
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
        homepage_url=str(body.get("homepage_url") or "").strip()[:1000],
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
                        `ID`=%s, `主页链接`=%s, `粉丝`=%s, `Bit窗口`=%s, `状态`=%s, `备注`=%s
                    WHERE `平台`=%s AND `昵称`=%s
                """, (platform, nickname, creator_id, vals["account_id"], vals["homepage_url"], vals["fans"],
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
                        (`平台`,`昵称`,`UP主ID`,`ID`,`主页链接`,`粉丝`,`Bit窗口`,`状态`,`备注`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (platform, nickname, creator_id, vals["account_id"], vals["homepage_url"], vals["fans"],
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

# 评论分析接口已移除；人设参数仅通过 /api/skills/save 维护。

