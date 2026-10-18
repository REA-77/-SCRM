# -*- coding: utf-8 -*-
"""Process queued comment auto-operation tasks.

First closed loop: Douyin reply tasks through the task's BitBrowser window.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

from db import get_conn

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR / "douyin-creator-tools"
INPUT_PATH = REPO_DIR / "comments-output" / "auto-task-reply-input.json"
OUTPUT_PATH = REPO_DIR / "comments-output" / "auto-task-reply-result.json"
BIT_API = os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")
HEADLESS_WINDOW_FILE = BASE_DIR / "bitbrowser_headless_windows.json"
HEADLESS_COMMENT_URL = "https://creator.douyin.com/creator-micro/interactive/comment"


def open_bit_window(bit_window):
    window_id = resolve_bit_window_id(bit_window)
    last_resp = None
    for attempt in range(1, 13):
        resp = requests.post(f"{BIT_API}/browser/open", json={"id": window_id}, timeout=60).json()
        last_resp = resp
        data = resp.get("data") or {}
        ws = data.get("ws")
        http = data.get("http")
        # BitBrowser 同时返回 http 和 ws 调试地址。优先使用原始 ws，
        # 避免 Playwright 先通过 HTTP 再二次解析 websocket 时卡在启动阶段。
        endpoint = ws or (f"http://{http}" if http else "")
        if endpoint:
            if attempt > 1:
                print(f"BitBrowser 第 {attempt} 次打开成功")
            return ws or endpoint, (f"http://{http}" if http else ""), window_id
        msg = str(resp.get("msg") or resp)
        if "正在关闭" in msg or "稍后" in msg or "启动" in msg:
            time.sleep(5)
            continue
        break
    raise RuntimeError(f"BitBrowser 未返回 ws: {last_resp}")


def resolve_bit_window_id(bit_window):
    value = str(bit_window).strip()
    if not value.isdigit():
        return value
    page = 0
    while True:
        resp = requests.post(
            f"{BIT_API}/browser/list", json={"page": page, "pageSize": 100}, timeout=30
        ).json()
        data = resp.get("data") or {}
        rows = data.get("list") or []
        for row in rows:
            if str(row.get("seq")) == value:
                return row["id"]
        total = data.get("totalNum") or len(rows)
        if (page + 1) * 100 >= total or not rows:
            break
        page += 1
    raise RuntimeError(f"未找到 BitBrowser 序号 {value} 对应的窗口")


def close_bit_window(bit_window):
    try:
        requests.post(f"{BIT_API}/browser/close", json={"id": str(bit_window)}, timeout=30)
    except Exception:
        pass


def _bit_json(path, payload, timeout=30):
    resp = requests.post(f"{BIT_API}{path}", json=payload, timeout=timeout)
    data = resp.json()
    if not data.get("success", True):
        raise RuntimeError(f"BitBrowser {path} 失败: {data.get('msg') or data}")
    return data


def _load_headless_window_map():
    try:
        value = json.loads(HEADLESS_WINDOW_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_headless_window_map(value):
    HEADLESS_WINDOW_FILE.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _clone_bit_window(source_id, account_name):
    """Create a private BitBrowser profile carrying the source profile's cookies.

    The original profile is never opened or closed by this helper.  The clone is
    opened with BitBrowser's headless flag and a single explicit comment page.
    """
    source = (_bit_json("/browser/detail", {"id": source_id}).get("data") or {})
    if not source:
        raise RuntimeError(f"BitBrowser 找不到源窗口 {source_id}")
    fingerprint = dict(source.get("browserFingerPrint") or {})
    for key in ("id", "seq", "browserId"):
        fingerprint.pop(key, None)
    clean = {
        "name": f"自动回复无头-{account_name or source.get('seq') or source_id}",
        "remark": f"自动回复专用，不影响原窗口{source.get('seq') or source_id}",
        "platform": "https://creator.douyin.com",
        "url": "",
        "cookie": source.get("cookie") or "",
        "browserFingerPrint": fingerprint,
        "workbench": "disable",
        "syncTabs": False,
        "syncCookies": False,
        "syncIndexedDb": False,
        "syncBookmarks": False,
        "syncHistory": False,
        "syncSessions": False,
        "syncLocalStorage": False,
    }
    for key in ("proxyMethod", "proxyType", "host", "port", "proxyUserName",
                "proxyPassword", "enableSocks5Udp", "ipCheckService", "isIpv6",
                "country", "province", "city"):
        if key in source and source.get(key) not in (None, ""):
            clean[key] = source[key]
    created = _bit_json("/browser/update", clean, timeout=60)
    data = created.get("data") or {}
    clone_id = data.get("id") or data.get("browserId")
    if not clone_id:
        raise RuntimeError(f"BitBrowser 创建无头窗口未返回id: {created}")
    return str(clone_id)


def _refresh_headless_cookie(clone_id, cookie_header):
    if not cookie_header:
        return
    try:
        detail = _bit_json("/browser/detail", {"id": clone_id}, timeout=20).get("data") or {}
        items = json.loads(detail.get("cookie") or "[]")
        by_name = {}
        for item in items:
            if item.get("name"):
                by_name.setdefault(item["name"], []).append(item)
        for part in str(cookie_header).split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            name = name.strip()
            if not name:
                continue
            matches = by_name.get(name) or []
            if matches:
                for item in matches:
                    item["value"] = value.strip()
            else:
                items.append({"name": name, "value": value.strip(), "domain": ".douyin.com", "path": "/"})
        _bit_json("/browser/update", {"id": clone_id, "cookie": json.dumps(items, ensure_ascii=False)}, timeout=60)
    except Exception as exc:
        print(f"BitBrowser 无头窗口 Cookie 同步警告: {exc}")


def open_headless_window(bit_window, account_name="", cookie_header=""):
    """Open/reuse a dedicated headless clone, leaving the source window intact."""
    source_id = resolve_bit_window_id(bit_window)
    key = f"{source_id}:{normalize_account_name(account_name) or 'default'}"
    mapping = _load_headless_window_map()
    clone_id = str(mapping.get(key) or "")
    if clone_id:
        try:
            _bit_json("/browser/detail", {"id": clone_id}, timeout=15)
        except Exception:
            clone_id = ""
    if not clone_id:
        clone_id = _clone_bit_window(source_id, account_name)
        mapping[key] = clone_id
        _save_headless_window_map(mapping)
    _refresh_headless_cookie(clone_id, cookie_header)

    last_resp = None
    for attempt in range(1, 13):
        resp = _bit_json("/browser/open", {
            "id": clone_id,
            "args": ["--headless"],
            "queue": True,
            "ignoreDefaultUrls": True,
            "newPageUrl": HEADLESS_COMMENT_URL,
        }, timeout=120)
        last_resp = resp
        data = resp.get("data") or {}
        ws, http = data.get("ws"), data.get("http")
        if ws or http:
            return ws or f"http://{http}", (f"http://{http}" if http else ""), clone_id
        msg = str(resp.get("msg") or resp)
        if any(word in msg for word in ("正在关闭", "稍后", "启动", "打开")):
            time.sleep(5)
            continue
        break
    raise RuntimeError(f"BitBrowser 无头窗口未返回调试地址: {last_resp}")


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
            cur.execute(f"""SELECT t.id AS task_id, t.*, c.*{content_field} FROM comment_auto_task t JOIN `{table}` c ON c.id=t.src_pk{content_join}
                WHERE t.status='pending' AND t.platform=%s AND t.action_type='reply' ORDER BY t.id LIMIT %s""", (platform, limit))
            return cur.fetchall()

def call_web_reply(task, dry_run=False):
    message = str(task.get('reply_suggestion') or '').strip()
    if not message or message.startswith('⚠ 分析失败'): raise RuntimeError('缺少可发送的推荐回复')
    if dry_run: return {'success': True, 'status': 'dry-run'}
    if task['platform'] == '小红书':
        from xhs_reply import reply_api
        account = None
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT cookie,凭据 FROM `平台运营账号` WHERE 平台='小红书' AND 昵称=%s", (task.get('account_nickname'),))
                account = cur.fetchone()
        if account and account.get('cookie') and account.get('凭据'):
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
        result = asyncio.run(reply_browser(str(task.get('note_id')), message,
            browser_id=browser_id, target_comment_id=str(task.get('comment_id') or ''),
            xsec_token=str(task.get('note_url') or '').split('xsec_token=')[-1].split('&')[0] if 'xsec_token=' in str(task.get('note_url') or '') else '',
            timeout_seconds=120))
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
            elif status in ("success", "failed"):
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


def sync_douyin_cookie_to_platform(account_name, bit_window, cookie):
    if not cookie:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            n = cur.execute("""
                UPDATE `平台运营账号`
                SET `cookie`=%s
                WHERE `平台`='抖音' AND (`昵称`=%s OR `Bit窗口`=%s)
            """, (cookie, str(account_name or "").replace("～", "").replace("~", ""), str(bit_window or "")))
        conn.commit()
    return n

def build_input(tasks, cookie_header, cdp_endpoint=""):
    plans = []
    for task in tasks:
        if not task.get("title"):
            raise RuntimeError(f"任务 {task['id']} 缺少作品标题，无法定位作品")
        plans.append({
            "id": str(task["comment_id"]),
            "awemeId": str(task["aweme_id"]),
            "workTitle": task["title"],
            "commentText": task.get("content") or "",
            "replyMessage": task.get("reply_suggestion") or "",
            "action": task.get("action_type") or "reply",
        })
    INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account": {"bit_window": tasks[0]["bit_window"], "account_name": tasks[0].get("account_nickname")},
        "cookieHeader": cookie_header,
        "plans": plans,
    }
    if cdp_endpoint:
        payload["cdpWebSocket"] = cdp_endpoint
    INPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_douyin_reply(tasks, dry_run=False):
    cookie_row = get_douyin_cookie(tasks[0].get("account_nickname"), tasks[0].get("bit_window"))
    cookie_header = cookie_row.get("cookies") or ""
    if "sessionid" not in cookie_header:
        raise RuntimeError("抖音cookie缺少sessionid，请重新同步cookie")
    sync_douyin_cookie_to_platform(tasks[0].get("account_nickname"), tasks[0].get("bit_window"), cookie_header)
    # 抖音复用比特浏览器的登录态，但使用独立的无头克隆，避免清理/卡住原窗口。
    cdp_endpoint, cdp_http, headless_window_id = open_headless_window(
        tasks[0].get("bit_window"), tasks[0].get("account_nickname") or "", cookie_header
    )
    try:
        build_input(tasks, cookie_header, cdp_endpoint)
        payload = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
        payload["cdpHttp"] = cdp_http
        payload["headlessBitWindow"] = headless_window_id
        INPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = ["node", "db-auto-reply.mjs", str(INPUT_PATH), "--use-cdp", "--out", str(OUTPUT_PATH)]
        if dry_run:
            cmd.append("--dry-run")
        try:
            result = subprocess.run(
                cmd,
                cwd=REPO_DIR,
                timeout=300,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("抖音自动回复超过300秒未返回，已停止等待")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if len(detail) > 1800:
                detail = detail[-1800:]
            raise RuntimeError(
                f"db-auto-reply.mjs 退出码 {result.returncode}"
                + (f"：{detail}" if detail else "")
            )
        output = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return output.get("results") or []
    finally:
        # 仅关闭本次创建/复用的无头克隆；原窗口和它的页面保持不动。
        close_bit_window(headless_window_id)



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
    resp = requests.post(url, headers=bilibili_headers(cookie, task.get("bvid")), data=data, timeout=30)
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
        groups.setdefault(task["bit_window"], []).append(task)
    for bit_window, group in groups.items():
        print(f"处理 Bit窗口 {bit_window}: {len(group)} 个回复任务")
        for task in group:
            set_task_status(task["id"], "running")
        try:
            results = run_douyin_reply(group, dry_run=dry_run)
            success_ids = {
                str(r.get("replyPlanId")) for r in results
                if r.get("status") in (
                    "replied", "deleted", "dry_run_delete",
                    "skipped_already_replied", "skipped_reply_ledger", "sent_unconfirmed"
                )
            }
            for task in group:
                if str(task["comment_id"]) in success_ids:
                    if not dry_run:
                        mark_comment_handled(task["comment_id"])
                    set_task_status(task["id"], "success")
                    print(f"  success task={task['id']} comment_id={task['comment_id']}")
                else:
                    match = next((r for r in results if str(r.get("replyPlanId")) == str(task["comment_id"])), {})
                    error = match.get("error") or match.get("status") or "未返回成功结果"
                    set_task_status(task["id"], "failed", error)
                    print(f"  failed task={task['id']} {error}")
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
