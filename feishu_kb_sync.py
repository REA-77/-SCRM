# -*- coding: utf-8 -*-
"""知识库 9 张底表 → 飞书多维表格 同步创建
- 解析 wiki 节点 URL 中的 node token, 换取底层 bitable app_token
- 按 MySQL information_schema 的列定义在多维表格创建同名数据表
- 把 MySQL 现有行同步为多维表格记录(增量: 已存在同名表则跳过建表, 记录按主键去重)

用法:
    python feishu_kb_sync.py                      # 全量: 建表 + 同步数据
    python feishu_kb_sync.py --tables-only        # 只建表
    python feishu_kb_sync.py --tables model_facts # 只同步指定表
"""
import argparse
import json
import logging
import time

import requests

from config import FEISHU_APP_ID, FEISHU_APP_SECRET
from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("feishu_kb")

FS_BASE = "https://open.feishu.cn/open-apis"
WIKI_NODE = "HR0lw9OJbiRax7k29zScm3cSnwh"  # 知识库 wiki 节点

KB_TABLES = ["account_registry", "persona_pack", "routing_matrix", "guardrails",
             "model_facts", "glossary", "misconception", "banned_phrase", "reply_log"]


def fs_token(session):
    r = session.post(f"{FS_BASE}/auth/v3/tenant_access_token/internal",
                     json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                     timeout=20)
    r.raise_for_status()
    auth = r.json()
    if auth.get("code") != 0 or not auth.get("tenant_access_token"):
        raise RuntimeError(f"获取 tenant_access_token 失败: {auth.get('msg')}")
    return auth["tenant_access_token"]


def resolve_wiki_app_token(session, token, headers):
    """wiki 节点 token → 底层文档 app_token"""
    r = session.get(f"{FS_BASE}/wiki/v2/spaces/get_node",
                    headers=headers, params={"token": token}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"解析 wiki 节点失败: {body.get('msg')}(需在飞书后台给应用开 wiki 读权限)")
    node = (body.get("data") or {}).get("node") or {}
    if node.get("obj_type") != "bitable":
        raise RuntimeError(f"该 wiki 节点不是多维表格: obj_type={node.get('obj_type')}")
    return node["obj_token"]


# ---------- 类型映射: MySQL 列 → 多维表格字段 ----------
def map_field(col):
    """col: information_schema 行。返回 bitable field dict; None 表示不建(自增列由飞书自带行号代替)"""
    name, dtype, ctype = col["COLUMN_NAME"], col["DATA_TYPE"], col["COLUMN_TYPE"]
    if "auto_increment" in (col["EXTRA"] or ""):
        return None
    if dtype == "enum":
        opts = [v.strip("'") for v in ctype[5:-1].split("','")]
        return {"field_name": name, "type": 3, "property": {"options": [{"name": o} for o in opts]}}
    if dtype == "datetime" or dtype == "timestamp":
        return {"field_name": name, "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}}
    if dtype == "date":
        return {"field_name": name, "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}}
    if dtype == "json":
        return {"field_name": name, "type": 1}  # JSON 以文本存储, 保证可逆
    if dtype in ("int", "bigint", "smallint", "tinyint"):
        if ctype == "tinyint(1)":
            return {"field_name": name, "type": 7}  # 复选框
        return {"field_name": name, "type": 2}
    if dtype in ("decimal", "float", "double"):
        return {"field_name": name, "type": 2}
    if dtype in ("text", "mediumtext", "longtext"):
        return {"field_name": name, "type": 1}
    return {"field_name": name, "type": 1}  # varchar/char → 文本


def list_bitable_tables(session, headers, app_token):
    r = session.get(f"{FS_BASE}/bitable/v1/apps/{app_token}/tables",
                    headers=headers, params={"page_size": 100}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"列出多维表格数据表失败: {body.get('msg')}")
    return {t["name"]: t["table_id"] for t in (body.get("data") or {}).get("items") or []}


ZH_TITLE = {
    "account_registry": "账号注册表", "persona_pack": "人设参数包",
    "routing_matrix": "路由矩阵", "guardrails": "硬红线",
    "model_facts": "型号事实表", "glossary": "术语库",
    "misconception": "认知纠正库", "banned_phrase": "禁用句",
    "reply_log": "回复日志",
}

# 列注释缺失时的兜底中文
ZH_FALLBACK = {
    "id": "编号", "created_at": "创建时间", "created_ts": "创建时间",
    "updated_ts": "更新时间", "version": "版本", "model": "模型",
    "editor": "处理人", "source": "来源",
    # 建表时未写注释的列
    "account_id": "账号ID", "rule_id": "规则ID", "note": "备注",
    "enabled": "是否启用", "account_level": "账号层级", "brand": "品牌",
    "launch_date": "上市时间", "confidence": "置信度", "state": "状态",
    "term": "术语", "alias": "别名", "unit": "单位", "define": "定义",
    "updated_at": "更新日期", "topic_id": "主题ID", "truth": "真相",
    "condition": "适用条件", "exception": "例外", "verified_at": "核实日期",
    "phrase": "禁用句", "scope": "范围", "reply_id": "回复ID",
    "src_pk": "评论行ID", "prompt_version": "提示词版本", "action": "动作",
    "final_text": "最终文本", "latency_ms": "耗时ms", "level": "层级",
    "status": "状态", "read": "怎么读", "trap": "常见误读",
    "standard_version": "标准版本", "display_name": "对外账号名",
    "platform_uids": "平台UID", "owner": "主笔", "categories": "可覆盖品类",
    "reply_quota": "日回复上限", "few_shot_slots": "语料条数",
    "params": "人设参数", "fingerprint": "账号指纹",
    "comment_type": "评论类型", "reply_rate": "回复率",
    "target_len": "目标长度", "human_required": "强制人工",
    "extra_rule": "附加规则", "gtype": "红线类型",
    "trigger_pattern": "触发模式", "rewrite_to": "改写文本",
    "model_id": "型号ID", "model_name": "完整型号",
    "aliases": "别称", "category": "品类", "price_band": "价位段",
    "specs": "参数三元组", "known_issues": "已知问题",
    "tested_by": "实测记录", "expires_at": "有效期至",
    "trigger_words": "触发词", "trigger_synonyms": "触发词同义",
    "priority": "优先级", "tpl_by_level": "分层模板",
    "l_level_ok": "可用层级", "needs_human": "强制人工",
    "account_visibility": "可见账号", "src_table": "评论表",
    "comment_id": "平台评论ID", "type_conf": "分类置信度",
    "retrieved": "命中条目", "draft": "模型草稿", "detector": "四闸结果",
    "rejection_code": "打回码",
}

_HAS_ZH = None


def _has_zh(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def resolve_table_id(existing, table):
    """按英文名或 '中文 english' 格式匹配已建数据表"""
    if table in existing:
        return existing[table]
    for name, tid in existing.items():
        if name.endswith(f" {table}"):
            return tid
    return None


def _request(session, method, url, headers, retries=3, **kw):
    """带重试的 API 调用(飞书偶发读超时)"""
    kw.setdefault("timeout", 30)
    for i in range(retries):
        try:
            r = session.request(method, url, headers=headers, **kw)
            return r
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def rename_to_zh(session, headers, app_token, cur):
    """表名 → '中文 english';字段名 → '中文 english'(中文取 MySQL 列注释)"""
    existing = list_bitable_tables(session, headers, app_token)
    for table in KB_TABLES:
        tid = resolve_table_id(existing, table)
        if not tid:
            logger.warning("[%s] 飞书中未找到, 跳过", table)
            continue
        # 表名加中文
        cur.execute(
            "SELECT TABLE_COMMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        tcomment = (cur.fetchone() or {}).get("TABLE_COMMENT") or ""
        zh_table = (tcomment.split("(")[0].strip() or ZH_TITLE.get(table) or table)
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        zh_of = {r["COLUMN_NAME"]: (r["COLUMN_COMMENT"] or "").split("(")[0].split(",")[0].strip()
                 for r in cur.fetchall()}

        # 当前实际表名(可能已被改过)
        cur_name = next((n for n, i in existing.items() if i == tid), table)
        if not _has_zh(cur_name):
            new_name = f"{zh_table} {table}"
            r = session.patch(f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{tid}",
                              headers=headers, timeout=20, json={"name": new_name})
            b = r.json()
            logger.info("[%s] 表名 %s -> %s: %s", table, cur_name, new_name,
                        "OK" if b.get("code") == 0 else b.get("msg"))
        else:
            logger.info("[%s] 表名已含中文, 跳过", table)

        # 字段名加中文
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            r = session.get(f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{tid}/fields",
                            headers=headers, params=params, timeout=20)
            b = r.json()
            if b.get("code") != 0:
                raise RuntimeError(f"读取字段失败: {b.get('msg')}")
            data = b.get("data") or {}
            for f in data.get("items") or []:
                fname = f["field_name"]
                if _has_zh(fname):
                    continue
                zh = zh_of.get(fname) or ZH_FALLBACK.get(fname) or ""
                new_fname = f"{zh} {fname}".strip()
                payload = {"field_name": new_fname, "type": f["type"]}
                if f.get("property") is not None:
                    payload["property"] = f["property"]
                r = _request(session, "PUT",
                         f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{tid}/fields/{f['field_id']}",
                         headers, json=payload)
            rb = r.json()
            if rb.get("code") not in (0, 1254606):  # 1254606=DataNotChange 视为成功
                logger.warning("[%s] 字段 %s 改名失败: %s", table, fname, rb.get("msg"))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
        logger.info("[%s] 字段中文化完成", table)


def create_bitable_table(session, headers, app_token, name, fields):
    r = session.post(f"{FS_BASE}/bitable/v1/apps/{app_token}/tables",
                     headers=headers, timeout=20,
                     json={"table": {"name": name, "fields": fields}})
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"创建数据表 {name} 失败: {body.get('msg')}")
    return body["data"]["table_id"]


def mysql_columns(cur, table):
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, COLUMN_KEY, EXTRA "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (table,))
    return cur.fetchall()


def mysql_pk(cur, table):
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_KEY='PRI'", (table,))
    row = cur.fetchone()
    return row["COLUMN_NAME"] if row else None


def to_bitable_value(col, v):
    """MySQL 值 → 多维表格 record 字段值"""
    if v is None:
        return None
    dtype = col["DATA_TYPE"]
    if dtype == "json":
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        return json.dumps(v, ensure_ascii=False)
    if dtype in ("datetime", "timestamp", "date"):
        # date/datetime 对象或 'YYYY-MM-DD' 字符串 → 毫秒时间戳
        if hasattr(v, "timestamp"):
            return int(v.timestamp() * 1000)
        s = str(v)
        fmt = "%Y-%m-%d %H:%M:%S" if ":" in s else "%Y-%m-%d"
        return int(time.mktime(time.strptime(s, fmt)) * 1000)
    if dtype == "tinyint" and col["COLUMN_TYPE"] == "tinyint(1)":
        return bool(v)
    if dtype in ("int", "bigint", "smallint"):
        return int(v)
    if dtype in ("decimal", "float", "double"):
        return float(v)
    return str(v)


def fetch_mysql_rows(cur, table, cols):
    cur.execute(f"SELECT * FROM `{table}`")
    rows = []
    for r in cur.fetchall():
        rec, pk_val = {}, None
        for c in cols:
            v = r.get(c["COLUMN_NAME"])
            if "auto_increment" in (c["EXTRA"] or ""):
                continue  # 飞书行号代替自增主键
            rec[c["COLUMN_NAME"]] = to_bitable_value(c, v)
        rows.append(rec)
    return rows


def table_field_names(session, headers, app_token, table_id):
    """读数据表字段名列表(用于按主键去重判断记录是否存在)"""
    names, page_token = [], ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = _request(session, "GET",
                     f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                     headers, params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"读取字段失败: {body.get('msg')}")
        data = body.get("data") or {}
        names.extend(f["field_name"] for f in data.get("items") or [])
        if not data.get("has_more"):
            return names
        page_token = data.get("page_token") or ""


def english_field_map(names):
    """'中文 english' 字段名 → {英文列名: 实际字段名}; 兼容未改名的纯英文字段"""
    out = {}
    for n in names:
        out[n] = n
        if " " in n:
            eng = n.rsplit(" ", 1)[-1]
            out.setdefault(eng, n)
    return out


def existing_record_keys(session, headers, app_token, table_id, pk_field):
    """拉全表 pk 字段现有值集合, 用于增量去重"""
    keys, page_token = set(), ""
    while True:
        params = {"page_size": 500, "field_names": f'["{pk_field}"]'}
        if page_token:
            params["page_token"] = page_token
        r = session.get(f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                        headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"读取记录失败: {body.get('msg')}")
        data = body.get("data") or {}
        for item in data.get("items") or []:
            v = (item.get("fields") or {}).get(pk_field)
            if isinstance(v, list):  # 文本字段返回富文本数组
                v = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
            if v is not None:
                keys.add(str(v))
        if not data.get("has_more"):
            return keys
        page_token = data.get("page_token") or ""


def batch_create_records(session, headers, app_token, table_id, records):
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        r = session.post(
            f"{FS_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            headers=headers, timeout=30,
            json={"records": [{"fields": {k: v for k, v in rec.items() if v is not None}}
                              for rec in chunk]})
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"批量写入失败: {body.get('msg')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables-only", action="store_true", help="只建表不同步数据")
    ap.add_argument("--tables", nargs="*", default=KB_TABLES, help="指定同步的表(默认全部 9 张)")
    ap.add_argument("--rename-zh", action="store_true", help="表名/字段名加中文前缀(幂等,可重复跑)")
    args = ap.parse_args()

    session = requests.Session()
    session.trust_env = False
    token = fs_token(session)
    headers = {"Authorization": f"Bearer {token}"}
    app_token = resolve_wiki_app_token(session, WIKI_NODE, headers)
    logger.info("wiki 节点解析成功, bitable app_token=%s", app_token)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if args.rename_zh:
                rename_to_zh(session, headers, app_token, cur)
                return

            existing = list_bitable_tables(session, headers, app_token)
            logger.info("多维表格现有数据表: %s", ", ".join(existing) or "(空)")

            for table in args.tables:
                if table not in KB_TABLES:
                    logger.warning("跳过未知表 %s", table)
                    continue
                cols = mysql_columns(cur, table)
                pk = mysql_pk(cur, table)

                tid = resolve_table_id(existing, table)
                if tid:
                    table_id = tid
                    logger.info("[%s] 已存在, 跳过建表", table)
                else:
                    fields = [f for c in cols if (f := map_field(c))]
                    table_id = create_bitable_table(session, headers, app_token, table, fields)
                    logger.info("[%s] 已创建, table_id=%s, %d 个字段", table, table_id, len(fields))
                    existing = list_bitable_tables(session, headers, app_token)

                if args.tables_only:
                    continue

                rows = fetch_mysql_rows(cur, table, cols)
                if not rows:
                    logger.info("[%s] MySQL 无数据", table)
                    continue
                # 飞书表可能已被中文化为“中文 英文”字段名；读写时必须使用实际字段名。
                field_map = english_field_map(table_field_names(session, headers, app_token, table_id))
                missing = [name for name in rows[0] if name not in field_map]
                if missing:
                    raise RuntimeError(f"[{table}] 飞书缺少字段: {', '.join(missing)}")
                actual_pk = field_map.get(pk, pk) if pk else None
                if pk and any(c["COLUMN_NAME"] == pk and "auto_increment" not in (c["EXTRA"] or "")
                              for c in cols):
                    have = existing_record_keys(session, headers, app_token, table_id, actual_pk)
                    rows = [r for r in rows if str(r.get(pk)) not in have]
                    if not rows:
                        logger.info("[%s] 数据已同步, 无增量", table)
                        continue
                rows = [{field_map[name]: value for name, value in row.items()} for row in rows]
                batch_create_records(session, headers, app_token, table_id, rows)
                logger.info("[%s] 已同步 %d 行", table, len(rows))
    logger.info("完成")


if __name__ == "__main__":
    main()
