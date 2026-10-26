# -*- coding: utf-8 -*-
"""task#68 / 评论 7599140757425816369（账号 瑞伊～，窗口174）直连 API 可行性检查：
1. 登录态 profile/self
2. 目标作品评论列表可读性
3. 防重复检查（该评论下是否已有回复）
4. 全部通过后走直连签名 API 发送真实回复（即任务本体，非额外副作用）"""
import asyncio
import os
import sys

for n in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(n, None)

sys.path.insert(0, r"D:\下载\数据库")
sys.path.insert(0, r"D:\下载\数据库\douyin_commenter_reference\douyin_commenter")

from comment_auto_worker import get_douyin_browser_identity, get_douyin_cookie

AWEME = "7589999859524980026"
CID = "7599140757425816369"
REPLY = "哈哈哈收到收到，隐私保护必须拉满！有家电选购问题随时cue我，专业测评人绝不泄露半点信息~"

cookie = get_douyin_cookie("瑞伊～", "174")["cookies"]
ident = get_douyin_browser_identity("174")
print(f"identity: locale={ident['locale']} {ident['width']}x{ident['height']} ua={ident['ua'][:70]}")


async def main():
    from douyin_commenter import DouyinClient
    from comment_auto_worker import _post_douyin_reply_direct

    c = DouyinClient(cookie, ident["ua"], timeout=25, locale=ident["locale"],
                     screen_width=ident["width"], screen_height=ident["height"])
    async with c.session_scope():
        p = await c._get_json("/aweme/v1/web/user/profile/self/", {},
                              "https://www.douyin.com/user/self")
        if p and p.get("user"):
            print(f"[1] 登录态OK: {p['user'].get('nickname')} (uid={p['user'].get('uid')})")
        else:
            print(f"[1] 登录态异常: status_code={p.get('status_code') if p else None} err={c.last_error}")
            return "abort: 未登录"

        page = await c.fetch_comments_page(AWEME, 0, 50)
        found = [cm for cm in (page.get("comments") or []) if str(cm.get("cid")) == CID]
        if found:
            print(f"[2] 目标评论在一级列表, reply_comment_total={found[0].get('reply_comment_total')}")
        else:
            print("[2] 目标评论不在前50条（作品评论较多，不阻塞）")

        rp = await c.fetch_replies_page(AWEME, CID, 0, 50)
        replies = rp.get("comments") or []
        dup = any(REPLY[:12] in (r.get("text") or "") for r in replies)
        print(f"[3] 已有子评论 {len(replies)} 条, 含本次回复文案={dup}")
        if dup:
            return "abort: 已回复过"

    task = {"reply_suggestion": REPLY, "aweme_id": AWEME, "comment_id": CID}
    result = await _post_douyin_reply_direct(task, cookie, ident)
    print("[4] 发送结果:", result)
    return result


print(asyncio.run(main()))
