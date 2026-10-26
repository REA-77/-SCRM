# -*- coding: utf-8 -*-
"""在窗口174（瑞伊～本人）打开创作者中心评论管理页，
确认目标评论（小白，注意隐私）下是否已有我们的回复。"""
import asyncio
import json
import os
import sys

for n in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(n, None)

sys.path.insert(0, r"D:\下载\数据库")

import requests
from comment_auto_worker import BIT_API

JS = r"""
(async () => {
  const TARGET = "小白，注意隐私";
  const REPLY = "隐私保护必须拉满";
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // 1) 点「选择作品」
  const pickBtn = [...document.querySelectorAll('button, div, span')].find(e =>
    e.children.length === 0 && e.textContent.trim() === '选择作品');
  if (pickBtn) { pickBtn.click(); await sleep(4000); }

  // 2) 在搜索框输入作品标题并搜索
  const input = document.querySelector('input[placeholder*="搜索"], input[placeholder*="作品"]')
    || [...document.querySelectorAll('input')].find(i => i.offsetParent !== null);
  if (input) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, '新年快乐');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    await sleep(1500);
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
    await sleep(5000);
  }

  // 3) 点第一个作品项
  const workItem = [...document.querySelectorAll('div, li')].find(e =>
    e.textContent.includes('新年快乐') && e.textContent.includes('2026') === false && e.children.length > 0 && e.children.length < 8);
  if (workItem) { workItem.click(); await sleep(8000); }

  // 4) 滚动加载评论直到出现目标
  for (let i = 0; i < 10; i++) {
    if (document.body.innerText.includes(TARGET)) break;
    window.scrollBy(0, 3000);
    await sleep(2500);
  }

  const hasTarget = document.body.innerText.includes(TARGET);
  const hasReply = document.body.innerText.includes(REPLY);
  let ctx = "";
  let expanded = "";
  if (hasTarget) {
    const all = [...document.querySelectorAll('div, span, p, li')];
    const inner = all.filter(e => e.textContent.includes(TARGET) &&
      ![...e.children].some(c => c.textContent.includes(TARGET)));
    if (inner.length) {
      let card = inner[0];
      for (let i = 0; i < 20 && card.parentElement; i++) {
        card = card.parentElement;
        const t = card.innerText || "";
        if (t.length > 200 && t.includes(TARGET)) break;
      }
      ctx = card.innerText.slice(0, 400);
      // 点「查看1条回复」展开
      const viewBtn = [...card.querySelectorAll('*')].find(e =>
        e.children.length === 0 && /查看.*条回复/.test(e.textContent));
      if (viewBtn) {
        viewBtn.click();
        await sleep(5000);
        expanded = card.innerText.slice(0, 1000);
      }
    }
  }
  return JSON.stringify({hasTarget, hasReply, ctx, expanded});
})()
"""


def window_by_seq(seq):
    page = 0
    while True:
        r = requests.post(f"{BIT_API}/browser/list", json={"page": page, "pageSize": 100}, timeout=60)
        data = r.json().get("data") or {}
        for w in data.get("list") or []:
            if str(w.get("seq")) == str(seq):
                return w
        total = int(data.get("totalNum") or 0)
        if not data.get("list") or (page + 1) * 100 >= total:
            return None
        page += 1


async def main():
    import websockets
    w = window_by_seq(174)
    opened = None
    for attempt in range(4):
        r = requests.post(f"{BIT_API}/browser/open", json={"id": w["id"]}, timeout=180)
        j = r.json()
        if j.get("success"):
            opened = j
            break
        print(f"打开失败(第{attempt+1}次): {j.get('msg')}，等 25 秒重试...")
        await asyncio.sleep(25)
    if not opened:
        print("窗口打不开")
        return
    print("窗口已打开")
    http_addr = opened["data"]["http"]
    if "://" not in http_addr:
        http_addr = "http://" + http_addr
    try:
        vers = requests.get(f"{http_addr}/json/version", timeout=15).json()
        async with websockets.connect(vers["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024) as bws:
            await bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                       "params": {"url": "https://creator.douyin.com/creator-micro/interactive/comment"}}))
            target_id = None
            while True:
                m = json.loads(await asyncio.wait_for(bws.recv(), timeout=30))
                if m.get("id") == 1:
                    target_id = m["result"]["targetId"]; break
            await bws.send(json.dumps({"id": 2, "method": "Target.attachToTarget",
                                       "params": {"targetId": target_id, "flatten": True}}))
            session_id = None
            while True:
                m = json.loads(await asyncio.wait_for(bws.recv(), timeout=30))
                if m.get("id") == 2:
                    session_id = m["result"]["sessionId"]; break

            async def ev(cid, expr, timeout=120):
                await bws.send(json.dumps({"id": cid, "method": "Runtime.evaluate",
                                           "params": {"expression": expr, "awaitPromise": True,
                                                      "returnByValue": True, "timeout": 90000},
                                           "sessionId": session_id}))
                while True:
                    m = json.loads(await asyncio.wait_for(bws.recv(), timeout=timeout))
                    if m.get("id") == cid:
                        rr = m.get("result", {})
                        if "exceptionDetails" in rr:
                            return "JS异常: " + json.dumps(rr["exceptionDetails"])[:300]
                        return rr.get("result", {}).get("value")

            print("等创作者中心加载 15 秒...")
            await asyncio.sleep(15)
            r = await ev(3, JS)
            print("结果:", r)
            await bws.send(json.dumps({"id": 4, "method": "Target.closeTarget",
                                       "params": {"targetId": target_id}}))
    finally:
        requests.post(f"{BIT_API}/browser/close", json={"id": w["id"]}, timeout=180)


if __name__ == "__main__":
    asyncio.run(main())
