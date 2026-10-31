"""小红书单次回复：签名 API 或比特浏览器。导入不会发送，无自动重试。

用法见 xhs_reply_usage.md。API 使用随附的 xhs_direct 独立客户端。
浏览器路径只需 playwright，复用比特窗口登录态、代理和指纹。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from xhs_direct.responses import XhsApiError


def outcome(status, *, result=None, error=""):
    return {"status": status, "success": status == "success", "result": result, "error": error}


def validate(note_id, content, target_comment_id):
    if not isinstance(note_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", note_id):
        raise ValueError("note_id 必须是笔记 ID，不是链接")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 不能为空")
    if not isinstance(target_comment_id, str) or (target_comment_id and
            not re.fullmatch(r"[A-Za-z0-9_-]+", target_comment_id)):
        raise ValueError("target_comment_id 格式无效")


def cookie_from_state(state):
    if isinstance(state, str):
        state = json.loads(state)
    if not isinstance(state, dict):
        raise ValueError("storage_state 必须是 Playwright JSON 字符串或字典")
    cookies = {}
    for c in state.get("cookies", []):
        domain = str(c.get("domain", "")).lstrip(".").lower()
        if domain == "xiaohongshu.com" or domain.endswith(".xiaohongshu.com"):
            cookies[c["name"]] = c["value"]
    if not cookies.get("a1"):
        raise ValueError("登录态缺少小红书 a1 cookie")
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def classify(result):
    # 不能仅凭 HTTP 200、非空字典或无异常认定成功。
    if isinstance(result, dict):
        if result.get("success") is False or ("code" in result and result["code"] not in (0, "0")):
            return outcome("failed", result=result, error="平台返回业务失败")
        if result.get("success") is True or result.get("code") in (0, "0"):
            return outcome("success", result=result)
    return outcome("uncertain", result=result, error="未获得明确业务状态，请核对后再决定是否重发")


async def reply_api(note_id, content, *, storage_state, target_comment_id="",
                    xsec_token="", user_agent, proxy="", client_factory=None):
    """调用用户提供的 XhsApiClient；空 target_comment_id 表示顶层评论。

    client_factory 可注入兼容客户端。不会在 API 异常后自动转浏览器重发。
    """
    submitted = False
    validated_data = False
    try:
        validate(note_id, content, target_comment_id)
        cookie = cookie_from_state(storage_state)
        if not user_agent:
            raise ValueError("请传入账号实际 user_agent")
        if client_factory is None:
            from xhs_direct import XhsApiClient
            client_factory = XhsApiClient
            validated_data = True
        client = client_factory(cookie, user_agent=user_agent, proxy=proxy)
        async with client.session_scope():
            submitted = True
            result = await client.post_comment(note_id=note_id, content=content,
                xsec_token=xsec_token, target_comment_id=target_comment_id)
            # 内置客户端已通过 parse_response 校验完整响应，返回的是 data。
            return outcome("success", result=result) if validated_data else classify(result)
    except XhsApiError as exc:
        ambiguous = exc.signal == "ambiguous_response" or exc.category == "network"
        return outcome("uncertain" if submitted and ambiguous else "failed",
                       error=f"小红书接口异常：{exc.signal}（code={exc.code}, HTTP={exc.status_code}）")
    except ImportError as exc:
        return outcome("uncertain" if submitted else "failed",
                       error=f"缺少依赖 {exc.name}；请安装 requirements-xhs-reply.txt。已尝试发送时请先核对平台结果")
    except Exception as exc:
        return outcome("uncertain" if submitted else "failed",
                       error=f"{type(exc).__name__}：" + ("API 调用后状态未知，请核对，勿直接重发" if submitted else str(exc)))


def _open_bitbrowser(base_url, browser_id, timeout):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("bit_api_url 无效")
    req = Request(base_url.rstrip("/") + "/browser/open",
                  data=json.dumps({"id": browser_id}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as response:
        result = json.load(response)
    if result.get("success") is not True:
        raise RuntimeError("比特窗口打开失败，请检查窗口 ID 和本地 API 服务")
    endpoint = (result.get("data") or {}).get("ws")
    if not endpoint:
        raise RuntimeError("比特接口未返回 data.ws 调试地址")
    return endpoint


def _is_comment_request(request):
    url = urlsplit(request.url)
    host = url.hostname or ""
    return (request.method == "POST" and
            (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")) and
            any(marker in url.path for marker in ("/comment/post", "/comment/create", "/comment/add")))


def _matches_payload(payload, note_id, content, target_comment_id):
    return (isinstance(payload, dict) and payload.get("note_id") == note_id
            and payload.get("content") == content
            and (payload.get("target_comment_id") or "") == target_comment_id)


async def _unique_visible(locator):
    found = []
    for i in range(await locator.count()):
        item = locator.nth(i)
        if await item.is_visible():
            found.append(item)
    if len(found) > 1:
        raise ValueError("页面匹配到多个目标，已停止，请提供更精确的选择器")
    return found[0] if found else None


async def reply_browser(note_id, content, *, browser_id=None, storage_state=None, user_agent="", dry_run=False, target_comment_id="",
                        target_text="", xsec_token="", bit_api_url=None,
                        timeout_seconds=30, max_scrolls=16,
                        editor_selector='textarea, #content-textarea, [contenteditable="true"]',
                        submit_selector='button:has-text("发送"), .submit:has-text("发送")'):
    """连接指定比特窗口，新建专用页。提交后保留页面供核对，不关闭窗口。

    target_text 仅辅助定位；发送前仍校验网络请求中的目标评论 ID。
    选择器按已有页面实现编写，页面改版时可由调用方覆盖。
    """
    submitted = False
    page = None
    pw = None
    guard = None
    sent = False
    local_browser = None
    try:
        bit_api_url = bit_api_url or os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")
        validate(note_id, content, target_comment_id)
        if (not browser_id and storage_state is None) or timeout_seconds <= 0 or max_scrolls < 0:
            raise ValueError("需要有效 browser_id、正数超时及非负滚动次数")
        if target_text and not target_comment_id:
            raise ValueError("按文本辅助回复时也必须提供 target_comment_id")
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        if storage_state is not None:
            cookie_from_state(storage_state)
            state = json.loads(storage_state) if isinstance(storage_state, str) else storage_state
            local_browser = await pw.chromium.launch(headless=True)
            context = await local_browser.new_context(user_agent=user_agent or None)
            await context.add_cookies(state['cookies'])
        else:
            endpoint = await asyncio.to_thread(_open_bitbrowser, bit_api_url, browser_id, timeout_seconds)
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=timeout_seconds * 1000)
            if not browser.contexts:
                raise RuntimeError("比特窗口没有可用浏览器上下文")
            context = browser.contexts[0]
        page = await context.new_page()
        page.set_default_timeout(timeout_seconds * 1000)
        response_future = asyncio.get_running_loop().create_future()

        async def guard(route):
            nonlocal sent
            request = route.request
            if not _is_comment_request(request):
                await route.continue_()
                return
            try:
                matches = _matches_payload(request.post_data_json, note_id, content, target_comment_id)
            except Exception:
                matches = False
            if not submitted or sent or not matches:
                await route.abort()
                if not response_future.done():
                    response_future.set_result(outcome("uncertain" if sent else "failed", error="已拦截重复请求或与指定笔记、内容、评论 ID 不符的发送请求"))
                return
            sent = True
            await route.continue_()

        async def on_response(response):
            if not sent or not _is_comment_request(response.request):
                return
            try:
                if not _matches_payload(response.request.post_data_json, note_id, content, target_comment_id):
                    return
                result = classify(await response.json())
                if not response_future.done():
                    response_future.set_result(result)
            except Exception:
                pass

        await page.route("**/*", guard)
        page.on("response", on_response)
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            url += "?" + urlencode({"xsec_token": xsec_token, "xsec_source": "pc_comment"})
        await page.goto(url, wait_until="domcontentloaded")
        if target_comment_id:
            target = None
            for attempt in range(max_scrolls + 1):
                target = await _unique_visible(page.locator(
                    f'[data-comment-id="{target_comment_id}"], [id="comment-{target_comment_id}"]'))
                if target is None and target_text:
                    target = await _unique_visible(page.locator('.comment-item').filter(
                        has=page.get_by_text(target_text, exact=True)))
                if target is not None:
                    break
                if attempt < max_scrolls:
                    panel = await _unique_visible(page.locator('.comments-container'))
                    if panel is not None:
                        await panel.hover()
                    await page.mouse.wheel(0, 600)
                    await page.wait_for_timeout(500)
            if target is None:
                raise ValueError("未找到目标评论，不会转为顶层评论；可传 target_text 辅助定位")
            reply = await _unique_visible(target.get_by_text("回复", exact=True))
            if reply is None:
                raise ValueError("未找到该评论的回复按钮")
            await reply.click()
        await page.locator(editor_selector).first.wait_for(state="visible")
        editor = await _unique_visible(page.locator(editor_selector))
        if editor is None:
            raise ValueError("未找到评论输入框")
        await editor.fill(content)
        send = await _unique_visible(page.locator(submit_selector))
        if send is None or not await send.is_enabled():
            raise ValueError("未找到可用发送按钮")
        if dry_run:
            return outcome('ready', result={'message': '已定位评论、填写回复并确认发送按钮可用；未发送'})
        submitted = True
        await send.click()
        return await asyncio.wait_for(response_future, timeout=timeout_seconds)
    except Exception as exc:
        return outcome("uncertain" if submitted else "failed",
                       error=f"{type(exc).__name__}：" + ("已尝试发送，请在保留的比特页面核对，勿直接重发" if submitted else str(exc)))
    finally:
        if page is not None:
            with suppress(Exception):
                if guard is not None:
                    await page.unroute("**/*", guard)
            if not submitted:
                with suppress(Exception):
                    await page.close()
        if pw is not None:
            with suppress(Exception):
                if local_browser is not None:
                    await local_browser.close()
                await pw.stop()  # 断开连接，不调用 browser.close() 或比特 close 接口。


async def reply_comment(note_id, content, *, mode="browser", **kwargs):
    """统一异步入口，mode='api' 或 'browser'，无隐式跨路径重试。"""
    if mode == "api":
        return await reply_api(note_id, content, **kwargs)
    if mode == "browser":
        return await reply_browser(note_id, content, **kwargs)
    return outcome("failed", error="mode 必须为 api 或 browser")


def main():
    parser = argparse.ArgumentParser(description="小红书单次评论/回复，执行即实际发送")
    parser.add_argument("--mode", choices=("api", "browser"), default="browser")
    parser.add_argument("--note-id", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--target-comment-id", default="")
    parser.add_argument("--xsec-token", default="")
    parser.add_argument("--browser-id")
    parser.add_argument("--bit-api-url", default=os.getenv("BITBROWSER_API", "http://127.0.0.1:54345"))
    parser.add_argument("--target-text", default="")
    parser.add_argument("--storage-state", help="Playwright storage_state JSON 文件")
    parser.add_argument("--user-agent")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    common = dict(target_comment_id=args.target_comment_id, xsec_token=args.xsec_token)
    if args.mode == "api":
        if not args.storage_state or not args.user_agent:
            parser.error("API 路径需要 --storage-state 和 --user-agent")
        common.update(storage_state=Path(args.storage_state).read_text(encoding="utf-8-sig"),
                      user_agent=args.user_agent, proxy=args.proxy)
    else:
        if not args.browser_id:
            parser.error("浏览器路径需要 --browser-id")
        common.update(browser_id=args.browser_id, bit_api_url=args.bit_api_url, target_text=args.target_text)
    result = asyncio.run(reply_comment(args.note_id, args.content, mode=args.mode, **common))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return {"success": 0, "failed": 1, "uncertain": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
