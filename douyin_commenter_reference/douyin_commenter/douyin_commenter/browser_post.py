"""抖音发表评论(浏览器自动化路径)。

从 creatorhub app/browser/fetcher.py 原样抽出的最小可用集。
成功判据 = 拦截抖音 comment/publish 接口响应的 status_code(0=成功),
而非"输入框是否清空"(后者会被验证码/频控误判为成功)。

mgr 为鸭子类型:需要提供
    open_headed(identity) -> context   # 有头窗口,context.new_page() 打开页面
    new_page(identity, block_media=False) -> page
  兼容 Playwright / Patchright 的 API 即可,identity 由调用方的浏览器
  持久化方案决定(含登录态的 profile)。
"""
from __future__ import annotations

from typing import Tuple

# ── 抖音发评论(浏览器自动化)──
# 评论输入框 / 发送按钮选择器(抖音改版时改这里。data-e2e 较稳,排前)
_COMMENT_INPUT = [
    '[data-e2e="comment-input"]',
    'div.comment-input-inner [contenteditable="true"]',
    'div[data-e2e="comment-publish"] [contenteditable="true"]',
    '.comment-input [contenteditable="true"]',
    'div[contenteditable="true"][data-line-wrapper]',
    'div[contenteditable="true"]',
]
_COMMENT_SUBMIT = [
    '[data-e2e="comment-publish"]',
    'div.comment-input-area button:has-text("发送")',
    'button:has-text("发送")',
    'span:has-text("发送")',
]
# 抖音发表评论接口(权威成功判据:拦截它的响应看 status_code)
_PUBLISH_API = "aweme/v1/web/comment/publish"

# 找不到输入框时,导出页面真实结构,便于对症补选择器
_DIAG_INPUTS = """
() => {
  const ce = [];
  document.querySelectorAll('[contenteditable]').forEach(el => {
    ce.push(((el.tagName || '') + '.' + (typeof el.className === 'string' ? el.className : ''))
      .slice(0, 70) + ' | ph=' +
      (el.getAttribute('data-placeholder') || el.getAttribute('placeholder')
       || el.getAttribute('aria-label') || '').slice(0, 30));
  });
  const e2e = [];
  document.querySelectorAll('[data-e2e]').forEach(el => {
    const v = el.getAttribute('data-e2e');
    if (v && /comment|input|publish|reply|editor/i.test(v)) e2e.push(v);
  });
  return JSON.stringify({ ce: ce.slice(0, 12), e2e: [...new Set(e2e)].slice(0, 20),
                          url: location.href });
}
"""

_SCROLL_COMMENTS = """
() => {
  const item = document.querySelector('[data-e2e="comment-item"]')
            || document.querySelector('[data-e2e="comment-list"]');
  if (!item) { window.scrollBy(0, 3000); return false; }
  let el = item;
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
      el.scrollTop = el.scrollHeight;
      return true;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 3000);
  return false;
}
"""


async def post_comment_browser(mgr, identity, aweme_id: str,
                               content: str, reply_to_text: str = "", headed: bool = True,
                               settle_ms: int = 1800, timeout_ms: int = 12000,
                               require_reply: bool = False
                               ) -> Tuple[bool, str]:
    """用账号持久 profile(已含登录态)打开作品页,在评论框输入并发送。
    headed=True:弹真实浏览器窗口(抖音对无头写操作常降级/拦截,有头更稳,且能手动过验证码)。
    成功判据 = 拦截抖音 comment/publish 接口响应的 status_code(0=成功),
    而非"输入框是否清空"(后者会被验证码/频控误判为成功)。
    reply_to_text 非空:尝试定位含该文本的评论、点其「回复」内联输入;失败回退顶层评论。
    返回 (ok, error)。⚠️ 选择器随抖音改版可能失效,集中在 _COMMENT_INPUT/_COMMENT_SUBMIT。"""
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    if require_reply and not (reply_to_text or "").strip():
        return False, "缺少目标评论原文，已跳过回复"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)   # 同 profile 有头窗口(关闭即落盘 Cookie)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)
    # 拦截发表接口响应(权威判据)
    pub = {"seen": False, "ok": False, "code": None, "msg": ""}

    async def on_response(resp):
        if _PUBLISH_API in resp.url and not pub["seen"]:
            try:
                data = await resp.json()
            except Exception:
                return
            pub["seen"] = True
            pub["code"] = data.get("status_code")
            pub["ok"] = data.get("status_code") == 0
            pub["msg"] = data.get("status_msg") or ""

    page.on("response", on_response)
    try:
        await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录,无法发评论"

        # 评论区懒加载:显式等输入框出现(全是 CSS 选择器,可合并等待),并轻滚一下触发渲染
        try:
            await page.evaluate("() => window.scrollBy(0, 600)")
            await page.wait_for_selector(",".join(_COMMENT_INPUT), timeout=8000,
                                         state="attached")
        except Exception:
            pass

        editor = None
        # 回复模式:先在评论区找到目标评论,点它的「回复」打开内联框
        if reply_to_text:
            try:
                await page.evaluate(_SCROLL_COMMENTS)
                await page.wait_for_timeout(1200)
                item = page.locator('[data-e2e="comment-item"]', has_text=reply_to_text[:20]).first
                if await item.count():
                    rbtn = item.get_by_text("回复", exact=False).first
                    await rbtn.click(timeout=4000)
                    await page.wait_for_timeout(800)
                    editor = page.locator('[contenteditable="true"]').last
            except Exception:
                editor = None  # 回退到顶层评论框
            if editor is None and require_reply:
                return False, "未找到目标评论回复区，未发送成顶层评论"

        if editor is None:
            for sel in _COMMENT_INPUT:
                loc = page.locator(sel).first
                try:
                    if await loc.count():
                        editor = loc
                        break
                except Exception:
                    continue
        if editor is None:
            diag = ""
            try:
                diag = await page.evaluate(_DIAG_INPUTS)
            except Exception:
                pass
            print(f"[comment_post] 未找到输入框 aweme={aweme_id} diag={diag}")
            return False, ("未找到评论输入框(评论区可能未加载/被关闭/页面改版)。"
                           f"页面诊断: {diag[:300]}")

        await editor.click(timeout=timeout_ms)
        await page.wait_for_timeout(300)
        await page.keyboard.type(content, delay=40)   # 逐字输入,更像真人
        await page.wait_for_timeout(600)

        # 优先点发送按钮;找不到则回车提交
        sent = False
        for sel in _COMMENT_SUBMIT:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_enabled():
                    await btn.click(timeout=3000)
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            try:
                await page.keyboard.press("Enter")
                sent = True
            except Exception:
                pass
        if not sent:
            return False, "未找到发送按钮且回车提交失败"

        # 等抖音的发表接口回包(权威判据),最多 ~8s
        for _ in range(27):
            if pub["seen"]:
                break
            await page.wait_for_timeout(300)

        if pub["seen"]:
            if pub["ok"]:
                return True, ""
            return False, (f"抖音拒绝评论(status_code={pub['code']}"
                           f"{' ' + pub['msg'] if pub['msg'] else ''})—— 多为验证码/频控/风控,"
                           f"请降低频率或换号稍后再试")

        # 没等到发表接口:多半是点击没真正触发提交,或被前置验证拦下
        try:
            left = (await editor.inner_text())[:50]
        except Exception:
            left = ""
        if content[:8] in left:
            return False, "已输入但未触发发表(可能弹了验证码/发送按钮未激活)"
        return False, "未捕获到抖音发表接口响应,无法确认是否成功(请人工核对该作品评论区)"
    except Exception as e:
        return False, f"发评论异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()   # 有头:关 context 即落盘 Cookie
            else:
                await page.close()
        except Exception:
            pass
