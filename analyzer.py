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
    COMMENT_TABLES, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID,
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


_NO_NICKNAME_TABLES = set()  # 记录不含 nickname 列的评论表


def get_unanalyzed(table, limit):
    cols = "`id`, `content`" if table in _NO_NICKNAME_TABLES else "`id`, `content`, `nickname`"
    sql = f"SELECT {cols} FROM `{table}` WHERE `analyzed`=0 AND IFNULL(`handled`,0)=0 ORDER BY `id`"
    if limit and limit > 0:
        sql += " LIMIT %s"
        params = (limit,)
    else:
        params = ()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception:
        # 部分评论表无 nickname 列,降级后重试一次
        if table not in _NO_NICKNAME_TABLES:
            _NO_NICKNAME_TABLES.add(table)
            return get_unanalyzed(table, limit)
        raise


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
    alerts = []
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
                if is_neg:
                    alerts.append({
                        "table": table,
                        "id": row["id"],
                        "nickname": (row.get("nickname") or "").strip(),
                        "content": content,
                        "reply": reply,
                    })
            processed += 1
        status.processing = False
    send_feishu_alert(alerts)
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
