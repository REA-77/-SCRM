# -*- coding: utf-8 -*-
"""task#81 实测：窗口174打开小红书笔记页，检查「在的1027」是否已存在；
不存在则调用 xhs-browser-reply.mjs 真实发送。"""
import asyncio
import json
import os
import subprocess
import sys

for n in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(n, None)

sys.path.insert(0, r"D:\下载\数据库")

import requests
from comment_auto_worker import BIT_API

NOTE_ID = "6ab54c1c000000001802aeb0"
CID = "6ad0e8a5000000000702f6ec"
TARGET_TEXT = "还在吗"
REPLY_TEXT = "在的1027"
XSEC = "ABsh86bLf4pXmBAImyx7_dX-bKBZN_iKzo88zh9ajA9nk="
WID = "020fff2c553445469be01800b1ecbd2e"

CHECK_JS = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // 滚动加载评论
  for (let i = 0; i < 10; i++) {
    window.scrollBy(0, 1200);
    await sleep(1200);
  }
  const body = document.body.innerText;
  return JSON.stringify({
    hasComment: body.includes("%s"),
    hasReply: body.includes("%s"),
    bodyLen: body.length
  });
})()
""" % (TARGET_TEXT, REPLY_TEXT)


async def main():
    import websockets
    opened = None
    for attempt in range(4):
        r = requests.post(f"{BIT_API}/browser/open", json={"id": WID}, timeout=180)
        j = r.json()
        if j.get("success"):
            opened = j
            break
        print(f"打开失败(第{attempt+1}次): {j.get('msg')}，等 25 秒...")
        await asyncio.sleep(25)
    if not opened:
        print("窗口打不开")
        return
    http_addr = opened["data"]["http"]
    if "://" not in http_addr:
        http_addr = "http://" + http_addr
    try:
        vers = requests.get(f"{http_addr}/json/version", timeout=15).json()
        async with websockets.connect(vers["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024) as bws:
            await bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                       "params": {"url": f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token={XSEC}&xsec_source=pc_comment"}}}))
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

            async def ev(cid, expr, timeout=60):
                await bws.send(json.dumps({"id": cid, "method": "Runtime.evaluate",
                                           "params": {"expression": expr, "awaitPromise": True,
                                                      "returnByValue": True, "timeout": 50000},
                                           "sessionId": session_id}))
                while True:
                    m = json.loads(await asyncio.wait_for(bws.recv(), timeout=timeout))
                    if m.get("id") == cid:
                        rr = m.get("result", {})
                        if "exceptionDetails" in rr:
                            return "JS异常: " + json.dumps(rr["exceptionDetails"])[:300]
                        return rr.get("result", {}).get("value")

            print("等笔记页加载 12 秒...")
            await asyncio.sleep(12)
            r = await ev(3, CHECK_JS)
            print("检查结果:", r)
            await bws.send(json.dumps({"id": 4, "method": "Target.closeTarget",
                                       "params": {"targetId": target_id}}))
            return r
    finally:
        requests.post(f"{BIT_API}/browser/close", json={"id": WID}, timeout=180)


if __name__ == "__main__":
    result = asyncio.run(main())
    if result and '"hasReply":true' in result.replace(" ", ""):
        print("已回复过，不再发送")
        sys.exit(0)
    if not result or '"hasComment":true' not in result.replace(" ", ""):
        print("评论未加载到或页面异常，暂不发送")
        sys.exit(1)

    print("未检测到已有回复，开始真实发送...")
    payload = {
        "noteId": NOTE_ID,
        "content": REPLY_TEXT,
        "browserId": WID,
        "targetCommentId": CID,
        "targetText": TARGET_TEXT,
        "xsecToken": XSEC,
        "timeoutSeconds": 120,
        "bitApiUrl": BIT_API,
    }
    proc = subprocess.run(
        ["node", r"D:\下载\数据库\douyin-creator-tools\xhs-browser-reply.mjs"],
        input=json.dumps(payload, ensure_ascii=False),
        text=True, capture_output=True, timeout=240,
        cwd=r"D:\下载\数据库\douyin-creator-tools",
    )
    print("returncode:", proc.returncode)
    print("stdout:", proc.stdout[-600:])
    print("stderr:", proc.stderr[-600:])
