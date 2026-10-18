# -*- coding: utf-8 -*-
"""Sync Bilibili cookies from BitBrowser windows into 平台运营账号.cookie.

默认只同步 `平台运营账号` 中平台为 B站且已填写 Bit窗口 的账号。
使用方式:
  py -X utf8 bitbrowser_bilibili_cookie_sync.py
  py -X utf8 bitbrowser_bilibili_cookie_sync.py --all-match-by-name
"""
import argparse
import json
import asyncio
import re
import time
from collections import Counter

import requests
from playwright.async_api import async_playwright

from db import get_conn

BIT_API = "http://127.0.0.1:54345"
BILI_DOMAIN_SUFFIXES = ("bilibili.com", "biliapi.net")


def normalize_name(value):
    return re.sub(r"[\s～~_\-—]+", "", str(value or "").strip()).lower()


def list_windows():
    windows, page = [], 0
    while True:
        resp = requests.post(f"{BIT_API}/browser/list", json={"page": page, "pageSize": 100}, timeout=30).json()
        data = resp.get("data") or {}
        rows = data.get("list") or []
        windows.extend(rows)
        total = data.get("totalNum") or len(windows)
        if len(windows) >= total or not rows:
            break
        page += 1
    return windows


def resolve_window_id(bit_window, windows):
    value = str(bit_window or "").strip()
    if not value:
        return ""
    for row in windows:
        if str(row.get("id")) == value or str(row.get("seq")) == value:
            return row.get("id")
    return value if not value.isdigit() else ""


def find_window_by_name(account_name, windows):
    target = normalize_name(account_name)
    if not target:
        return None
    best = None
    for row in windows:
        text = normalize_name(" ".join(str(row.get(k) or "") for k in ("name", "remark", "userName", "seq")))
        if not text:
            continue
        if target == text or target in text or text in target:
            return row
        if target[:4] and target[:4] in text:
            best = best or row
    return best


def open_window(window_id):
    last = None
    for attempt in range(1, 13):
        resp = requests.post(f"{BIT_API}/browser/open", json={"id": window_id}, timeout=60).json()
        last = resp
        ws = (resp.get("data") or {}).get("ws")
        if ws:
            return ws
        msg = str(resp.get("msg") or resp)
        if "正在关闭" in msg or "稍后" in msg or "启动" in msg:
            time.sleep(5)
            continue
        break
    raise RuntimeError(f"打开比特窗口失败: {last}")


def close_window(window_id):
    try:
        requests.post(f"{BIT_API}/browser/close", json={"id": window_id}, timeout=30)
    except Exception:
        pass


async def fetch_bili_cookie(ws):
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws, timeout=90000)
        try:
            cookies = await browser.contexts[0].cookies() if browser.contexts else []
        finally:
            await browser.close()
    domain_stat = Counter(c.get("domain", "") for c in cookies)
    seen = {}
    for item in cookies:
        domain = str(item.get("domain") or "").lstrip(".")
        if any(domain.endswith(suffix) for suffix in BILI_DOMAIN_SUFFIXES):
            name = item.get("name")
            value = item.get("value")
            if name:
                seen[name] = value or ""
    cookie = "; ".join(f"{k}={v}" for k, v in seen.items())
    return cookie, dict(domain_stat.most_common(12))


def load_accounts(all_match_by_name=False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if all_match_by_name:
                cur.execute("""
                    SELECT `平台`, `昵称`, `ID`, `Bit窗口`, LENGTH(COALESCE(`cookie`,'')) cookie_len
                    FROM `平台运营账号`
                    WHERE `平台`='B站' AND COALESCE(`昵称`, '') <> ''
                    ORDER BY COALESCE(`Bit窗口`,'')<>'' DESC, `昵称`
                """)
            else:
                cur.execute("""
                    SELECT `平台`, `昵称`, `ID`, `Bit窗口`, LENGTH(COALESCE(`cookie`,'')) cookie_len
                    FROM `平台运营账号`
                    WHERE `平台`='B站' AND COALESCE(`Bit窗口`, '') <> ''
                    ORDER BY `昵称`
                """)
            return cur.fetchall()


def save_account_cookie(nickname, bit_window, cookie):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE `平台运营账号`
                SET `Bit窗口`=%s, `cookie`=%s
                WHERE `平台`='B站' AND `昵称`=%s
            """, (str(bit_window or ""), cookie, nickname))
            affected = cur.rowcount
        conn.commit()
    return affected


async def sync_account(account, windows, all_match_by_name=False):
    nickname = account.get("昵称") or ""
    window_row = None
    window_id = resolve_window_id(account.get("Bit窗口"), windows)
    if window_id:
        window_row = next((w for w in windows if str(w.get("id")) == str(window_id)), None)
    elif all_match_by_name:
        window_row = find_window_by_name(nickname, windows)
        window_id = (window_row or {}).get("id") or ""
    if not window_id:
        return {"nickname": nickname, "status": "skipped", "reason": "缺少Bit窗口且未匹配到窗口"}
    seq = (window_row or {}).get("seq") or account.get("Bit窗口") or ""
    try:
        ws = await asyncio.to_thread(open_window, window_id)
        cookie, domain_stat = await fetch_bili_cookie(ws)
        if "bili_jct=" not in cookie:
            return {"nickname": nickname, "seq": seq, "status": "failed", "reason": "未找到B站登录cookie(bili_jct)", "domains": domain_stat}
        save_account_cookie(nickname, seq, cookie)
        return {"nickname": nickname, "seq": seq, "status": "updated", "cookie_len": len(cookie)}
    finally:
        close_window(window_id)
        await asyncio.sleep(1)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-match-by-name", action="store_true", help="对没有 Bit窗口 的 B站账号，尝试按比特窗口名称匹配")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    windows = list_windows()
    accounts = load_accounts(args.all_match_by_name)
    if args.limit:
        accounts = accounts[:args.limit]
    print(f"比特窗口 {len(windows)} 个，待同步 B站账号 {len(accounts)} 个")
    updated = failed = skipped = 0
    for account in accounts:
        result = await sync_account(account, windows, all_match_by_name=args.all_match_by_name)
        status = result.get("status")
        if status == "updated":
            updated += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
        safe = {k: v for k, v in result.items() if k != "cookie"}
        print(json.dumps(safe, ensure_ascii=False), flush=True)
    print(f"完成: updated={updated}, failed={failed}, skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
