# -*- coding: utf-8 -*-
"""扫描比特浏览器全量窗口，把所有登录抖音的窗口 cookie 追加写入 MySQL 账号池。

流程: Local API 列全部窗口 -> 逐个 open -> CDP 拉 douyin cookie(含HttpOnly)
      -> 签名服务签名 self profile 接口 -> 拿 sec_uid/nickname
      -> 按 account_name upsert 到 crawler_cookies_account (新窗口追加、同窗口更新)

用法: uv run python bitbrowser_multi_sync.py
"""
import asyncio
import json
import os
from urllib.parse import urlencode

import httpx
import pymysql
import requests
import websockets

BIT_API = "http://127.0.0.1:54345"
SIGN_SRV = "http://localhost:8989"
DY_API = "https://www.douyin.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def list_windows():
    """分页拉取全部窗口（比特API单页最多10条）"""
    all_windows, page = [], 0
    while True:
        r = requests.post(f"{BIT_API}/browser/list",
                          json={"page": page, "pageSize": 100}, timeout=30).json()
        data = r.get("data", {})
        batch = data.get("list", [])
        all_windows.extend(batch)
        total = data.get("totalNum") or len(all_windows)
        if len(all_windows) >= total or not batch:
            break
        page += 1
    return all_windows


def open_window(wid: str) -> str:
    r = requests.post(f"{BIT_API}/browser/open", json={"id": wid}, timeout=60).json()
    return r["data"]["ws"]


def close_window(wid: str) -> None:
    """关闭比特浏览器窗口，释放资源；失败不影响主流程"""
    try:
        requests.post(f"{BIT_API}/browser/close", json={"id": wid}, timeout=30)
    except Exception:
        pass


async def fetch_dy_cookies(ws: str) -> str:
    """原生 CDP 拉取 douyin.com 全部 cookie（含 HttpOnly），返回 cookie 字符串"""
    async with websockets.connect(ws, max_size=50 * 1024 * 1024) as conn:
        await conn.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        resp = json.loads(await asyncio.wait_for(conn.recv(), timeout=30))
    cookies = resp.get("result", {}).get("cookies", [])
    seen = {}
    for c in cookies:
        if c["domain"].endswith("douyin.com"):
            seen[c["name"]] = c["value"]
    return "; ".join(f"{k}={v}" for k, v in seen.items())


async def get_self_sec_uid(cookie_str: str) -> tuple:
    """调签名服务给 user/profile/self 接口签名，请求拿 (sec_uid, nickname)"""
    uri = "/aweme/v1/web/user/profile/self/"
    params = {"aid": "6383", "cookie_enabled": "true", "platform": "PC"}
    query = urlencode(params)
    sign_resp = requests.post(
        f"{SIGN_SRV}/signsrv/v1/douyin/sign",
        json={"uri": uri, "query_params": query,
              "user_agent": UA, "cookies": cookie_str},
        timeout=30,
    ).json()
    if not sign_resp.get("isok"):
        raise RuntimeError(f"签名失败: {sign_resp.get('msg')}")
    a_bogus = sign_resp["data"]["a_bogus"]
    url = f"{DY_API}{uri}?{query}&a_bogus={a_bogus}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers={"Cookie": cookie_str, "User-Agent": UA})
        data = resp.json()
    if data.get("status_code") != 0 or not data.get("user"):
        raise RuntimeError(f"未登录或接口异常: {data.get('status_msg')}")
    user = data["user"]
    return user["sec_uid"], user.get("nickname", "")


def upsert_account(account_name: str, cookie_str: str, sec_uid: str) -> int:
    """按 account_name upsert 到 crawler_cookies_account（同窗口覆盖cookie，新窗口追加行）"""
    conn = pymysql.connect(
        host="localhost", port=3306, user="root", password=os.getenv("MYSQL_PASSWORD", ""),
        database="media_crawler_pro", charset="utf8mb4",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM crawler_cookies_account WHERE platform_name='dy' AND account_name=%s",
            (account_name,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE crawler_cookies_account SET cookies=%s, status=0 WHERE id=%s",
                (cookie_str, row[0]),
            )
            aid = row[0]
        else:
            cur.execute(
                "INSERT INTO crawler_cookies_account (account_name, platform_name, cookies, status) "
                "VALUES (%s,'dy',%s,0)",
                (account_name, cookie_str),
            )
            aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


async def main():
    windows = list_windows()
    # 断点续扫：MAX_SEQ 环境变量只扫 seq <= 该值的窗口
    max_seq = os.getenv("MAX_SEQ")
    if max_seq:
        windows = [w for w in windows if (w.get("seq") or 0) <= int(max_seq)]
    print(f"本次扫描 {len(windows)} 个比特浏览器窗口，逐个检查抖音登录态...", flush=True)
    # 续扫时合并已有 dy_creators.json，避免覆盖
    creators = []
    if max_seq and os.path.exists("dy_creators.json"):
        try:
            creators = json.load(open("dy_creators.json", encoding="utf-8"))
            print(f"合并已有 {len(creators)} 个 sec_uid", flush=True)
        except Exception:
            pass
    seen_sec_uids = set(creators)
    for w in windows:
        seq = w.get("seq")
        name = w.get("name") or f"窗口{seq}"
        wid = w["id"]
        try:
            # 整个窗口处理设 60s 超时，避免卡死
            ws = await asyncio.wait_for(asyncio.to_thread(open_window, wid), timeout=60)
            cookie_str = await asyncio.wait_for(fetch_dy_cookies(ws), timeout=30)
            if "sessionid" not in cookie_str:
                print(f"[seq {seq} {name}] 未登录抖音（无sessionid），跳过", flush=True)
                continue
            sec_uid, nickname = await asyncio.wait_for(get_self_sec_uid(cookie_str), timeout=30)
            account_name = f"比特{seq}-{nickname or name}"
            aid = upsert_account(account_name, cookie_str, sec_uid)
            if sec_uid not in seen_sec_uids:
                creators.append(sec_uid)
                seen_sec_uids.add(sec_uid)
            print(f"[seq {seq} {name}] 抖音昵称={nickname} sec_uid={sec_uid} account_id={aid}", flush=True)
        except Exception as e:
            print(f"[seq {seq} {name}] 失败: {e}", flush=True)
        finally:
            close_window(wid)
            await asyncio.sleep(1)

    # 输出可直接粘贴进 base_config.py 的配置块（按唯一 sec_uid）
    print("\n# === 复制到 config/base_config.py 的 DY_CREATOR_ID_LIST ===")
    print("DY_CREATOR_ID_LIST = [")
    for s in creators:
        print(f'    "{s}",')
    print("]")
    # 同时落盘一份
    with open("dy_creators.json", "w", encoding="utf-8") as f:
        json.dump(creators, f, ensure_ascii=False, indent=2)
    print(f"\n共 {len(creators)} 个唯一创作者，sec_uid 已写入 dy_creators.json")


if __name__ == "__main__":
    asyncio.run(main())
