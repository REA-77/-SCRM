"""Read-only login checks; network/risk failures are never called expired."""
import asyncio
import hashlib
import json
import time

import requests

from account_cookie_sync import PLATFORMS
from db import get_conn

DOUYIN_SIGN_SERVICE = "http://localhost:8989"
DOUYIN_API = "https://www.douyin.com"


def fingerprint(cookie):
    return hashlib.sha256((cookie or "").encode("utf-8")).hexdigest()


def ensure_schema(cur):
    columns = {
        "cookie_status": "VARCHAR(16) NOT NULL DEFAULT 'unknown'",
        "cookie_checked_at": "BIGINT NULL",
        "cookie_check_message": "VARCHAR(255) NULL",
        "cookie_fingerprint": "CHAR(64) NULL",
    }
    for column, definition in columns.items():
        cur.execute("SELECT COUNT(*) c FROM information_schema.COLUMNS WHERE "
                    "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='平台运营账号' AND COLUMN_NAME=%s", (column,))
        if not cur.fetchone()["c"]:
            cur.execute(f"ALTER TABLE `平台运营账号` ADD COLUMN `{column}` {definition}")


def check_cookie(platform, cookie, credentials=None):
    if not str(cookie or "").strip():
        return "missing", "尚未保存 Cookie"
    parts = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    if platform in PLATFORMS and not any(parts.get(k) for k in PLATFORMS[platform][1]):
        return "expired", "缺少平台登录凭证，请重新登录并更新 Cookie"
    try:
        meta = json.loads(credentials or "{}")
        ua = meta.get("user_agent") if isinstance(meta, dict) else None
    except (ValueError, TypeError):
        ua = None
    ua = ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    try:
        if platform == "小红书":
            from xhs_direct.client import XhsApiClient
            from xhs_direct.responses import XhsApiError
            try:
                result = asyncio.run(XhsApiClient(cookie, ua, timeout=15).self_info())
            except XhsApiError as exc:
                if exc.signal in ("auth_expired", "http_401"):
                    return "expired", "平台返回未登录，请重新登录并更新 Cookie"
                return "unknown", "检测受网络或平台验证限制，暂无法确认"
            data = result
            if data.get("guest") is True:
                return "expired", "当前为游客登录状态"
            if data.get("user_id") and data.get("guest") is False:
                return "valid", "平台确认登录有效"
        elif platform == "抖音":
            from urllib.parse import urlencode
            uri = "/aweme/v1/web/history/read/"
            query = urlencode({"aid": "6383", "cookie_enabled": "true", "platform": "PC",
                               "max_cursor": "0", "count": "20"})
            signed = requests.post(DOUYIN_SIGN_SERVICE + "/signsrv/v1/douyin/sign",
                                   json={"uri": uri, "query_params": query,
                                         "user_agent": ua, "cookies": cookie}, timeout=15).json()
            if not signed.get("isok"):
                return "unknown", "签名服务不可用，暂无法检测"
            response = requests.get(DOUYIN_API + uri + "?" + query + "&a_bogus=" + signed["data"]["a_bogus"],
                                    headers={"Cookie": cookie, "User-Agent": ua}, timeout=15,
                                    allow_redirects=False)
            if response.status_code == 401:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
            if response.status_code != 200:
                return "unknown", "检测受网络或平台验证限制，暂无法确认"
            result = response.json()
            if result.get("status_code") == 0:
                return "valid", "平台确认登录有效"
            if result.get("status_code") == 8 or str(result.get("status_msg") or "").strip() in ("用户未登录", "未登录", "登录已过期", "登录失效"):
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
        elif platform == "快手":
            query = "query visionProfileUserList($ftype: Int) { visionProfileUserList(ftype: $ftype) { result } }"
            response = requests.post(
                "https://www.kuaishou.com/graphql",
                json={"operationName": "visionProfileUserList", "variables": {"ftype": 1}, "query": query},
                headers={"Cookie": cookie, "User-Agent": ua, "Content-Type": "application/json"},
                timeout=15, allow_redirects=False)
            if response.status_code == 401:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
            if response.status_code != 200:
                return "unknown", "快手接口网络异常，未判定 Cookie 失效"
            result = response.json()
            value = ((result.get("data") or {}).get("visionProfileUserList") or {}).get("result")
            if value == 1:
                return "valid", "平台确认登录有效"
            if value is not None:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
        elif platform == "微博":
            response = requests.get("https://m.weibo.cn/api/config",
                                    headers={"Cookie": cookie, "User-Agent": ua}, timeout=15,
                                    allow_redirects=False)
            if response.status_code == 401:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
            if response.status_code != 200:
                return "unknown", "微博接口网络异常，未判定 Cookie 失效"
            result = response.json()
            logged_in = (result.get("data") or {}).get("login")
            if logged_in is True:
                return "valid", "平台确认登录有效"
            if logged_in is False:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
        elif platform == "Reddit":
            response = requests.get("https://www.reddit.com/api/me.json?raw_json=1",
                                    headers={"Cookie": cookie, "User-Agent": ua}, timeout=15,
                                    allow_redirects=False)
            if response.status_code in (401, 403):
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
            if response.status_code != 200:
                return "unknown", "Reddit 接口网络异常，未判定 Cookie 失效"
            result = response.json()
            data = result.get("data") or {}
            if result.get("kind") == "t2" and data.get("id"):
                return "valid", "平台确认登录有效"
            if result.get("kind") is not None:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
        elif platform in ("B站", "知乎"):
            url = ("https://api.bilibili.com/x/web-interface/nav" if platform == "B站"
                   else "https://www.zhihu.com/api/v4/me")
            response = requests.get(url, headers={"Cookie": cookie, "User-Agent": ua},
                                    timeout=15, allow_redirects=False)
            if response.status_code == 401:
                return "expired", "平台返回未登录，请重新登录并更新 Cookie"
            if response.status_code != 200:
                return "unknown", "检测受网络或平台验证限制，暂无法确认"
            result = response.json()
            if platform == "B站":
                data = result.get("data") or {}
                if result.get("code") == -101:
                    return "expired", "平台返回未登录，请重新登录并更新 Cookie"
                if result.get("code") == 0 and data.get("isLogin") is True:
                    return "valid", "平台确认登录有效"
            elif result.get("id") and not result.get("error"):
                return "valid", "平台确认登录有效"
        else:
            return "unknown", "此平台暂不支持在线有效性检测"
    except requests.exceptions.ProxyError:
        return "unknown", f"{platform}接口网络不可用（当前代理无法连接），未判定 Cookie 失效"
    except requests.exceptions.Timeout:
        return "unknown", f"{platform}接口请求超时，未判定 Cookie 失效"
    except requests.exceptions.RequestException:
        return "unknown", f"{platform}接口网络异常，未判定 Cookie 失效"
    except Exception:
        # Never expose upstream response bodies, cookies or credentials.
        return "unknown", "检测失败或超时，暂无法确认 Cookie 是否有效"
    return "unknown", "平台未返回明确登录状态"


def check_account(platform, nickname):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `cookie`, `凭据` AS credentials FROM `平台运营账号` "
                        "WHERE `平台`=%s AND `昵称`=%s", (platform, nickname))
            account = cur.fetchone()
    if not account:
        return None
    cookie = account.get("cookie") or ""
    status, message = check_cookie(platform, cookie, account.get("credentials"))
    checked_at = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE `平台运营账号` SET cookie_status=%s, cookie_check_message=%s, "
                        "cookie_checked_at=%s, cookie_fingerprint=%s WHERE `平台`=%s AND `昵称`=%s "
                        "AND SHA2(COALESCE(`cookie`, ''), 256)=%s",
                        (status, message, checked_at, fingerprint(cookie), platform, nickname, fingerprint(cookie)))
            # A concurrent refresh must never acquire an old cookie's status.
            if not cur.rowcount:
                cur.execute("SELECT SHA2(COALESCE(`cookie`, ''), 256) AS fingerprint "
                            "FROM `平台运营账号` WHERE `平台`=%s AND `昵称`=%s", (platform, nickname))
                current = cur.fetchone()
                if not current or current["fingerprint"] != fingerprint(cookie):
                    return {"cookie_status": "unknown", "message": "Cookie 已改变，请重新检测"}
        conn.commit()
    return {"cookie_status": status, "message": message, "cookie_checked_at": checked_at}
