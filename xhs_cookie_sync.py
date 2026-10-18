"""从比特浏览器登录窗口通过原生 CDP 读取小红书 Cookie，并写入平台运营账号表。

用法: py -X utf8 xhs_cookie_sync.py --window 174 --account 是小芋圆
"""
import argparse, asyncio, json
import requests, websockets
from db import get_conn

BIT_API = 'http://127.0.0.1:54345'

def resolve_window(value):
    value = str(value)
    rows = (requests.post(f'{BIT_API}/browser/list', json={'page': 0, 'pageSize': 200}, timeout=60).json().get('data') or {}).get('list') or []
    row = next((r for r in rows if str(r.get('id')) == value or str(r.get('seq')) == value), None)
    if not row: raise RuntimeError(f'未找到比特窗口 {value}')
    return str(row['id'])

async def read_cdp(ws):
    async with websockets.connect(ws, max_size=50 * 1024 * 1024, open_timeout=120, close_timeout=10) as sock:
        await sock.send(json.dumps({'id': 1, 'method': 'Storage.getCookies'}))
        cookies = json.loads(await asyncio.wait_for(sock.recv(), 120)).get('result', {}).get('cookies', [])
        await sock.send(json.dumps({'id': 2, 'method': 'Browser.getVersion'}))
        version = json.loads(await asyncio.wait_for(sock.recv(), 30)).get('result', {})
    selected = {c['name']: c['value'] for c in cookies if 'xiaohongshu.com' in str(c.get('domain',''))}
    if not selected.get('a1'): raise RuntimeError('窗口中没有小红书 a1 Cookie，请先登录小红书')
    return '; '.join(f'{k}={v}' for k,v in selected.items()), version.get('userAgent','')

def main():
    p=argparse.ArgumentParser(); p.add_argument('--window',required=True); p.add_argument('--account',required=True); a=p.parse_args()
    wid=resolve_window(a.window)
    r=requests.post(f'{BIT_API}/browser/open',json={'id':wid},timeout=120).json()
    if not r.get('success'): raise RuntimeError(r.get('msg') or '打开窗口失败')
    cookie, ua=asyncio.run(read_cdp(r['data']['ws']))
    with get_conn() as c:
        with c.cursor() as x:
            x.execute("UPDATE `平台运营账号` SET `cookie`=%s, `凭据`=%s, `Bit窗口`=%s WHERE `平台`='小红书' AND `昵称`=%s", (cookie,json.dumps({'user_agent':ua},ensure_ascii=False),str(a.window),a.account))
            if not x.rowcount: raise RuntimeError('未找到对应的小红书平台账号')
        c.commit()
    print(f'已保存 {a.account} 的小红书 Cookie，长度={len(cookie)}，UA={ua}')

if __name__=='__main__': main()
