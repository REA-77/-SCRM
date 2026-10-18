"""Read platform cookies from an explicitly bound BitBrowser window."""
import asyncio
import json
import time

import requests
import websockets

BIT_API = "http://127.0.0.1:54345"
# Login markers prevent anonymous browser cookies from replacing saved credentials.
PLATFORMS = {
    "抖音": (("douyin.com",), ("sessionid", "sessionid_ss")),
    "B站": (("bilibili.com", "biliapi.net"), ("SESSDATA",)),
    "小红书": (("xiaohongshu.com",), ("web_session",)),
    "知乎": (("zhihu.com",), ("z_c0",)),
    "微博": (("weibo.com", "weibo.cn"), ("SUB",)),
    "快手": (("kuaishou.com",), ("kuaishou.server.web_st", "kuaishou.server.web_ph")),
    "贴吧": (("baidu.com",), ("BDUSS",)),
    "Reddit": (("reddit.com",), ("reddit_session", "token_v2")),
}


def bit_call(path, payload):
    try:
        response = requests.post(BIT_API + path, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("无法连接比特浏览器，请确认已启动并开启本地 API") from None
    if not result.get("success"):
        raise RuntimeError("比特浏览器操作失败，请检查窗口状态后重试")
    return result.get("data") or {}


def resolve_window(value):
    value = str(value or "").strip()
    if not value:
        raise RuntimeError("未配置 Bit 窗口，请先编辑账号")
    page = 0
    while True:
        data = bit_call("/browser/list", {"page": page, "pageSize": 100})
        rows = data.get("list") or []
        matches = [r for r in rows if value in (str(r.get("id")), str(r.get("seq")))]
        if matches:
            return matches[0]["id"]
        if not rows:
            break
        page += 1
    raise RuntimeError("未找到绑定的 Bit 窗口，请核对窗口序号或 ID")


def select_cookie(platform, cookies):
    domains, markers = PLATFORMS[platform]
    selected = {}
    for item in cookies:
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if not any(domain == d or domain.endswith("." + d) for d in domains):
            continue
        if 0 < item.get("expires", -1) < time.time():
            continue
        if item.get("name") and item.get("value"):
            selected[item["name"]] = item["value"]
    if not any(selected.get(marker) for marker in markers):
        raise RuntimeError("未读取到登录 Cookie，请在已打开的窗口登录对应平台后重试")
    return "; ".join(f"{key}={value}" for key, value in selected.items())


async def read_browser(ws, platform):
    async with websockets.connect(ws, max_size=50 * 1024 * 1024,
                                  open_timeout=20, close_timeout=5) as sock:
        async def command(number, method):
            await sock.send(json.dumps({"id": number, "method": method}))
            while True:
                result = json.loads(await sock.recv())
                if result.get("id") == number:
                    if "error" in result:
                        raise RuntimeError("读取浏览器 Cookie 失败，请重试")
                    return result.get("result") or {}
        data = await asyncio.wait_for(command(1, "Storage.getCookies"), 25)
        cookie = select_cookie(platform, data.get("cookies") or [])
        version = await asyncio.wait_for(command(2, "Browser.getVersion"), 10)
        return cookie, version.get("userAgent", "")


def fetch_account_cookie(platform, bit_window):
    if platform not in PLATFORMS:
        raise RuntimeError(f"暂不支持同步{platform}的 Cookie")
    window_id = resolve_window(bit_window)
    data = bit_call("/browser/open", {"id": window_id})
    if not data.get("ws"):
        raise RuntimeError("窗口尚未就绪，请稍后重试")
    # Disconnect CDP only; leave the visible window open for login/retry.
    return asyncio.run(read_browser(data["ws"], platform))
