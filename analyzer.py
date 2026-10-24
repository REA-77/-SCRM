# -*- coding: utf-8 -*-
"""评论决策守护进程
- 事件驱动式快扫:每 ANALYZE_INTERVAL 秒轮询所有评论表 analyzed=0 的记录(小批量)
- 每 ANALYZE_FULL_SWEEP 秒做一次全量补扫,清完快扫批次外的遗留
- 自动分析只处理最近 ANALYZE_MAX_AGE_DAYS 天内的评论(手动分析不受限)
- 由独立 reply-service 一次完成分析、路由与回复决策
- 回写 analyzed/negative_alert/purchase_intent/comment_type/reply_suggestion
"""
import json
import time
import logging
import requests
from pymysql.err import OperationalError

from config import (
    ANALYZE_INTERVAL, ANALYZE_BATCH, ANALYZE_FULL_SWEEP, ANALYZE_MAX_AGE_DAYS,
    COMMENT_TABLES, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID, FEISHU_ALERT_ENABLED,
)
from db import get_conn, mark_self_replies_handled
from reply_context import resolve_comment_context
from reply_service_client import evaluate_comment

logger = logging.getLogger("analyzer")

# 高级负向类型:识别到即强制标注负面预警/高级负面，并立即发送飞书提醒。
# 产品差评仅需人工审核，属于常规负面反馈，不升级为高级负面。
ADVANCED_NEGATIVE_TYPES = {
    "攻击引战", "无关灌水", "恰饭质疑", "专业质疑",
    "恶意质疑", "导购广告", "竞品引流",
}

ANALYSIS_CLAIM_TIMEOUT_SECONDS = 300

# 各评论表的业务时间列(create_time 多为秒;add_ts 部分平台为毫秒,过滤时统一归一化)
_TABLE_TIME_COL = {
    "bilibili_video_comment": "create_time",
    "channels_video_comment": "create_time",
    "douyin_aweme_comment": "create_time",
    "kuaishou_video_comment": "create_time",
    "weibo_note_comment": "create_time",
    "xhs_note_comment": "create_time",
    "zhihu_comment": "add_ts",
    "tieba_comment": "add_ts",
    "reddit_comment": "add_ts",
}

class _Status:
    def __init__(self):
        self.running = False
        self.processing = False
        self.last_table = ""
        self.last_error = ""
        self.session_analyzed = 0
        self.session_failed = 0
        self.started_at = None


status = _Status()


_NO_NICKNAME_TABLES = set()  # 记录不含 nickname 列的评论表


def get_unanalyzed(table, limit, after_id=0, through_id=None):
    cols = "`id`, `content`" if table in _NO_NICKNAME_TABLES else "`id`, `content`, `nickname`"
    sql = f"SELECT {cols} FROM `{table}` WHERE `analyzed`=0 AND IFNULL(`handled`,0)=0 AND `id`>%s"
    params = [after_id]
    if through_id is not None:
        sql += " AND `id`<=%s"
        params.append(through_id)
    # 自动分析默认只处理最近 N 天内的评论(0=不限制);时间未知(0/NULL)的不跳过
    if ANALYZE_MAX_AGE_DAYS and ANALYZE_MAX_AGE_DAYS > 0:
        col = _TABLE_TIME_COL.get(table, "create_time")
        cutoff = int(time.time()) - int(ANALYZE_MAX_AGE_DAYS) * 86400
        sql += (f" AND (IFNULL(`{col}`,0)=0 OR (CASE WHEN `{col}` > 100000000000"
                f" THEN ROUND(`{col}`/1000) ELSE `{col}` END) >= %s)")
        params.append(cutoff)
    sql += " ORDER BY `id` LIMIT %s"
    params.append(min(500, max(1, int(limit or 100))))
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return cur.fetchall()
    except OperationalError as exc:
        # 部分评论表无 nickname 列,降级后重试一次
        if exc.args[0] == 1054 and 'nickname' in str(exc) and table not in _NO_NICKNAME_TABLES:
            _NO_NICKNAME_TABLES.add(table)
            return get_unanalyzed(table, limit, after_id, through_id)
        raise


def iter_unanalyzed(table, batch):
    """Full sweeps use bounded ID pages; committed analyzed flags survive restarts."""
    through_id = None
    if batch == 0:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(id) AS last_id FROM `{table}`")
                through_id = cur.fetchone()['last_id']
        if through_id is None:
            return
    after_id = 0
    while True:
        rows = get_unanalyzed(table, batch or 100, after_id, through_id)
        if not rows:
            return
        yield from rows
        after_id = rows[-1]['id']
        if batch != 0:
            return
        del rows


# ---------- 飞书自建应用发送(负面预警)----------
_FEISHU_TOKEN = {"value": "", "expire": 0}  # tenant_access_token 缓存


def _feishu_token():
    """获取 tenant_access_token,本地缓存至过期前 60 秒"""
    now = time.time()
    if _FEISHU_TOKEN["value"] and now < _FEISHU_TOKEN["expire"]:
        return _FEISHU_TOKEN["value"]
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    _FEISHU_TOKEN["value"] = data["tenant_access_token"]
    _FEISHU_TOKEN["expire"] = now + int(data.get("expire", 7200)) - 60
    return _FEISHU_TOKEN["value"]


def send_feishu_high_alert(alert):
    """高级负向评论识别到后立即单条推送飞书提醒"""
    if not FEISHU_ALERT_ENABLED:
        return
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_CHAT_ID):
        return
    # Alert callers include both batch analysis and manual regeneration.  Read
    # the persisted body again so a mismatched context-field name can never
    # turn a valid alert into an empty Feishu message.
    comment_text = _alert_comment_text(alert)
    tag = "🚨🚨 高级负向评论预警"
    text = "\n".join([
        tag,
        f"类型:{alert.get('type') or '未知'}",
        f"【{alert['table']}#{alert['id']}】{alert['nickname'] or '匿名'}",
        f"评论:{comment_text[:150]}",
    ])
    logger.info("发送高级负向飞书预警: %s#%s，正文 %s 字", alert["table"], alert["id"], len(comment_text))
    try:
        data = _post_feishu_text(text)
        if data.get("code") != 0:
            logger.warning("高级负向飞书提醒发送失败: %s", data)
        else:
            logger.info("高级负向飞书提醒已发送: %s#%s", alert["table"], alert["id"])
    except Exception as e:
        logger.warning("高级负向飞书提醒发送异常: %s", e)


def _alert_comment_text(alert):
    """Return the authoritative stored comment body for an alert when possible."""
    table = alert.get("table")
    rid = alert.get("id")
    if table in COMMENT_TABLES:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT CAST(content AS CHAR) AS content FROM `{table}` WHERE id=%s", (int(rid),))
                    row = cur.fetchone() or {}
            stored = str(row.get("content") or "").strip()
            if stored:
                return stored
        except Exception as exc:
            logger.warning("读取飞书预警评论正文失败: %s", exc)
    return str(alert.get("content") or "").strip()


def _post_feishu_text(text):
    token = _feishu_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=10,
    )
    return r.json()


def send_feishu_alert(alerts):
    """每轮结束后汇总负面评论,发一条(过长时分多条)消息到目标群聊
    alerts: [{"table","id","nickname","content","reply"}, ...]"""
    if not FEISHU_ALERT_ENABLED:
        return
    if not alerts or not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_CHAT_ID):
        return
    # 分表统计
    counts = {}
    for a in alerts:
        counts[a["table"]] = counts.get(a["table"], 0) + 1
    summary = "、".join(f"{t} {n} 条" for t, n in sorted(counts.items(), key=lambda x: -x[1]))
    header = f"🚨 负面评论小时汇总(本轮新增 {len(alerts)} 条)\n{summary}"
    # 明细按 30 条/片 或 ~9000 字分片,避免超出飞书单条消息长度限制
    MAX_ALERTS_PER_MSG = 30
    MAX_CHARS = 9000
    chunks, cur, cur_len = [], [], len(header)
    for a in alerts:
        block = "\n".join([
            f"【{a['table']}#{a['id']}】{a['nickname'] or '匿名'}",
            f"评论:{a['content'][:100]}",
            f"建议回复:{a['reply'][:100]}",
        ])
        if cur and (len(cur) >= MAX_ALERTS_PER_MSG or cur_len + len(block) > MAX_CHARS):
            chunks.append(cur)
            cur, cur_len = [], len(header)
        cur.append(block)
        cur_len += len(block)
    if cur:
        chunks.append(cur)
    total = len(chunks)
    try:
        for idx, blocks in enumerate(chunks, 1):
            title = header if total == 1 else f"{header}\n——明细 {idx}/{total}——"
            data = _post_feishu_text(f"{title}\n" + "\n".join(blocks))
            if data.get("code") != 0:
                logger.warning("飞书预警发送失败(片 %s/%s): %s", idx, total, data)
        logger.info("飞书预警已发送,本轮共 %s 条负面评论,分 %s 条消息", len(alerts), total)
    except Exception as e:
        logger.warning("飞书预警发送异常: %s", e)


def mark_analyzed(table, rid, is_negative, has_intent, reply, comment_type=None, failed=False, high_negative=None, human_review=False):
    if failed:
        sql = f"UPDATE `{table}` SET `analyzed`=1, `analysis_claimed_ts`=NULL, `negative_alert`=0, `purchase_intent`=0, `comment_type`=NULL, `human_review`=0, `reply_suggestion`=IF(IFNULL(`reply_manual_override`,0)=1, `reply_suggestion`, %s) WHERE `id`=%s"
        params = ("⚠ 分析失败,可重置 analyzed=0 后重试", rid)
    else:
        neg = 1 if is_negative else 0
        intent = 1 if has_intent else 0
        high = "" if high_negative is None else ", `high_negative`=%s"
        sql = (f"UPDATE `{table}` SET `analyzed`=1, `analysis_claimed_ts`=NULL, `negative_alert`=%s, `purchase_intent`=%s, "
               f"`comment_type`=%s, `human_review`=%s{high}, `reply_suggestion`=IF(IFNULL(`reply_manual_override`,0)=1, `reply_suggestion`, %s) WHERE `id`=%s")
        if high:
            params = (neg, intent, str(comment_type or "")[:64] or None, 1 if human_review else 0,
                      1 if high_negative else 0, reply, rid)
        else:
            params = (neg, intent, str(comment_type or "")[:64] or None, 1 if human_review else 0, reply, rid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def claim_comment(table, rid, *, now=None):
    """原子领取一条待分析评论，避免多个服务实例重复分析、重复告警。"""
    claimed_at = int(time.time() if now is None else now)
    expired_at = claimed_at - ANALYSIS_CLAIM_TIMEOUT_SECONDS
    with get_conn() as conn:
        with conn.cursor() as cur:
            affected = cur.execute(
                f"UPDATE `{table}` SET `analysis_claimed_ts`=%s "
                f"WHERE `id`=%s AND `analyzed`=0 AND IFNULL(`handled`,0)=0 "
                f"AND (`analysis_claimed_ts` IS NULL OR `analysis_claimed_ts`<%s)",
                (claimed_at, rid, expired_at),
            )
        conn.commit()
    return affected == 1


def scan_all(batch=None):
    """扫描所有评论表;0 表示分批补扫至本轮开始时的最大 ID。"""
    if batch is None:
        batch = ANALYZE_BATCH
    processed = 0
    alerts = []
    for table in COMMENT_TABLES:
        try:
            rows = iter_unanalyzed(table, batch)
            # Prime the generator inside the query error handler.
            first = next(rows, None)
        except Exception as e:
            logger.warning("查询 %s 未分析评论失败: %s", table, e)
            continue
        if first is None:
            continue
        status.processing = True
        from itertools import chain
        for row in chain((first,), rows):
            try:
                if not claim_comment(table, row["id"]):
                    continue
            except Exception as exc:
                logger.warning("领取 %s#%s 分析任务失败: %s", table, row["id"], exc)
                continue
            status.last_table = f"{table}#{row['id']}"
            content = (row.get("content") or "")[:500]
            if not content.strip():
                mark_analyzed(table, row["id"], False, False, "(空评论)")
                status.session_analyzed += 1
                processed += 1
                continue
            context = resolve_comment_context(table, row["id"])
            if not context:
                mark_analyzed(table, row["id"], False, False, "", failed=True)
                status.session_failed += 1
                processed += 1
                continue
            generated = evaluate_comment(context, "batch")
            if not generated.get("available"):
                mark_analyzed(table, row["id"], False, False, "", failed=True)
                status.session_failed += 1
                status.last_error = f"{time.strftime('%H:%M:%S')} {generated.get('error', '回复服务不可用')}"[:200]
            else:
                analysis = generated["analysis"]
                is_neg = analysis["is_negative"]
                has_intent = analysis["has_purchase_intent"]
                ctype = analysis.get("comment_type") or ""
                # 高级负向类型:无条件标注负向 + 高级负向,并立即推送飞书
                high_neg = ctype in ADVANCED_NEGATIVE_TYPES
                if high_neg:
                    is_neg = True
                reply = (generated.get("reply") or "")[:300] if generated.get("action") == "reply" else ""
                mark_analyzed(table, row["id"], is_neg, has_intent, reply, ctype,
                              high_negative=high_neg, human_review=generated.get("action") == "human_review")
                status.session_analyzed += 1
                logger.info("分析 %s#%s neg=%s high=%s type=%s intent=%s reply=%s",
                            table, row["id"], is_neg, high_neg, ctype, has_intent, reply[:40])
                if high_neg:
                    send_feishu_high_alert({
                        "table": table,
                        "id": row["id"],
                        "nickname": (row.get("nickname") or "").strip(),
                        "content": content,
                        "type": ctype,
                    })
                elif is_neg:
                    alerts.append({
                        "table": table,
                        "id": row["id"],
                        "nickname": (row.get("nickname") or "").strip(),
                        "content": content,
                        "reply": reply,
                    })
                    if len(alerts) >= 30:
                        send_feishu_alert(alerts)
                        alerts.clear()
            processed += 1
        status.processing = False
    send_feishu_alert(alerts)
    return processed


def analyzer_loop():
    """守护循环:快扫(事件驱动式,小批量) + 定时全量补扫"""
    status.running = True
    status.started_at = time.time()
    logger.info("评论分析守护进程启动,快扫间隔 %ss/每表 %s 条,全量补扫每 %ss",
                ANALYZE_INTERVAL, ANALYZE_BATCH, ANALYZE_FULL_SWEEP)
    next_full_sweep = time.time() + ANALYZE_FULL_SWEEP
    while True:
        try:
            handled_self_replies = mark_self_replies_handled()
            if handled_self_replies:
                logger.info("已自动标记 %s 条本人回复为已处理", handled_self_replies)
            if time.time() >= next_full_sweep:
                scan_all(batch=0)  # 全量补扫,一轮清完所有遗留
                next_full_sweep = time.time() + ANALYZE_FULL_SWEEP
            else:
                scan_all()
        except Exception as e:
            logger.exception("守护循环异常: %s", e)
            status.last_error = str(e)[:200]
        finally:
            status.processing = False
        time.sleep(ANALYZE_INTERVAL)


def get_status_dict():
    """供 API 返回运行状态 + 各表统计"""
    stats = {}
    total_a = total_p = total_n = total_i = total_h = 0
    for t in COMMENT_TABLES:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `analyzed`=1")
                    a = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `analyzed`=0 AND IFNULL(`handled`,0)=0")
                    p = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `negative_alert`=1")
                    n = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `purchase_intent`=1")
                    i = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `handled`=1")
                    h = cur.fetchone()["c"]
            stats[t] = {"analyzed": a, "pending": p, "negative": n, "intent": i, "handled": h}
            total_a += a; total_p += p; total_n += n; total_i += i; total_h += h
        except Exception as e:
            stats[t] = {"analyzed": 0, "pending": 0, "negative": 0, "intent": 0, "handled": 0, "error": str(e)[:100]}
    return {
        "running": status.running,
        "processing": status.processing,
        "last_table": status.last_table,
        "last_error": status.last_error,
        "session_analyzed": status.session_analyzed,
        "session_failed": status.session_failed,
        "total_analyzed": total_a,
        "total_pending": total_p,
        "total_negative": total_n,
        "total_purchase": total_i,
        "total_handled": total_h,
        "tables": stats,
    }
