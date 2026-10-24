"""用法示例。

依赖:pip install curl_cffi patchright(API 路径只需 curl_cffi)
"""
import asyncio
import json

from douyin_commenter import DouyinClient, cookie_from_state, post_comment_browser

AWEME_ID = "7xxxxxxxxxxxxxxxxx"          # 作品 id
REPLY_TEXT = "目标评论原文前 20 字(回复模式才需要)"
CONTENT = "感谢支持!"

# Patchright 持久 profile 目录(需已登录抖音)
PROFILE_DIR = r"D:\profiles\my_douyin_account"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def api_mode():
    """API 直连:从浏览器 storage_state JSON 提取 Cookie,本地 a_bogus 签名后 POST。"""
    with open("storage_state.json", encoding="utf-8") as f:
        cookie = cookie_from_state(f.read())
    if not cookie:
        print("Cookie 提取失败")
        return
    client = DouyinClient(cookie, UA, timeout=20, proxy="")
    async with client.session_scope():
        # 回复评论时传 reply_comment_id;发一级评论留空
        ok, cid, err = await client.post_comment(
            AWEME_ID, CONTENT, reply_comment_id="")
    print("api:", ok, cid, err)
    # 错误约定:err 以 write_uncertain: 开头 = 结果未知,禁止盲目重试


class _Mgr:
    """最小浏览器管理器示例:持久 profile 复用登录态。

    生产环境建议自行实现媒体屏蔽、代理注入等;接口只需满足
    open_headed(identity) / new_page(identity, block_media=...) 即可。
    """

    def __init__(self):
        from patchright.async_api import async_playwright
        self._pw = async_playwright()
        self._p = None

    async def start(self):
        self._p = await self._pw.start()

    async def open_headed(self, identity):
        ctx = await self._p.chromium.launch_persistent_context(
            identity, headless=False, user_agent=UA, viewport=None)
        return ctx

    async def new_page(self, identity, block_media=False):
        ctx = await self.open_headed(identity)
        return await ctx.new_page()

    async def stop(self):
        await self._pw.__aexit__(None, None, None)


async def browser_mode():
    """浏览器路径:打开作品页,定位目标评论回复框,逐字输入发送。
    成功判据 = 拦截 publish 接口 status_code==0。"""
    mgr = _Mgr()
    await mgr.start()
    try:
        ok, err = await post_comment_browser(
            mgr, PROFILE_DIR, AWEME_ID, CONTENT,
            reply_to_text=REPLY_TEXT,       # 留空 = 发顶层评论
            headed=True,                    # 抖音对无头写操作常拦截
            require_reply=False,
        )
        print("browser:", ok, err)
    finally:
        await mgr.stop()


if __name__ == "__main__":
    asyncio.run(api_mode())
    # asyncio.run(browser_mode())
