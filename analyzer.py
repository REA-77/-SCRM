# -*- coding: utf-8 -*-
"""评论 AI 分析守护进程
- 后台轮询所有评论表 analyzed=0 的记录
- 调 DeepSeek 判断负面 + 生成 10-50 字回复
- 回写 analyzed/negative_alert/reply_suggestion
"""
import json
import time
import logging
import requests

from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    ANALYZE_INTERVAL, ANALYZE_BATCH, ANALYZE_MAX_TOKENS, ANALYZE_TEMPERATURE,
    COMMENT_TABLES,
)
from db import get_conn

logger = logging.getLogger("analyzer")

SYSTEM_PROMPT = """你是一位互联网冲浪选手,同时具备家电测评专家的专业性,语气温和礼貌。
根据用户给出的评论,做三件事:
1. 判断 is_negative:评论含抱怨、批评、不满、质量问题、负面情绪则为 true,否则 false。
2. 判断 has_purchase_intent:评论表达购买意向(如求推荐、想买、求链接、比价、咨询选购、问型号配置等)则为 true,否则 false。
3. 生成 10-50 字回复 reply:
   - 若 has_purchase_intent=true:给出引导性回复,温和专业地推荐合适家电型号/品类、给出选购建议、促进购买决策,体现家电测评专家的专业性,可适当带网络用语。
   - 否则:常规回复,家电测评专家口吻 + 温和礼貌 + 互联网冲浪选手接地气,可适当用网络用语但不失专业。
严格只返回 JSON,格式:{"is_negative": true/false, "has_purchase_intent": true/false, "reply": "回复内容"}"""


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


def call_deepseek(content):
    """调 DeepSeek 分析单条评论,返回 (is_negative:bool, reply:str) 或 None"""
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"评论:{content}"},
        ],
        "max_tokens": ANALYZE_MAX_TOKENS,
        "temperature": ANALYZE_TEMPERATURE,
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        # 容错:提取首个 JSON 对象(模型可能包裹 ```json)
        if "{" in text:
            text = text[text.find("{"):text.rfind("}") + 1]
        obj = json.loads(text)
        is_neg = bool(obj.get("is_negative", False))
        has_intent = bool(obj.get("has_purchase_intent", False))
        reply = (obj.get("reply") or "").strip()
        if not reply:
            return None
        return is_neg, has_intent, reply[:300]
    except Exception as e:
        logger.warning("DeepSeek 调用失败: %s", e)
        status.last_error = f"{time.strftime('%H:%M:%S')} {e}"[:200]
        return None


def get_unanalyzed(table, limit):
    sql = f"SELECT `id`, `content` FROM `{table}` WHERE `analyzed`=0 ORDER BY `id` LIMIT %s"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()


def mark_analyzed(table, rid, is_negative, has_intent, reply, failed=False):
    if failed:
        sql = f"UPDATE `{table}` SET `analyzed`=1, `negative_alert`=0, `purchase_intent`=0, `reply_suggestion`=%s WHERE `id`=%s"
        params = ("⚠ 分析失败,可重置 analyzed=0 后重试", rid)
    else:
        neg = 1 if is_negative else 0
        intent = 1 if has_intent else 0
        sql = f"UPDATE `{table}` SET `analyzed`=1, `negative_alert`=%s, `purchase_intent`=%s, `reply_suggestion`=%s WHERE `id`=%s"
        params = (neg, intent, reply, rid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def scan_all():
    """扫描所有评论表一轮,返回本轮处理条数"""
    processed = 0
    for table in COMMENT_TABLES:
        try:
            rows = get_unanalyzed(table, ANALYZE_BATCH)
        except Exception as e:
            logger.warning("查询 %s 未分析评论失败: %s", table, e)
            continue
        if not rows:
            continue
        status.processing = True
        for row in rows:
            status.last_table = f"{table}#{row['id']}"
            content = (row.get("content") or "")[:500]
            if not content.strip():
                mark_analyzed(table, row["id"], False, False, "(空评论)")
                status.session_analyzed += 1
                processed += 1
                continue
            res = call_deepseek(content)
            if res is None:
                mark_analyzed(table, row["id"], False, False, "", failed=True)
                status.session_failed += 1
            else:
                is_neg, has_intent, reply = res
                mark_analyzed(table, row["id"], is_neg, has_intent, reply)
                status.session_analyzed += 1
                logger.info("分析 %s#%s neg=%s intent=%s reply=%s", table, row["id"], is_neg, has_intent, reply[:40])
            processed += 1
        status.processing = False
    return processed


def analyzer_loop():
    """守护循环"""
    status.running = True
    status.started_at = time.time()
    logger.info("评论分析守护进程启动,间隔 %ss,每轮每表 %s 条", ANALYZE_INTERVAL, ANALYZE_BATCH)
    while True:
        try:
            scan_all()
        except Exception as e:
            logger.exception("守护循环异常: %s", e)
            status.last_error = str(e)[:200]
        time.sleep(ANALYZE_INTERVAL)


def get_status_dict():
    """供 API 返回运行状态 + 各表统计"""
    stats = {}
    total_a = total_p = total_n = total_i = 0
    for t in COMMENT_TABLES:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `analyzed`=1")
                    a = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `analyzed`=0")
                    p = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `negative_alert`=1")
                    n = cur.fetchone()["c"]
                    cur.execute(f"SELECT COUNT(*) c FROM `{t}` WHERE `purchase_intent`=1")
                    i = cur.fetchone()["c"]
            stats[t] = {"analyzed": a, "pending": p, "negative": n, "intent": i}
            total_a += a; total_p += p; total_n += n; total_i += i
        except Exception as e:
            stats[t] = {"analyzed": 0, "pending": 0, "negative": 0, "intent": 0, "error": str(e)[:100]}
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
        "tables": stats,
    }
