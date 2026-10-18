"""Read saved account cookies and test the browser reply path without sending."""
import asyncio
import json
from urllib.parse import urlsplit, parse_qs
from db import get_conn
from xhs_reply import reply_browser

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT cookie,凭据 FROM `平台运营账号` WHERE 平台='小红书' AND 昵称=%s", ('是小芋圆',))
        account = cur.fetchone()
        cur.execute('SELECT * FROM xhs_note_comment WHERE id=17')
        comment = cur.fetchone()
cookies = []
for part in account['cookie'].split(';'):
    if '=' in part:
        name, value = part.strip().split('=', 1)
        cookies.append(dict(name=name, value=value, domain='.xiaohongshu.com', path='/'))
result = asyncio.run(reply_browser(comment['note_id'], comment['reply_suggestion'],
    storage_state={'cookies': cookies}, user_agent=json.loads(account['凭据'])['user_agent'],
    target_comment_id=comment['comment_id'], target_text=comment['content'],
    xsec_token=parse_qs(urlsplit(comment['note_url']).query).get('xsec_token', [''])[0],
    timeout_seconds=60, dry_run=True))
print(json.dumps(result, ensure_ascii=False))
