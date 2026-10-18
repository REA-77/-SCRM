"""Browser management for Playwright-based automation."""

from __future__ import annotations

import os
import requests
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Generator

from loguru import logger
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from zhihu.cookies import load_cookies, save_cookies


ZHIHU_BASE_URL = "https://www.zhihu.com"


_bit_options = ContextVar("zhihu_bit_options", default=None)


@contextmanager
def use_bitbrowser(window_id, api_url="http://127.0.0.1:54345"):
    """Use one explicitly selected BitBrowser window for this call."""
    token = _bit_options.set((window_id, api_url.rstrip("/")))
    try:
        yield
    finally:
        _bit_options.reset(token)


@contextmanager
def connect_bitbrowser(window_id, api_url):
    response = requests.post(api_url + "/browser/open", json={"id": window_id}, timeout=120)
    response.raise_for_status()
    payload = response.json()
    ws = (payload.get("data") or {}).get("ws")
    if payload.get("success") is not True or not isinstance(ws, str) or not ws:
        raise RuntimeError("比特浏览器未返回有效连接，请检查窗口ID及本地API")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws, timeout=120000)
        if not browser.contexts:
            raise RuntimeError("比特窗口没有可用的浏览器上下文")
        context = browser.contexts[0]
        page = context.new_page()
        page.set_default_timeout(120000)
        try:
            yield browser, context, page
        finally:
            # Only close our tab; keep the user's window and login session.
            if not page.is_closed():
                page.close()


@contextmanager
def create_browser(headless: bool = True) -> Generator[tuple[Browser, BrowserContext, Page], None, None]:
    """Create a browser instance with cookies loaded.

    Yields:
        (browser, context, page) tuple
    """
    bit_options = _bit_options.get()
    if bit_options:
        with connect_bitbrowser(*bit_options) as session:
            yield session
        return
    proxy = os.environ.get("ZHIHU_PROXY")

    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        browser = p.chromium.launch(
            headless=headless,
            args=launch_args,
        )

        context_options: dict = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }

        if proxy:
            context_options["proxy"] = {"server": proxy}
            logger.info("Using proxy for browser")

        context = browser.new_context(**context_options)

        # Load cookies if available
        cookies = load_cookies()
        if cookies:
            context.add_cookies(cookies)
            logger.info(f"Cookies loaded into browser context: {len(cookies)} cookies")
            # Log cookie domains for debugging
            domains = set(c.get("domain", "") for c in cookies)
            logger.info(f"Cookie domains: {domains}")
        else:
            logger.warning("No cookies found — please run login.py first")

        # Anti-detection: override webdriver property
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            window.chrome = { runtime: {} };
            """
        )

        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            yield browser, context, page
        finally:
            page.close()
            context.close()
            browser.close()
            logger.debug("Browser closed")


def save_browser_cookies(context: BrowserContext) -> None:
    """Save cookies from browser context to disk."""
    if _bit_options.get():
        return  # BitBrowser manages its own account cookies.
    cookies = context.cookies()
    save_cookies(cookies)
