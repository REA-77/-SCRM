# -*- coding: utf-8 -*-
"""Process queued comment auto-operation tasks.

Douyin replies use the signed direct API while existing BitBrowser account
bindings remain available for login and Cookie synchronisation.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from db import get_conn

BASE_DIR = Path(__file__).resolve().parent
BIT_API = os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")

# The supplied ``douyin_commenter`` bundle is kept next to this application.
# It signs and sends the Web comment API directly, so a reply does not need a
# BitBrowser clone (or any additional BitBrowser window).
DOUYIN_COMMENTER_ROOT = BASE_DIR / "douyin_commenter_reference" / "douyin_commenter"
DEFAULT_DOUYIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
        response = requests.get('https://api.bilibili.com/x/v2/reply/reply', params={
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


async def _post_douyin_reply_direct(task, cookie_header):
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
    client_type = _load_douyin_commenter()
    client = client_type(cookie_header, DEFAULT_DOUYIN_UA, timeout=25)
    async with client.session_scope():
        ok, sent_comment_id, error = await client.post_comment(
            aweme_id, message, reply_comment_id=comment_id
        )
    if ok:
        return {
            "replyPlanId": comment_id,
            "status": "replied",
            "sentCommentId": sent_comment_id,
        }
    return {
        "replyPlanId": comment_id,
        "status": "uncertain" if str(error).startswith("write_uncertain:") else "failed",
        "error": error or "抖音未返回可确认的发表结果",
    }


def run_douyin_reply(tasks, dry_run=False):
    """Reply through the signed API; never create or open a BitBrowser window.

    Account-to-BitBrowser mapping remains the source of the stored Cookie, so
    existing accounts continue to work.  BitBrowser is not contacted during a
    send; it is only still used separately for login/Cookie synchronisation.
    """
    if not tasks:
        return []
    cookie_row = get_douyin_cookie(tasks[0].get("account_nickname"), tasks[0].get("bit_window"))
    cookie_header = cookie_row.get("cookies") or ""
    if "sessionid" not in cookie_header:
        raise RuntimeError("抖音cookie缺少sessionid，请重新同步cookie")
    if dry_run:
        return [
            {"replyPlanId": str(task.get("comment_id") or ""), "status": "dry_run"}
            for task in tasks
        ]
    return [asyncio.run(_post_douyin_reply_direct(task, cookie_header)) for task in tasks]



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
                    "replied", "dry_run"
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
