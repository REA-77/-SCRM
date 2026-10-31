# -*- coding: utf-8 -*-
"""Process queued comment auto-operation tasks.

Douyin replies use the signed direct API while existing BitBrowser account
bindings remain available for login and Cookie synchronisation.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import subprocess
import threading
import time
from pathlib import Path

import requests

from db import get_conn

BASE_DIR = Path(__file__).resolve().parent
BIT_API = os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")
_BIT_WINDOW_LOCKS = {}
_BIT_WINDOW_LOCKS_GUARD = threading.Lock()
# Platform APIs must not inherit a stale desktop proxy (for example
# HTTP_PROXY=http://127.0.0.1:9). BitBrowser/local control requests may still
# use the normal environment, but Bilibili's public API should go direct.
_DIRECT_HTTP = requests.Session()
_DIRECT_HTTP.trust_env = False


def _bit_window_lock(window_id):
    key = str(window_id or "").strip() or "__default__"
    with _BIT_WINDOW_LOCKS_GUARD:
        return _BIT_WINDOW_LOCKS.setdefault(key, threading.Lock())


def _reset_cdp_page_targets(cdp_http):
    """Close stale page targets without PowerShell, preserving the profile."""
    address = str(cdp_http or '').strip().rstrip('/')
    if not address:
        return 0
    if not address.startswith(('http://', 'https://')):
        address = 'http://' + address
    try:
        listing = requests.get(f'{address}/json/list', timeout=10).json()
    except Exception:
        return 0
    closed = 0
    for target in listing if isinstance(listing, list) else []:
        target_id = str(target.get('id') or '').strip()
        if target.get('type') != 'page' or not target_id:
            continue
        cdp = {}
        cdp_http = ""
        try:
            requests.get(f'{address}/json/close/{target_id}', timeout=5)
            closed += 1
        except Exception:
            continue
    return closed

# The supplied ``douyin_commenter`` bundle is kept next to this application.
# It signs and sends the Web comment API directly.  Its identity must however
# be read from the account's BitBrowser profile: a Cookie captured in one
# browser fingerprint cannot safely be replayed with a global fake fingerprint.
DOUYIN_COMMENTER_ROOT = BASE_DIR / "douyin_commenter_reference" / "douyin_commenter"
DOUYIN_BROWSER_TOOL_ROOT = BASE_DIR / "douyin-creator-tools"
_DOUYIN_BROWSER_IDENTITY_CACHE = {}


def _explicitly_missing(task):
    """Honour deletion flags emitted by collectors without guessing from text."""
    return any(task.get(key) in (1, True, '1', 'true', 'deleted') for key in
               ('deleted', 'is_deleted', 'comment_deleted', 'unavailable'))


def verify_comment_exists(task):
    """Lightweight pre-send check supplied by each platform adapter.

    ``False`` means the target is known gone; ``None`` means the adapter cannot
    safely decide and the normal send path must obtain its own page/API result.
    We intentionally never infer deletion from a transient network error.
    """
    if _explicitly_missing(task):
        return False
    if task.get('platform') != 'B站':
        return None
    try:
        cookie = get_bilibili_cookie(task.get('video_account') or task.get('account_nickname')).get('cookies') or ''
        oid = str(task.get('video_id') or task.get('content_id') or '').strip()
        rpid = str(task.get('comment_id') or '').strip()
        if not oid or not rpid:
            return None
        response = _DIRECT_HTTP.get('https://api.bilibili.com/x/v2/reply/reply', params={
            'type': '1', 'oid': oid, 'root': rpid,
        }, headers=bilibili_headers(cookie, task.get('bvid')), timeout=15)
        payload = response.json()
        # A successful empty root response is a reliable deletion/not-visible signal.
        if payload.get('code') == 0:
            root = (payload.get('data') or {}).get('root') or {}
            return str(root.get('rpid') or '') == rpid
    except Exception:
        pass
    return None


def verify_reply(task):
    """Adapter reconciliation hook used by the delayed uncertain-task scanner.

    Existing adapters do not expose a reliable idempotent lookup for every
    platform yet.  Returning ``unknown`` is intentional: it keeps the task in
    the review queue rather than risking a duplicate reply.
    """
    if _explicitly_missing(task):
        return 'missing'
    # B站's reply endpoint confirms that the source still exists, but cannot
    # identify our child reply without a persisted reply ID on old tasks.
    exists = verify_comment_exists(task)
    return 'missing' if exists is False else 'unknown'


def fetch_douyin_tasks(limit):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.*, c.comment_id, c.aweme_id, c.content, c.reply_suggestion, a.title
                FROM comment_auto_task t
                JOIN douyin_aweme_comment c ON c.id=t.src_pk
                LEFT JOIN douyin_aweme a ON a.aweme_id=c.aweme_id
                WHERE t.status='pending' AND t.platform='抖音' AND t.action_type IN ('reply', 'delete')
                ORDER BY t.id LIMIT %s
            """, (limit,))
            return cur.fetchall()


def fetch_bilibili_tasks(limit):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.*, c.comment_id, c.video_id, c.parent_comment_id, c.content,
                       c.reply_suggestion, v.bvid, v.title, v.nickname AS video_account
                FROM comment_auto_task t
                JOIN bilibili_video_comment c ON c.id=t.src_pk
                LEFT JOIN bilibili_video v ON v.video_id=c.video_id
                WHERE t.status='pending' AND t.platform='B站'
                  AND t.action_type IN ('reply', 'delete')
                ORDER BY t.id LIMIT %s
            """, (limit,))
            return cur.fetchall()

def fetch_web_tasks(limit, platform, table):
    with get_conn() as conn:
        with conn.cursor() as cur:
            content_join = " LEFT JOIN zhihu_content z ON z.content_id=c.content_id" if table == 'zhihu_comment' else ''
            content_field = ", COALESCE(NULLIF(c.content_url, ''), z.content_url) AS reply_url" if table == 'zhihu_comment' else ''
            # XHS browser automation supports the native delete flow as well;
            # Zhihu remains reply-only until it has an equivalent verified path.
            action_filter = "IN ('reply', 'delete')" if platform == '小红书' else "='reply'"
            cur.execute(f"""SELECT t.id AS task_id, t.*, c.*{content_field} FROM comment_auto_task t JOIN `{table}` c ON c.id=t.src_pk{content_join}
                WHERE t.status='pending' AND t.platform=%s AND t.action_type {action_filter} ORDER BY t.id LIMIT %s""", (platform, limit))
            return cur.fetchall()

def call_web_reply(task, dry_run=False):
    action = str(task.get('action_type') or 'reply').strip().lower()
    message = str(task.get('reply_suggestion') or '').strip()
    if action != 'delete' and (not message or message.startswith('⚠ 分析失败')): raise RuntimeError('缺少可发送的推荐回复')
    if dry_run: return {'success': True, 'status': 'dry-run'}
    if task['platform'] == '小红书':
        # XHS is browser-only in this deployment. Do not allow an inherited
        # environment value or an old worker to silently fall back to the API.
        browser_only = True
        from xhs_reply import reply_api
        account = None
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT cookie,凭据 FROM `平台运营账号` WHERE 平台='小红书' AND 昵称=%s", (task.get('account_nickname'),))
                account = cur.fetchone()
        if account and account.get('cookie') and account.get('凭据') and not browser_only:
            meta = json.loads(account['凭据']) if isinstance(account['凭据'], str) else account['凭据']
            cookies = []
            for part in str(account['cookie']).split(';'):
                if '=' in part:
                    name, value = part.strip().split('=', 1)
                    cookies.append({'name': name, 'value': value, 'domain': '.xiaohongshu.com', 'path': '/'})
            result = asyncio.run(reply_api(str(task.get('note_id')), message,
                storage_state=json.dumps({'cookies': cookies}),
                user_agent=str(meta.get('user_agent') or ''),
                target_comment_id=str(task.get('comment_id') or ''),
                xsec_token=str(task.get('note_url') or '').split('xsec_token=')[-1].split('&')[0] if 'xsec_token=' in str(task.get('note_url') or '') else ''))
            if result.get('status') != 'success': raise RuntimeError(result.get('error') or '小红书 API 回复失败')
            return result
        from xhs_reply import reply_browser
        browser_id = str(task.get('bit_window'))
        if browser_id.isdigit():
            rows = (requests.post(f"{BIT_API}/browser/list", json={"page": 0, "pageSize": 200}, timeout=30).json().get("data") or {}).get("list") or []
            match = next((r for r in rows if str(r.get("seq")) == browser_id), None)
            if match: browser_id = str(match.get("id"))
        # Match the Douyin browser flow: open the profile and preflight its
        # CDP targets before the Node worker attaches. XHS pages often leave a
        # stale note-detail renderer behind; Playwright can otherwise connect
        # to the websocket but hang while enumerating that target.
        cdp = {}
        cdp_http = ""
        try:
            opened = requests.post(
                f"{BIT_API}/browser/open", json={"id": browser_id}, timeout=60
            )
            opened.raise_for_status()
            cdp = opened.json().get("data") or {}
            cdp_http = str(cdp.get("http") or "").strip()
            if cdp_http and "://" not in cdp_http:
                cdp_http = "http://" + cdp_http
            preflight_ok = False
            if cdp_http:
                try:
                    subprocess.run(
                        ["pwsh", "-NoProfile", "-File", str(BASE_DIR / "bitbrowser-cdp-check.ps1"),
                         "-Port", cdp_http.rsplit(":", 1)[-1], "-CloseDead"],
                        cwd=str(BASE_DIR), capture_output=True, text=True,
                        timeout=30, check=True,
                    )
                    preflight_ok = True
                except Exception:
                    pass
                if not preflight_ok:
                    _reset_cdp_page_targets(cdp_http)
        except Exception:
            # The Node worker still performs the authoritative open/connect;
            # this diagnostic step must not hide its actual error.
            pass
        browser_payload = {
            'action': action,
            'noteId': str(task.get('note_id')),
            'content': message,
            'browserId': browser_id,
            'targetCommentId': str(task.get('comment_id') or ''),
            'targetText': str(task.get('content') or ''),
            'xsecToken': str(task.get('note_url') or '').split('xsec_token=')[-1].split('&')[0] if 'xsec_token=' in str(task.get('note_url') or '') else '',
            'timeoutSeconds': 120,
            'bitApiUrl': BIT_API,
            'cdpWebSocket': str((cdp or {}).get('ws') or ''),
            'cdpHttp': cdp_http,
        }
        # Run Playwright in a fresh main process. The dispatcher itself runs
        # platform tasks in a background thread, which can trigger WinError 5
        # when asyncio creates Playwright's Windows overlapped pipes.
        worker_script = str(BASE_DIR / 'douyin-creator-tools' / 'xhs-browser-reply.mjs')
        proc = subprocess.run(
            ['node', worker_script],
            input=json.dumps(browser_payload, ensure_ascii=False),
            # CDP attachment may use the shared 180s connection budget; allow
            # the worker to return a stage-specific error instead of killing
            # it at 150s with only an opaque subprocess timeout.
            text=True, capture_output=True, timeout=240,
            cwd=str(BASE_DIR / 'douyin-creator-tools')
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or '小红书浏览器子进程失败')[-1000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        if result.get('status') != 'success': raise RuntimeError(result.get('error') or '小红书回复失败')
        return result
    from zhihu_reply import reply_comment
    browser_id = str(task.get('bit_window'))
    if browser_id.isdigit():
        rows = (requests.post(f"{BIT_API}/browser/list", json={"page": 0, "pageSize": 200}, timeout=30).json().get("data") or {}).get("list") or []
        match = next((r for r in rows if str(r.get("seq")) == browser_id), None)
        if match: browser_id = str(match.get("id"))
    url = str(task.get('reply_url') or task.get('content_url') or '').strip()
    if not url:
        raise RuntimeError('缺少知乎帖子链接，请补充 content_url')
    zhihu_kwargs = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cookie FROM `平台运营账号` WHERE 平台='知乎' AND 昵称=%s", (task.get('account_nickname'),))
            account = cur.fetchone()
    if account and account.get('cookie'):
        items = []
        for part in str(account['cookie']).split(';'):
            if '=' in part:
                name, value = part.strip().split('=', 1)
                items.append({'name': name, 'value': value, 'domain': '.zhihu.com', 'path': '/'})
        os.environ['ZHIHU_COOKIES_JSON'] = json.dumps(items, ensure_ascii=False)
    result = reply_comment(url, str(task.get('user_nickname') or ''), message,
                           target_content=str(task.get('content') or ''), **zhihu_kwargs)
    if not result.get('success'): raise RuntimeError(result.get('message') or '知乎回复失败')
    return result

def process_web(platform, table, limit, dry_run=False):
    tasks = fetch_web_tasks(limit, platform, table)
    for task in tasks:
        set_task_status(task['task_id'], 'running')
        health_check_ok = False
        try:
            call_web_reply(task, dry_run)
            if not dry_run: mark_comment_handled(task['comment_id'], table)
            set_task_status(task['task_id'], 'success')
            print(f"  success task={task['task_id']} platform={platform}")
        except Exception as exc:
            set_task_status(task['task_id'], 'failed', str(exc)); print(f"  failed task={task['task_id']} {exc}")
    return len(tasks)


def set_task_status(task_id, status, error=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if status == "running":
                cur.execute("""
                    UPDATE comment_auto_task SET status='running', started_ts=%s, error_message=NULL
                    WHERE id=%s
                """, (int(time.time()), task_id))
            elif status in ("success", "failed", "uncertain"):
                cur.execute("""
                    UPDATE comment_auto_task SET status=%s, finished_ts=%s, error_message=%s
                    WHERE id=%s
                """, (status, int(time.time()), (error or "")[:1000] if error else None, task_id))
        conn.commit()


def mark_comment_handled(comment_id, table="douyin_aweme_comment"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE `{table}`
                SET handled=1, last_modify_ts=UNIX_TIMESTAMP()
                WHERE comment_id=%s AND handled=0
            """, (comment_id,))
            n = cur.rowcount
        conn.commit()
    return n



def get_douyin_cookie(account_name="", bit_window=""):
    target = normalize_account_name(account_name)
    bit = str(bit_window or "").strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT `昵称` AS account_name, `cookie` AS cookies, `Bit窗口` AS bit_window
                FROM `平台运营账号`
                WHERE `平台`='抖音' AND COALESCE(`cookie`, '') <> ''
                ORDER BY (`昵称`=%s) DESC, (`Bit窗口`=%s) DESC, `昵称`
            """, (str(account_name or "").strip(), bit))
            rows = cur.fetchall()
            for row in rows:
                row_name = normalize_account_name(row.get("account_name"))
                if target and (row_name == target or row_name in target or target in row_name):
                    return row
                if bit and str(row.get("bit_window") or "").strip() == bit:
                    return row
            cur.execute("""
                SELECT id, account_name, cookies
                FROM crawler_cookies_account
                WHERE platform_name IN ('dy', '抖音', 'douyin') AND COALESCE(cookies, '') <> ''
                ORDER BY update_time DESC
            """)
            cookie_rows = cur.fetchall()
    if not cookie_rows:
        raise RuntimeError("缺少抖音cookie，请先同步账号cookie")
    for row in cookie_rows:
        name = normalize_account_name(row.get("account_name"))
        if target and (target in name or name in target):
            return row
        if bit and bit in str(row.get("account_name") or ""):
            return row
    return cookie_rows[0]


def _find_bitbrowser_window(bit_window):
    """Resolve the stored BitBrowser sequence number to its stable window ID."""
    seq = str(bit_window or "").strip()
    if not seq:
        raise RuntimeError("抖音任务缺少Bit窗口，无法读取浏览器身份")
    page = 0
    while True:
        response = requests.post(
            f"{BIT_API}/browser/list", json={"page": page, "pageSize": 100}, timeout=20
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        rows = data.get("list") or []
        match = next((row for row in rows if str(row.get("seq")) == seq), None)
        if match:
            return match
        total = int(data.get("totalNum") or 0)
        if not rows or (page + 1) * 100 >= total:
            break
        page += 1
    raise RuntimeError(f"未找到Bit窗口 {seq}")


def _read_bitbrowser_identity(debug_address):
    """Read the active profile's JS-visible browser identity over local CDP."""
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，无法读取Bit窗口浏览器身份") from exc

    address = str(debug_address or "").strip().removeprefix("http://").removeprefix("https://")
    if not address:
        raise RuntimeError("Bit窗口未提供调试地址")
    listing = requests.get(f"http://{address}/json/list", timeout=15).json()
    pages = [item for item in listing if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("Bit窗口没有可读取的页面")
    # The profile settings are shared by pages.  Prefer an ordinary page over
    # BitBrowser's own workbench tab, which can have a different document host.
    page = next((item for item in pages if "bitbrowser" not in str(item.get("url") or "")), pages[0])

    async def read():
        expression = (
            "JSON.stringify({ua:navigator.userAgent,locale:navigator.language,"
            "width:screen.width,height:screen.height})"
        )
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=1024 * 1024) as socket:
            await socket.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if message.get("id") == 1:
                    value = ((message.get("result") or {}).get("result") or {}).get("value")
                    if not value:
                        raise RuntimeError("Bit窗口未返回浏览器身份")
                    return json.loads(value)

    try:
        result = asyncio.run(read())
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"读取Bit窗口浏览器身份失败: {type(exc).__name__}") from exc
    ua = str(result.get("ua") or "").strip()
    locale = str(result.get("locale") or "").strip()
    try:
        width, height = int(result.get("width")), int(result.get("height"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Bit窗口返回的屏幕尺寸无效") from exc
    if "Chrome/" not in ua or not locale or width < 320 or height < 240:
        raise RuntimeError("Bit窗口返回的浏览器身份不完整")
    return {"ua": ua, "locale": locale, "width": width, "height": height}


def get_douyin_browser_identity(bit_window):
    """Open the account profile if needed and return its actual request identity."""
    seq = str(bit_window or "").strip()
    if seq in _DOUYIN_BROWSER_IDENTITY_CACHE:
        return _DOUYIN_BROWSER_IDENTITY_CACHE[seq]
    window_id = None
    try:
        window = _find_bitbrowser_window(seq)
        window_id = window.get("id")
        opened = requests.post(
            f"{BIT_API}/browser/open", json={"id": window_id}, timeout=60
        )
        opened.raise_for_status()
        payload = opened.json()
        if not payload.get("success") or not (payload.get("data") or {}).get("http"):
            raise RuntimeError(str(payload.get("msg") or "打开Bit窗口失败"))
        identity = _read_bitbrowser_identity(payload["data"]["http"])
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"读取Bit窗口 {seq} 身份失败: {type(exc).__name__}") from exc
    finally:
        # 环境指纹已从真实窗口读取完毕，直连模式不应把 BitBrowser 窗口
        # 长时间留在打开状态。网页回复模式由 Node 脚本在任务结束时关闭。
        if window_id:
            try:
                requests.post(f"{BIT_API}/browser/close", json={"id": window_id}, timeout=30)
            except Exception as exc:
                logging.getLogger(__name__).warning("关闭Bit窗口 %s 失败: %s", seq, type(exc).__name__)
    _DOUYIN_BROWSER_IDENTITY_CACHE[seq] = identity
    return identity


def _load_douyin_commenter():
    """Load the supplied direct-comment client without requiring BitBrowser.

    The bundle is deliberately imported only when a Douyin task runs, keeping
    the rest of the SCRM service usable if its optional HTTP dependency has not
    been installed yet.
    """
    root = str(DOUYIN_COMMENTER_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from douyin_commenter import DouyinClient
        return DouyinClient
    except ImportError as exc:
        raise RuntimeError(
            "抖音直连回复组件不可用，请安装 curl_cffi 后重试：" + str(exc)
        ) from exc


async def _post_douyin_reply_direct(task, cookie_header, browser_identity):
    """Send exactly one reply through the supplied signed Web API client.

    A write result that cannot be confirmed is intentionally *not* retried.
    The caller maps it to ``uncertain`` so an operator can check the comment
    thread before deciding whether to retry.
    """
    message = str(task.get("reply_suggestion") or "").strip()
    aweme_id = str(task.get("aweme_id") or task.get("content_id") or "").strip()
    comment_id = str(task.get("comment_id") or "").strip()
    if not message or message.startswith("⚠ 分析失败"):
        return {"replyPlanId": comment_id, "status": "failed", "error": "缺少可发送的推荐回复"}
    if not aweme_id or not comment_id:
        return {"replyPlanId": comment_id, "status": "failed", "error": "缺少作品 ID 或目标评论 ID"}
    submitted = False
    try:
        client_type = _load_douyin_commenter()
        client = client_type(
            cookie_header, browser_identity["ua"], timeout=25,
            locale=browser_identity["locale"],
            screen_width=browser_identity["width"],
            screen_height=browser_identity["height"],
        )
        async with client.session_scope():
            # From this point a transport failure cannot prove that the
            # platform did not persist the reply.  Preserve that distinction
            # for the dispatcher instead of allowing a second send path.
            submitted = True
            ok, sent_comment_id, error = await client.post_comment(
                aweme_id, message, reply_comment_id=comment_id
            )
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        category = _classify_douyin_reply_error(error)
        return {
            "replyPlanId": comment_id,
            "status": "uncertain" if submitted else "failed",
            "error": ("API 调用后状态未知，请核对，勿直接重发：" if submitted else "API 调用前失败：") + error,
            "failure_category": category,
        }
    if ok:
        return {
            "replyPlanId": comment_id,
            "status": "replied",
            "sentCommentId": sent_comment_id,
        }
    category = _classify_douyin_reply_error(error)
    return {
        "replyPlanId": comment_id,
        "status": "uncertain" if str(error).startswith("write_uncertain:") else "failed",
        "error": error or "抖音未返回可确认的发表结果",
        "failure_category": category,
    }


def _classify_douyin_reply_error(error):
    """Return an auditable category without relying on a successful HTTP code.

    The signed client intentionally retains a short response snippet for an
    edge rejection, so classify the complete string rather than only its
    prefix.  Categories mirror the reply adapter contract and are safe to
    show in task evidence; no cookies or request payloads are included.
    """
    text = str(error or "").lower()
    if any(marker in text for marker in ("cookie", "登录", "session", "logged_out", "auth")):
        return "auth"
    if any(marker in text for marker in ("验证码", "风控", "频控", "captcha", "verify", "429", "412")):
        return "risk_control"
    if "http_403" in text:
        return "request_rejected"
    if any(marker in text for marker in ("timeout", "timed out", "超时", "连接", "network", "empty_body", "invalid_json", "http_5")):
        return "network"
    return "platform"


def _run_douyin_browser_plans_unlocked(tasks, cookie_header, account_name):
    """Run reply/delete plans through the existing headless creator-center flow.

    Deletion is deliberately not sent through the signed comment API: the
    browser implementation verifies the target comment, clicks the native
    delete menu/confirmation, and confirms that the row disappeared.
    """
    plans = []
    for task in tasks:
        action = "delete" if task.get("action_type") == "delete" else "reply"
        plans.append({
            "id": str(task.get("comment_id") or ""),
            "awemeId": str(task.get("aweme_id") or task.get("content_id") or ""),
            "workTitle": task.get("title") or "",
            "commentText": task.get("content") or "",
            "action": action,
            "replyMessage": task.get("reply_suggestion") or "",
        })
    if any(not p["id"] or not p["awemeId"] or not p["workTitle"] for p in plans):
        raise RuntimeError("抖音网页任务缺少评论ID、作品ID或作品标题")
    bit_window = str(tasks[0].get("bit_window") or "").strip()
    window_id = None
    opened_window = False
    cdp = None
    if bit_window:
        window = _find_bitbrowser_window(bit_window)
        window_id = window.get("id")
        opened = requests.post(f"{BIT_API}/browser/open", json={"id": window_id}, timeout=60)
        opened.raise_for_status()
        payload = opened.json()
        cdp = payload.get("data") or {}
        if not cdp.get("ws"):
            raise RuntimeError("Bit窗口未返回CDP地址，无法执行网页回退")
        if cdp.get("http") and "://" not in str(cdp["http"]):
            cdp["http"] = "http://" + str(cdp["http"])
        if cdp.get("ws") and "://" not in str(cdp["ws"]):
            cdp["ws"] = "ws://" + str(cdp["ws"])
        # Playwright 初始化 CDP 时会枚举窗口内所有 page target；任一死渲染器
        # 都会让连接长期等待。先用现有健康检查关闭死 target，失败也不阻断
        # 后续连接（健康检查本身是保护性步骤）。
        try:
            port = str(cdp["http"]).rsplit(":", 1)[-1]
            check_script = BASE_DIR / "bitbrowser-cdp-check.ps1"
            subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(check_script), "-Port", port, "-CloseDead"],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30,
                check=True,
            )
            health_check_ok = True
        except Exception:
            pass
        if not health_check_ok:
            _reset_cdp_page_targets(cdp.get("http"))
        opened_window = True
    work_dir = DOUYIN_BROWSER_TOOL_ROOT / "comments-output"
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"auto-task-{os.getpid()}-{time.time_ns()}"
    input_path = work_dir / f"{stem}-input.json"
    output_path = work_dir / f"{stem}-output.json"
    try:
        input_path.write_text(json.dumps({
            "account": {"account_name": account_name or ""},
            "cookieHeader": cookie_header,
            "plans": plans,
            "bitWindow": bit_window,
            "bitBrowserId": window_id,
            "cdpWebSocket": cdp.get("ws") if cdp else None,
            "cdpHttp": cdp.get("http") if cdp else None,
        }, ensure_ascii=False), encoding="utf-8")
        command = ["node", "db-auto-reply.mjs", str(input_path)]
        if cdp:
            # 受限 Windows 环境不能创建新的 Chromium 进程；复用原 BitBrowser
            # CDP 环境既避免 spawn EPERM，也保持账号原有 Cookie/指纹。
            command.append("--use-cdp")
        command.extend(["--headless", "--out", str(output_path)])
        browser_env = os.environ.copy()
        browser_env["SKIP_CDP_PREFLIGHT"] = "1"
        # db-auto-reply 的评论采集本身允许 120 秒；原来的 90 秒外层
        # 超时会在采集尚未结束时强行杀掉 Node，表现为“无匹配”或空结果。
        # 外层只负责兜底，给导航、作品切换和评论滚动留出缓冲。
        browser_timeout = max(
            int(os.getenv("DB_REPLY_TIMEOUT_SECONDS", "210")),
            180,
        )
        try:
            result = subprocess.run(
                command,
                cwd=str(DOUYIN_BROWSER_TOOL_ROOT), env=browser_env,
                capture_output=True, text=True, timeout=browser_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_tail = str(exc.stdout or "")[-1200:]
            stderr_tail = str(exc.stderr or "")[-1200:]
            detail = "网页回复脚本超时（{}秒）".format(browser_timeout)
            if stdout_tail or stderr_tail:
                detail += "\n" + (stdout_tail or stderr_tail)
            raise RuntimeError(detail) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "网页删除脚本失败").strip()[-500:]
            raise RuntimeError(detail)
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("网页删除脚本未返回结果") from exc
    finally:
        # Cleanup is best-effort; it must not turn a confirmed browser result
        # into a task failure on Windows.
        for path in (input_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if opened_window and window_id:
            try:
                requests.post(f"{BIT_API}/browser/close", json={"id": window_id}, timeout=30)
            except Exception:
                pass
    return payload.get("results") or []


def _run_douyin_browser_plans(tasks, cookie_header, account_name):
    """Serialize CDP use per BitBrowser profile.

    A second task for the same profile used to close the window while the
    first Playwright process was navigating, producing the misleading
    ``Target page, context or browser has been closed`` error.
    """
    window_id = str(tasks[0].get("bit_window") or "") if tasks else ""
    with _bit_window_lock(window_id):
        return _run_douyin_browser_plans_unlocked(tasks, cookie_header, account_name)


def _run_douyin_browser_delete(tasks, cookie_header, account_name):
    """Compatibility wrapper for the browser delete path."""
    return _run_douyin_browser_plans(tasks, cookie_header, account_name)


def run_douyin_reply(tasks, dry_run=False):
    """Reply through the signed API using the Cookie profile's real identity.

    A reply is sent through exactly one write path per execution.  In
    particular, an API rejection is reported for review rather than silently
    retried through the browser, since an intermediary response is not a
    sufficient proof that no comment was created.
    """
    if not tasks:
        return []
    first = tasks[0]
    cookie_row = get_douyin_cookie(first.get("account_nickname"), first.get("bit_window"))
    cookie_header = cookie_row.get("cookies") or ""
    if "sessionid" not in cookie_header:
        raise RuntimeError("抖音cookie缺少sessionid，请重新同步cookie")
    if dry_run:
        return [
            {"replyPlanId": str(task.get("comment_id") or ""), "status": "dry_run"}
            for task in tasks
        ]
    # Explicit browser-only mode for accounts/tasks that must avoid the
    # signed comment API. The existing browser flow verifies the target
    # comment and submits through the creator-center UI.
    # Browser/CDP is the safe default for the current deployment. An explicit
    # DOUYIN_REPLY_MODE=api is required to opt back into signed API writes.
    browser_only = str(os.getenv("DOUYIN_REPLY_MODE", "browser")).strip().lower() in {
        "browser", "web", "ui"
    }
    if browser_only:
        return _run_douyin_browser_plans(
            tasks,
            cookie_header,
            first.get("account_nickname")
        )
    delete_tasks = [task for task in tasks if task.get("action_type") == "delete"]
    reply_tasks = [task for task in tasks if task.get("action_type") != "delete"]
    results = []
    if delete_tasks:
        results.extend(_run_douyin_browser_delete(
            delete_tasks, cookie_header, first.get("account_nickname")
        ))
    if not reply_tasks:
        return results
    # 同一账号/Bit窗口的任务共享一次真实环境读取；发送仍保持串行，
    # 避免同一 profile 并发造成请求节奏异常或重复提交。
    identity = get_douyin_browser_identity(first.get("bit_window"))
    for task in reply_tasks:
        result = asyncio.run(_post_douyin_reply_direct(task, cookie_header, identity))
        results.append(result)
    return results



def parse_cookie_value(cookie_header, name):
    for part in str(cookie_header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return ""


def normalize_account_name(name):
    return re.sub(r"[\s～~_-]+", "", str(name or "").strip()).lower()


def get_bilibili_cookie(account_name=""):
    target = normalize_account_name(account_name)
    if not target:
        raise RuntimeError("缺少B站所属账号，无法选择cookie")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT `昵称` AS account_name, `cookie` AS cookies, `Bit窗口` AS bit_window
                FROM `平台运营账号`
                WHERE `平台`='B站' AND COALESCE(`cookie`, '') <> ''
                ORDER BY (`昵称`=%s) DESC, `昵称`
            """, (str(account_name or "").strip(),))
            rows = cur.fetchall()
    for row in rows:
        row_name = normalize_account_name(row.get("account_name"))
        if row_name == target or row_name in target or target in row_name:
            return row
    raise RuntimeError(f"所属B站账号 {account_name} 缺少cookie，请先配置Bit窗口并同步cookie")


def bilibili_headers(cookie, bvid=""):
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/"
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie,
    }


def call_bilibili(task, dry_run=False):
    cookie_row = get_bilibili_cookie(task.get("video_account") or task.get("account_nickname"))
    cookie = cookie_row.get("cookies") or ""
    csrf = parse_cookie_value(cookie, "bili_jct")
    if not csrf:
        raise RuntimeError("B站cookie缺少bili_jct")
    oid = str(task.get("video_id") or task.get("content_id") or "").strip()
    rpid = str(task.get("comment_id") or "").strip()
    if not oid or not rpid:
        raise RuntimeError("缺少B站评论ID或视频ID")
    if dry_run:
        return {"code": 0, "message": "dry-run"}
    data = {"oid": oid, "type": "1", "csrf": csrf}
    if task["action_type"] == "delete":
        url = "https://api.bilibili.com/x/v2/reply/del"
        data["rpid"] = rpid
    else:
        message = str(task.get("reply_suggestion") or "").strip()
        if not message or message.startswith("⚠ 分析失败"):
            raise RuntimeError("缺少可发送的推荐回复")
        url = "https://api.bilibili.com/x/v2/reply/add"
        root = str(task.get("parent_comment_id") or "").strip() or rpid
        data.update({
            "message": message,
            "plat": "1",
            "root": root,
            "parent": rpid,
        })
    resp = _DIRECT_HTTP.post(url, headers=bilibili_headers(cookie, task.get("bvid")), data=data, timeout=30)
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"B站接口返回非JSON: HTTP {resp.status_code} {resp.text[:200]}")
    if payload.get("code") != 0:
        raise RuntimeError(f"B站接口失败 code={payload.get('code')} message={payload.get('message')}")
    return payload


def process_bilibili(limit, dry_run=False):
    tasks = fetch_bilibili_tasks(limit)
    if not tasks:
        print("没有 pending B站自动操作任务")
        return 0
    print(f"处理 B站自动操作: {len(tasks)} 个任务")
    for task in tasks:
        set_task_status(task["id"], "running")
        try:
            call_bilibili(task, dry_run=dry_run)
            if not dry_run:
                mark_comment_handled(task["comment_id"], "bilibili_video_comment")
            set_task_status(task["id"], "success")
            print(f"  success task={task['id']} action={task['action_type']} comment_id={task['comment_id']}")
        except Exception as exc:
            set_task_status(task["id"], "failed", str(exc))
            print(f"  failed task={task['id']} {exc}")
    return len(tasks)

def process(limit, dry_run=False):
    process_bilibili(limit, dry_run=dry_run)
    process_web('小红书', 'xhs_note_comment', limit, dry_run=dry_run)
    process_web('知乎', 'zhihu_comment', limit, dry_run=dry_run)
    tasks = fetch_douyin_tasks(limit)
    if not tasks:
        print("没有 pending 抖音回复任务")
        return
    # 第一版按 Bit 窗口串行分组，避免同一浏览器窗口并发操作。
    groups = {}
    for task in tasks:
        # 不能只按窗口序号分组：账号配置被替换或历史数据异常时，
        # 同一个序号可能对应不同账号；按账号+窗口可避免串用 cookie。
        key = (normalize_account_name(task.get("account_nickname")), str(task.get("bit_window") or ""))
        groups.setdefault(key, []).append(task)
    for (account_key, bit_window), group in groups.items():
        print(f"处理账号 {account_key or '?'} / Bit窗口 {bit_window}: {len(group)} 个回复任务")
        for task in group:
            set_task_status(task["id"], "running")
        try:
            results = run_douyin_reply(group, dry_run=dry_run)
            success_ids = {
                str(r.get("replyPlanId")) for r in results
                if r.get("status") in (
                    "replied", "deleted", "dry_run", "dry_run_delete"
                )
            }
            unmatched_success_results = [
                r for r in results
                if r.get("status") in {"replied", "deleted", "dry_run", "dry_run_delete"}
            ]
            for task in group:
                match = next((r for r in results if str(r.get("replyPlanId")) == str(task["comment_id"])), None)
                # Older browser results could lose replyPlanId after a DOM
                # delete, even though the sole planned delete returned
                # ``deleted``. Associate that unambiguous result with the
                # only task in the group instead of recording error=deleted.
                if match is None and len(group) == 1 and len(results) == 1:
                    match = results[0]
                if match is None and unmatched_success_results:
                    # Some old browser paths returned a result without the
                    # original plan id. Keep result order as a safe fallback
                    # when the successful result is unambiguous for this task.
                    match = unmatched_success_results.pop(0)
                if (match and match.get("status") in {"replied", "deleted", "dry_run", "dry_run_delete"}) or str(task["comment_id"]) in success_ids:
                    if not dry_run:
                        mark_comment_handled(task["comment_id"])
                    set_task_status(task["id"], "success")
                    print(f"  success task={task['id']} comment_id={task['comment_id']}")
                else:
                    match = match or {}
                    error = match.get("error") or match.get("status") or "未返回成功结果"
                    status = "uncertain" if match.get("status") == "uncertain" else "failed"
                    set_task_status(task["id"], status, error)
                    print(f"  {status} task={task['id']} {error}")
        except Exception as exc:
            for task in group:
                set_task_status(task["id"], "failed", str(exc))
            print(f"  group failed: {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
