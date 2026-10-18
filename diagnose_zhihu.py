import json
import os
import sys
from pathlib import Path
from db import get_conn

sys.path.insert(0, str(Path(__file__).parent / 'zhihu-mcp-master'))
from browser.manager import create_browser

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT cookie FROM `平台运营账号` WHERE 平台='知乎' AND 昵称=%s", ('知乎用户btGzJU',))
        cookie = cur.fetchone()['cookie']
items = []
for part in cookie.split(';'):
    if '=' in part:
        name, value = part.strip().split('=', 1)
        items.append(dict(name=name, value=value, domain='.zhihu.com', path='/'))
os.environ['ZHIHU_COOKIES_JSON'] = json.dumps(items)
with create_browser(headless=True) as (_, context, page):
    page.goto('https://zhuanlan.zhihu.com/p/2090090908129804744', wait_until='domcontentloaded', timeout=120000)
    page.wait_for_timeout(4000)
    print('PAGE', page.url)
    print(page.locator('body').inner_text()[-12000:])
    buttons = page.get_by_role('button').filter(has_text='评论')
    if buttons.count():
        buttons.first.click()
        page.wait_for_timeout(2000)
    match = page.get_by_text('我在看', exact=True)
    print('MATCHES', match.count())
    if match.count():
        parent = match.first
        for _ in range(8):
            parent = parent.locator('xpath=..')
            if parent.get_by_text('回复', exact=True).count():
                print('TARGET', parent.evaluate('(e)=>e.outerHTML'))
                parent.get_by_text('回复', exact=True).first.click()
                page.wait_for_timeout(1000)
                break
    print('INPUTS', page.locator('textarea,input,[contenteditable=true]').evaluate_all('(es)=>es.map(e=>e.outerHTML)'))
    print('BUTTONS', page.get_by_role('button').all_text_contents())
