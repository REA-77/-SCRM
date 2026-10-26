# -*- coding: utf-8 -*-
"""确认 task#68 的目标评论下那条子评论（reply_comment_total=1）是不是本次要发的回复。
在窗口174的视频页用 XHR 读 comment/list/reply（webmssdk 签名，读自己作品的子评论通常放行）。"""
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
CID = "7599140757425816369"

JS = r"""
(async () => {
  const r = await fetch("https://www.douyin.com/aweme/v1/web/comment/list/reply/?item_id=%s&comment_id=%s&cursor=0&count=50&item_type=0", {credentials: "include"});
  const t = await r.text();
  return JSON.stringify({status: r.status, body: t.slice(0, 1500)});
})()
""" % (AWEME, CID)


def window_by_seq(seq):
    page = 0
    while True:
        r = requests.post(f"{BIT_API}/browser/list", json={"page": page, "pageSize": 100}, timeout=20)
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
    if not w:
        print("找不到窗口 174"); return
    opened = requests.post(f"{BIT_API}/browser/open", json={"id": w["id"]}, timeout=60).json()
    if not opened.get("success"):
        print("打开窗口失败:", opened.get("msg")); return
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
            print("等视频页加载 8 秒...")
            await asyncio.sleep(8)
            await bws.send(json.dumps({"id": 3, "method": "Runtime.evaluate",
                                       "params": {"expression": JS, "awaitPromise": True,
                                                  "returnByValue": True, "timeout": 30000},
                                       "sessionId": session_id}))
            while True:
                m = json.loads(await asyncio.wait_for(bws.recv(), timeout=60))
                if m.get("id") == 3:
                    rr = m.get("result", {})
                    if "exceptionDetails" in rr:
                        print("JS 异常:", json.dumps(rr["exceptionDetails"])[:300])
                    else:
                        print("结果:", rr.get("result", {}).get("value"))
                    break
            await bws.send(json.dumps({"id": 4, "method": "Target.closeTarget",
                                       "params": {"targetId": target_id}}))
    finally:
        requests.post(f"{BIT_API}/browser/close", json={"id": w["id"]}, timeout=30)


if __name__ == "__main__":
    asyncio.run(main())
