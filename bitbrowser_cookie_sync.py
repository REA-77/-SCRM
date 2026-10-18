# -*- coding: utf-8 -*-
"""从比特浏览器窗口拉取抖音完整 cookie（含 HttpOnly），写入 MySQL 账号池。

用法: uv run python bitbrowser_cookie_sync.py
"""
import asyncio
import json
import os
import sys

import pymysql
import requests
import websockets

BIT_API = "http://127.0.0.1:54345"
# 要同步的比特浏览器窗口 ID（Z-Lab）
WINDOW_ID = "4e9c132695784c18ac054f68987856e7"
# 抖音 cookie 匹配的域名后缀
DY_DOMAIN_SUFFIX = "douyin.com"


def open_browser() -> str:
    """打开(或复用已开)比特浏览器窗口，返回 CDP websocket 端点"""
    r = requests.post(f"{BIT_API}/browser/open", json={"id": WINDOW_ID}, timeout=30).json()
    if not r.get("success"):
        # 已打开的窗口再调 open 会返回错误，尝试 close 后重开
        requests.post(f"{BIT_API}/browser/close", json={"id": WINDOW_ID}, timeout=30)
        r = requests.post(f"{BIT_API}/browser/open", json={"id": WINDOW_ID}, timeout=30).json()
        if not r.get("success"):
            raise RuntimeError(f"打开比特浏览器窗口失败: {r}")
    return r["data"]["ws"]


async def fetch_cookies(ws: str) -> str:
    """通过原生 CDP WebSocket 拉取窗口内全部 cookie（含 HttpOnly），拼接 douyin.com 域的 cookie 字符串"""
    async with websockets.connect(ws, max_size=50 * 1024 * 1024) as conn:
        # Storage.getCookies 不带 browserContextId 时返回所有上下文的全部 cookie
        await conn.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        resp = json.loads(await asyncio.wait_for(conn.recv(), timeout=30))
    if "error" in resp:
        # 老内核回退到 Network.getAllCookies
        await conn.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
        resp = json.loads(await asyncio.wait_for(conn.recv(), timeout=30))
    cookies = resp.get("result", {}).get("cookies", [])
    # 调试：打印各域名 cookie 数量
    from collections import Counter
    domain_stat = Counter(c["domain"] for c in cookies)
    print("cookie域名分布:", dict(domain_stat.most_common(20)))
    seen = {}
    for c in cookies:
        if c["domain"].endswith(DY_DOMAIN_SUFFIX):
            seen[c["name"]] = c["value"]
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def save_to_mysql(cookie_str: str, account_name: str) -> int:
    """写入账号池表 crawler_cookies_account（platform_name='dy'）"""
    conn = pymysql.connect(
        host="localhost", port=3306, user="root", password=os.getenv("MYSQL_PASSWORD", ""),
        database="media_crawler_pro", charset="utf8mb4",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM crawler_cookies_account WHERE platform_name='dy' LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE crawler_cookies_account SET cookies=%s, account_name=%s, status=0 WHERE id=%s",
                (cookie_str, account_name, row[0]),
            )
            account_id = row[0]
        else:
            cur.execute(
                "INSERT INTO crawler_cookies_account (account_name, platform_name, cookies, status) "
                "VALUES (%s, %s, %s, 0)",
                (account_name, "dy", cookie_str),
            )
            account_id = cur.lastrowid
    conn.commit()
    conn.close()
    return account_id


async def main():
    # 遍历所有窗口，找到登录了抖音（含sessionid）的窗口
    r = requests.post(f"{BIT_API}/browser/list", json={"page": 0, "pageSize": 100}, timeout=30).json()
    windows = r["data"]["list"]
    print(f"共 {len(windows)} 个窗口，逐个检查 douyin cookie...")

    for w in windows:
        wid, name = w["id"], w.get("name") or w.get("seq")
        try:
            resp = requests.post(f"{BIT_API}/browser/open", json={"id": wid}, timeout=60).json()
            if not resp.get("success"):
                print(f"[{name}] 打开失败: {resp.get('msg')}")
                continue
            ws = resp["data"]["ws"]
            cookie_str = await fetch_cookies(ws)
            has_login = "sessionid" in cookie_str
            if cookie_str:
                print(f"[{name}] douyin cookie 长度={len(cookie_str)} 含sessionid={has_login}")
            if has_login:
                aid = save_to_mysql(cookie_str, f"比特浏览器-{name}")
                print(f"==> 窗口[{name}] 已写入 MySQL 账号池, account id={aid}")
                return
        except Exception as e:
            print(f"[{name}] 检查异常: {e}")

    print("所有窗口均未找到含 sessionid 的抖音登录 cookie，请确认已在某个窗口登录 douyin.com")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
