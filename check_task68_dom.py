# -*- coding: utf-8 -*-
"""在窗口113打开目标视频页，从 DOM 里找目标评论并展开其子评论，读出回复内容。
绕开 comment/list/reply 接口风控（该接口持续返回空 body）。"""
import asyncio
import json
import os
import sys

for n in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(n, None)

sys.path.insert(0, r"D:\下载\数据库")

import requests
from comment_auto_worker import BIT_API

AWEME = "7589999859524980026"

JS_FIND = r"""
(async () => {
  const TARGET = "小白，注意隐私";
  // 先尝试点「评论」tab 展开评论区（部分页面默认收起）
  const tabs = [...document.querySelectorAll('div, span, p')].filter(e => e.textContent.trim() === '评论' && e.children.length === 0);
  if (tabs.length) { tabs[0].click(); await new Promise(r => setTimeout(r, 3000)); }
  // 全文找包含目标文本的最小元素
  const all = document.querySelectorAll('div, span, p');
  let hits = 0;
  for (const e of all) {
    if (e.textContent.includes(TARGET)) hits++;
  }
  return JSON.stringify({hits, url: location.href, title: document.title.slice(0,60)});
})()
"""

JS_READ = r"""
(async () => {
  // 1) XHR（webmssdk 自动签名）拉子评论
  const replyData = await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "https://www.douyin.com/aweme/v1/web/comment/list/reply/?item_id=7589999859524980026&comment_id=7599140757425816369&cursor=0&count=50&item_type=0", true);
    xhr.onload = () => resolve({status: xhr.status, body: (xhr.responseText || "").slice(0, 1200)});
    xhr.onerror = () => resolve({error: "xhrerr"});
    xhr.timeout = 15000;
    xhr.ontimeout = () => resolve({error: "timeout"});
    xhr.send();
  });
  // 2) 同时点目标评论卡片的「回复」按钮，试图展开
  const TARGET = "小白，注意隐私";
  const all = [...document.querySelectorAll('div, span, p')];
  const inner = all.filter(e => e.textContent.includes(TARGET) &&
    ![...e.children].some(c => c.textContent.includes(TARGET)));
  let clickedText = "";
  if (inner.length) {
    let card = inner[0];
    for (let i = 0; i < 15 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.textContent.includes('9月前')) break;
    }
    const replyBtn = [...card.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.trim() === '回复')[0];
    if (replyBtn) { replyBtn.click(); await new Promise(r => setTimeout(r, 4000)); }
    clickedText = card.innerText.slice(0, 600);
  }
  return JSON.stringify({replyData, clickedText, bodyLen: document.body.innerText.length});
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
        print("窗口 174 多次重试仍打不开")
        return
    print("窗口已打开")
    http_addr = opened["data"]["http"]
    if "://" not in http_addr:
        http_addr = "http://" + http_addr
    try:
        vers = requests.get(f"{http_addr}/json/version", timeout=15).json()
        async with websockets.connect(vers["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024) as bws:
            await bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                       "params": {"url": "https://www.douyin.com/video/" + AWEME}}))
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

            async def ev(cid, expr):
                await bws.send(json.dumps({"id": cid, "method": "Runtime.evaluate",
                                           "params": {"expression": expr, "awaitPromise": True,
                                                      "returnByValue": True, "timeout": 20000},
                                           "sessionId": session_id}))
                while True:
                    m = json.loads(await asyncio.wait_for(bws.recv(), timeout=40))
                    if m.get("id") == cid:
                        rr = m.get("result", {})
                        if "exceptionDetails" in rr:
                            return "JS异常: " + json.dumps(rr["exceptionDetails"])[:300]
                        return rr.get("result", {}).get("value")

            print("等视频页加载 12 秒...")
            await asyncio.sleep(12)
            # 滚动让评论区加载
            await ev(3, "window.scrollTo(0, 800); 'ok'")
            await asyncio.sleep(4)
            r1 = await ev(4, JS_FIND)
            print("查找:", r1)
            await asyncio.sleep(4)
            r2 = await ev(5, JS_READ)
            print("读取:", r2)

            await bws.send(json.dumps({"id": 6, "method": "Target.closeTarget",
                                       "params": {"targetId": target_id}}))
    finally:
        requests.post(f"{BIT_API}/browser/close", json={"id": w["id"]}, timeout=180)


if __name__ == "__main__":
    asyncio.run(main())
