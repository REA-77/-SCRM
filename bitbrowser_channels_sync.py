"""Sync WeChat Channels cookies from BitBrowser into crawler_cookies_account."""
import asyncio
import json
import os

import pymysql
import requests
import websockets
from urllib.parse import parse_qs, urlparse

BIT_API = os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")
DOMAIN_SUFFIX = os.getenv("CHANNELS_DOMAIN_SUFFIX", "channels.weixin.qq.com")
PLATFORM = "channels"


def normalize_ws(endpoint):
    if endpoint and "://" not in endpoint:
        return f"ws://{endpoint}"
    return endpoint


def resolve_page_ws(endpoint):
    endpoint = normalize_ws(endpoint)
    if "/devtools/browser/" not in endpoint:
        return endpoint
    base = endpoint.split("/devtools/browser/", 1)[0]
    try:
        pages = requests.get(f"http://{base.split('://')[-1]}/json/list", timeout=10).json()
        page = next((item for item in pages if item.get("type") == "page"), None)
        if page and page.get("webSocketDebuggerUrl"):
            return page["webSocketDebuggerUrl"]
    except requests.RequestException:
        pass
    return endpoint


def list_windows():
    selected = os.getenv("BITBROWSER_WINDOW_ID", "").strip()
    if selected:
        return [{"id": selected, "name": selected}]
    response = requests.post(
        f"{BIT_API}/browser/list", json={"page": 0, "pageSize": 100}, timeout=30
    ).json()
    return response.get("data", {}).get("list", [])


def open_window(window_id):
    response = requests.post(
        f"{BIT_API}/browser/open", json={"id": window_id}, timeout=60
    ).json()
    if not response.get("success"):
        raise RuntimeError(response)
    data = response["data"]
    return {
        "ws": data.get("ws", ""),
        "http": data.get("http", "") or data.get("httpUrl", ""),
    }


def close_window(window_id):
    try:
        requests.post(f"{BIT_API}/browser/close", json={"id": window_id}, timeout=30)
    except requests.RequestException:
        pass


async def fetch_cookies(ws):
    async with websockets.connect(normalize_ws(ws), max_size=50 * 1024 * 1024) as conn:
        await conn.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        response = json.loads(await asyncio.wait_for(conn.recv(), timeout=30))
    cookies = response.get("result", {}).get("cookies", [])
    values = {
        item["name"]: item["value"]
        for item in cookies
        if item.get("domain", "").lstrip(".").endswith(DOMAIN_SUFFIX)
    }
    return "; ".join(f"{key}={value}" for key, value in values.items()), values


async def capture_request_metadata(debug_endpoint):
    """Capture request headers and query parameters from the selected window."""
    if not debug_endpoint:
        return {}

    metadata = {}
    command_id = 100

    async def send(conn, method, params=None, session_id=None):
        nonlocal command_id
        command_id += 1
        message = {"id": command_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        await conn.send(json.dumps(message))
        return command_id

    def collect(url, headers):
        if DOMAIN_SUFFIX not in url:
            return
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        for source, target in (
            ("finger-print-device-id", "fingerprint_device_id"),
            ("x-wechat-uin", "wechat_uin"),
            ("user-agent", "user_agent"),
        ):
            if headers.get(source) and not metadata.get(target):
                metadata[target] = headers[source]
        query = parse_qs(urlparse(url).query)
        for source, target in (("_aid", "aid"), ("_rid", "rid")):
            if query.get(source) and not metadata.get(target):
                metadata[target] = query[source][0]

    page_ws = resolve_page_ws(debug_endpoint)
    async with websockets.connect(normalize_ws(page_ws), max_size=50 * 1024 * 1024) as conn:
        await send(conn, "Network.enable")
        await send(conn, "Page.navigate", {"url": "https://channels.weixin.qq.com/platform"})
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            message = json.loads(await asyncio.wait_for(conn.recv(), timeout=20))
            if message.get("method") == "Network.requestWillBeSent":
                request = message.get("params", {}).get("request", {})
                collect(request.get("url", ""), request.get("headers", {}))
                if metadata.get("fingerprint_device_id") and metadata.get("wechat_uin"):
                    break
    return metadata


def upsert_account(name, cookies, metadata):
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "media_crawler_pro"),
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "ALTER TABLE crawler_cookies_account ADD COLUMN account_meta TEXT NULL"
                )
            except Exception:
                pass
            cur.execute(
                "SELECT id FROM crawler_cookies_account WHERE platform_name=%s AND account_name=%s",
                (PLATFORM, name),
            )
            row = cur.fetchone()
            encoded = json.dumps(metadata, ensure_ascii=False)
            if row:
                cur.execute(
                    "UPDATE crawler_cookies_account SET cookies=%s, account_meta=%s, status=0 WHERE id=%s",
                    (cookies, encoded, row[0]),
                )
                return row[0]
            cur.execute(
                "INSERT INTO crawler_cookies_account "
                "(account_name, platform_name, cookies, account_meta, status) VALUES (%s,%s,%s,%s,0)",
                (name, PLATFORM, cookies, encoded),
            )
            return cur.lastrowid
    finally:
        conn.commit()
        conn.close()


async def main():
    for window in list_windows():
        window_id = window["id"]
        name = window.get("name") or str(window.get("seq") or window_id)
        try:
            debug_info = open_window(window_id)
            cookies, cookie_map = await fetch_cookies(debug_info["ws"])
            if "sessionid" not in cookie_map:
                print(f"[{name}] 未检测到 sessionid，跳过")
                continue
            captured = await capture_request_metadata(debug_info["ws"])
            metadata = {
                "wechat_uin": os.getenv(
                    "CHANNELS_WECHAT_UIN",
                    captured.get("wechat_uin") or cookie_map.get("wxuin", ""),
                ),
                "fingerprint_device_id": os.getenv(
                    "CHANNELS_FINGERPRINT_DEVICE_ID",
                    captured.get("fingerprint_device_id", ""),
                ),
                "user_agent": os.getenv(
                    "CHANNELS_USER_AGENT",
                    captured.get("user_agent", "Mozilla/5.0"),
                ),
                "aid": os.getenv("CHANNELS_AID", captured.get("aid", "")),
                "rid": os.getenv("CHANNELS_RID", captured.get("rid", "")),
            }
            account_id = upsert_account(f"比特视频号-{name}", cookies, metadata)
            print(f"[{name}] 已写入 channels 账号池，account_id={account_id}")
        except Exception as exc:
            print(f"[{name}] 同步失败: {exc}")
        finally:
            close_window(window_id)


if __name__ == "__main__":
    asyncio.run(main())
