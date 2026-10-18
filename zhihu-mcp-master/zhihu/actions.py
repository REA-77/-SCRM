"""Browser automation actions for Zhihu (知乎).

Each action class encapsulates a specific workflow on zhihu.com.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from loguru import logger
from playwright.sync_api import Page, BrowserContext, TimeoutError as PwTimeout

from browser.manager import ZHIHU_BASE_URL, save_browser_cookies


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class LoginAction:
    """Handle Zhihu login via QR code scanning."""

    LOGIN_URL = f"{ZHIHU_BASE_URL}/signin"

    def __init__(self, page: Page, context: BrowserContext):
        self.page = page
        self.context = context

    def fetch_qrcode(self) -> dict:
        """Navigate to login page and fetch the QR code image.

        Returns:
            dict with keys: qrcode_base64 (str), is_logged_in (bool)
        """
        page = self.page
        page.goto(self.LOGIN_URL, wait_until="networkidle")
        time.sleep(3)

        # Check if already logged in (redirected to homepage)
        if "/signin" not in page.url:
            logger.info("Already logged in (redirected)")
            return {"qrcode_base64": "", "is_logged_in": True}

        logger.info(f"Login page loaded: {page.url}")
        logger.info("Please scan the QR code in the browser window")

        # Try to get QR code image for base64 (best-effort)
        qr_base64 = ""
        for selector in [
            "div.Qrcode-container img",
            "img[src*='qrcode']",
            "img[src*='QR']",
            ".SignFlowQrcode img",
            ".Qrcode-img img",
            "img[class*='qrcode']",
            "img[class*='Qrcode']",
        ]:
            qr_el = page.query_selector(selector)
            if qr_el:
                try:
                    screenshot_bytes = qr_el.screenshot()
                    qr_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                    logger.info(f"QR code found with selector: {selector}")
                    break
                except Exception:
                    continue

        if not qr_base64:
            # Fallback: screenshot the whole page
            try:
                page.screenshot(
                    path=str(Path(__file__).parent.parent / "cookies" / "login_qrcode.png"),
                    full_page=False,
                )
                logger.info("QR screenshot saved to cookies/login_qrcode.png")
            except Exception:
                pass

        return {"qrcode_base64": qr_base64, "is_logged_in": False}

    def wait_for_login(self, timeout: int = 240) -> bool:
        """Wait for the user to scan the QR code.

        Args:
            timeout: seconds to wait

        Returns:
            True if login succeeded
        """
        page = self.page
        deadline = time.time() + timeout

        while time.time() < deadline:
            current_url = page.url
            # After successful scan, page redirects to homepage
            if "/signin" not in current_url and "zhihu.com" in current_url:
                logger.info("Login successful!")
                save_browser_cookies(self.context)
                return True
            # Check if QR expired
            expired_el = page.query_selector("text=二维码已失效")
            if expired_el:
                logger.warning("QR code expired")
                return False
            time.sleep(2)

        logger.warning("Login wait timed out")
        return False


# ---------------------------------------------------------------------------
# Check Login Status
# ---------------------------------------------------------------------------

class CheckLoginAction:
    """Check if the current session is logged in."""

    def __init__(self, page: Page):
        self.page = page

    def check(self) -> dict:
        """Visit Zhihu and check login status.

        Returns:
            dict with keys: logged_in (bool), username (str | None)
        """
        page = self.page
        page.goto(ZHIHU_BASE_URL, wait_until="networkidle")
        time.sleep(2)

        # Method 1: Check for "登录" button — if found, NOT logged in
        login_btn = page.query_selector(
            'button:has-text("登录"), a:has-text("登录"), '
            'a.SignFlow-submit, .Login-content'
        )
        if login_btn:
            logger.info("Not logged in — login button found")
            return {"logged_in": False, "username": None}

        # Method 2: Check for user menu (only visible when logged in)
        user_menu = page.query_selector(
            '.AppHeader-userEntry, .Popover-wrapper [class*="user"], '
            'header [class*="userMenu"], .AppHeader-userInfo'
        )
        if user_menu:
            # Try to get username from the menu
            username_el = page.query_selector(
                '.AppHeader-userEntry .UserLink-link, '
                '.AppHeader-userEntry [class*="name"], '
                '.AppHeader-userInfo span'
            )
            username = username_el.inner_text().strip() if username_el else "已登录用户"
            logger.info(f"Logged in as: {username}")
            return {"logged_in": True, "username": username}

        # Method 3: Check for "写回答" or "提问" buttons (only for logged-in users)
        write_btn = page.query_selector(
            'button:has-text("写回答"), button:has-text("提问"), '
            'a:has-text("写文章")'
        )
        if write_btn:
            logger.info("Logged in — write buttons visible")
            return {"logged_in": True, "username": "已登录用户"}

        logger.info("Not logged in — no login indicators found")
        return {"logged_in": False, "username": None}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchAction:
    """Search Zhihu content by keyword."""

    def __init__(self, page: Page):
        self.page = page

    def search(self, keyword: str, search_type: str = "综合", limit: int = 20) -> list[dict]:
        """Search Zhihu and return results.

        Args:
            keyword: search keyword
            search_type: 综合 | 问题 | 回答 | 文章 | 用户
            limit: max results to return

        Returns:
            list of search result dicts
        """
        page = self.page

        # Build search URL
        type_map = {
            "综合": "general",
            "问题": "question",
            "回答": "answer",
            "文章": "article",
            "用户": "people",
        }
        search_url = f"{ZHIHU_BASE_URL}/search?type={type_map.get(search_type, 'general')}&q={keyword}"
        logger.info(f"Navigating to: {search_url}")
        page.goto(search_url, wait_until="domcontentloaded")
        time.sleep(3)

        # Debug: log current URL and save screenshot
        logger.info(f"Current URL after navigation: {page.url}")
        debug_dir = Path(__file__).parent.parent / "cookies"
        try:
            page.screenshot(path=str(debug_dir / "search_debug.png"), full_page=False)
            logger.info(f"Debug screenshot saved to {debug_dir / 'search_debug.png'}")
        except Exception as e:
            logger.debug(f"Screenshot failed: {e}")

        # Debug: log page title and first 500 chars of body text
        try:
            title = page.title()
            logger.info(f"Page title: {title}")
            body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 1000) : 'no body'")
            logger.info(f"Page body preview:\n{body_text[:1000]}")
        except Exception as e:
            logger.debug(f"Body text extraction failed: {e}")

        # Scroll down to trigger lazy loading
        for _ in range(3):
            page.mouse.wheel(0, 800)
            time.sleep(1)

        # Wait for search results to appear (multiple selector strategies)
        found = False
        for selector in [
            ".SearchResult-Card",
            ".Card.SearchResult-Card",
            ".List-item",
            "[class*='SearchResult']",
            "[class*='Card']",
            ".content-main",
            "main .Card",
        ]:
            try:
                page.wait_for_selector(selector, timeout=5000)
                logger.info(f"Found results with selector: {selector}")
                found = True
                break
            except PwTimeout:
                continue

        if not found:
            logger.warning("No result containers found, trying full page parse")
            return self._fallback_parse(page, limit)

        results = []

        # Parse search result items — try multiple container selectors
        items = []
        for selector in [
            ".SearchResult-Card",
            ".Card.SearchResult-Card",
            ".List-item",
            "[class*='SearchResult']",
        ]:
            items = page.query_selector_all(selector)
            if items:
                logger.info(f"Found {len(items)} items with selector: {selector}")
                break

        for item in items[:limit]:
            try:
                result = self._parse_search_item(item)
                if result:
                    results.append(result)
            except Exception as e:
                logger.debug(f"Failed to parse search item: {e}")
                continue

        # If still empty, try fallback
        if not results:
            logger.warning("Selector-based parsing returned nothing, trying fallback")
            return self._fallback_parse(page, limit)

        logger.info(f"Search '{keyword}' returned {len(results)} results")
        return results

    def _fallback_parse(self, page: Page, limit: int) -> list[dict]:
        """Fallback: parse links and text from the entire page."""
        results = []
        # Get all links that look like content links
        links = page.query_selector_all("a[href*='/question/'], a[href*='/p/'], a[href*='/answer/']")
        seen = set()
        for link in links[:limit * 2]:
            try:
                href = link.get_attribute("href") or ""
                if href in seen:
                    continue
                seen.add(href)

                title = link.inner_text().strip()
                if not title or len(title) < 2:
                    continue

                if not href.startswith("http"):
                    href = f"{ZHIHU_BASE_URL}{href}"

                results.append({
                    "title": title[:100],
                    "link": href,
                    "excerpt": "",
                    "author": "",
                    "votes": "0",
                })
                if len(results) >= limit:
                    break
            except Exception:
                continue

        logger.info(f"Fallback parse found {len(results)} results")
        return results

    def _parse_search_item(self, item) -> dict | None:
        """Parse a single search result card."""
        # Try to find the title link — multiple strategies
        title_el = None
        for selector in [
            "h2 a",
            "h2 span",
            ".ContentItem-title a",
            ".ContentItem-title span",
            "a[data-za-detail-view-element_name='Title']",
            ".ContentItem-title",
            "a[href*='/question/']",
            "a[href*='/p/']",
            "a[href*='/answer/']",
        ]:
            title_el = item.query_selector(selector)
            if title_el:
                break

        if not title_el:
            return None

        title = title_el.inner_text().strip()
        if not title:
            return None

        link = ""
        href = title_el.get_attribute("href") or ""
        if href:
            if href.startswith("http"):
                # Already a full URL
                link = href
            elif href.startswith("//"):
                # Protocol-relative URL like //zhuanlan.zhihu.com/p/xxx
                link = f"https:{href}"
            elif href.startswith("/zhuanlan.zhihu.com") or href.startswith("/www.zhihu.com"):
                # Malformed: contains domain in path, fix it
                link = f"https:/{href.lstrip('/')}"
            elif "/p/" in href:
                link = f"https://zhuanlan.zhihu.com{href}"
            else:
                link = f"{ZHIHU_BASE_URL}{href}"

        # Extract excerpt
        excerpt_el = item.query_selector(
            ".RichContent-inner, .CopyrightRichTextContainer, .content, "
            "[class*='RichContent'], [class*='content'], .meta"
        )
        excerpt = excerpt_el.inner_text().strip()[:200] if excerpt_el else ""

        # Extract author
        author_el = item.query_selector(
            ".AuthorInfo-name, .UserLink-link, .AuthorInfo-head, "
            "[class*='Author'], [class*='author'], meta"
        )
        author = author_el.inner_text().strip() if author_el else ""

        # Extract vote count
        vote_el = item.query_selector(
            ".VoteButton--up, .Vote-count, button[class*='Vote']"
        )
        votes = vote_el.inner_text().strip() if vote_el else "0"

        return {
            "title": title,
            "link": link,
            "excerpt": excerpt,
            "author": author,
            "votes": votes,
        }


# ---------------------------------------------------------------------------
# Feed / Recommendation List
# ---------------------------------------------------------------------------

class RecommendFeedAction:
    """Get Zhihu home page recommended content."""

    def __init__(self, page: Page):
        self.page = page

    def get_recommend_list(self, scroll_count: int = 3, limit: int = 20) -> list[dict]:
        """Get recommended feeds from Zhihu homepage.

        Args:
            scroll_count: number of scroll-down actions to load more content
            limit: max items to return

        Returns:
            list of feed dicts
        """
        page = self.page
        page.goto(ZHIHU_BASE_URL, wait_until="networkidle")
        time.sleep(2)

        # Scroll to load more content
        for _ in range(scroll_count):
            page.mouse.wheel(0, 1500)
            time.sleep(1.5)

        feeds = []
        items = page.query_selector_all(
            ".Card.TopstoryItem, .TopstoryItem, .ContentItem"
        )

        for item in items[:limit]:
            try:
                feed = self._parse_feed_item(item)
                if feed:
                    feeds.append(feed)
            except Exception as e:
                logger.debug(f"Failed to parse feed item: {e}")
                continue

        logger.info(f"Got {len(feeds)} recommended feeds")
        return feeds

    def _parse_feed_item(self, item) -> dict | None:
        """Parse a single feed item."""
        title_el = item.query_selector(
            "h2.ContentItem-title a, .ContentItem-title span, "
            ".RichText.ztext"
        )
        if not title_el:
            return None

        title = title_el.inner_text().strip()

        # Get the answer/article link
        link_el = item.query_selector("a[href*='/question/'], a[href*='/p/']")
        link = ""
        if link_el:
            href = link_el.get_attribute("href") or ""
            if not href.startswith("http"):
                link = f"{ZHIHU_BASE_URL}{href}"
            else:
                link = href

        # Author
        author_el = item.query_selector(".AuthorInfo-name, .UserLink-link")
        author = author_el.inner_text().strip() if author_el else ""

        # Excerpt
        excerpt_el = item.query_selector(".RichContent-inner, .RichText")
        excerpt = excerpt_el.inner_text().strip()[:300] if excerpt_el else ""

        # Votes
        vote_el = item.query_selector("button[class*='Vote']")
        votes = vote_el.inner_text().strip() if vote_el else "0"

        # Comments count
        comment_el = item.query_selector("button[class*='ContentItem-action']:has-text('评论')")
        comments = comment_el.inner_text().strip() if comment_el else "0"

        return {
            "title": title,
            "link": link,
            "author": author,
            "excerpt": excerpt,
            "votes": votes,
            "comments": comments,
        }


# ---------------------------------------------------------------------------
# Feed Detail
# ---------------------------------------------------------------------------

class FeedDetailAction:
    """Get detailed content of a Zhihu answer/article including comments."""

    def __init__(self, page: Page):
        self.page = page

    def get_detail(self, url: str, load_comments: bool = True, comment_limit: int = 20) -> dict:
        """Fetch full detail of a Zhihu answer or article.

        Args:
            url: full URL to the answer or article
            load_comments: whether to load comments
            comment_limit: max comments to load

        Returns:
            detail dict
        """
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(2)

        # Title
        title_el = page.query_selector(
            "h1.QuestionHeader-title, .Post-Title, h1.QuestionHeader-title span"
        )
        title = title_el.inner_text().strip() if title_el else ""

        # Author
        author_el = page.query_selector(
            ".AuthorInfo-name, .UserLink-link, meta[name='author']"
        )
        author = author_el.inner_text().strip() if author_el else ""

        # Content body
        content_el = page.query_selector(
            ".RichContent-inner, .RichText.ztext, .Post-RichTextContainer"
        )
        content = content_el.inner_text().strip() if content_el else ""

        # Interaction stats
        vote_el = page.query_selector(
            "button.VoteButton--up, .VoteButton--up"
        )
        votes = "0"
        if vote_el:
            vote_text = vote_el.inner_text().strip()
            votes = vote_text if vote_text else "0"

        comment_count_el = page.query_selector(
            "div.ContentItem-action:has-text('评论'), button[class*='ContentItem-action']"
        )
        comment_count = "0"
        if comment_count_el:
            comment_count = comment_count_el.inner_text().strip()

        # Load comments if requested
        comments_list = []
        if load_comments:
            comments_list = self._load_comments(page, comment_limit)

        return {
            "title": title,
            "author": author,
            "content": content,
            "votes": votes,
            "comment_count": comment_count,
            "url": url,
            "comments": comments_list,
        }

    def _load_comments(self, page: Page, limit: int) -> list[dict]:
        """Load comments from the page."""
        comments = []

        # Click "查看评论" or similar button to open comments
        try:
            comment_btn = page.query_selector(
                "button:has-text('评论'), a:has-text('查看评论'), "
                "div:has-text('条评论')"
            )
            if comment_btn:
                comment_btn.click()
                time.sleep(2)
        except Exception:
            pass

        # Scroll to load more comments
        for _ in range(3):
            page.mouse.wheel(0, 800)
            time.sleep(1)

        # Parse comment items
        comment_items = page.query_selector_all(
            ".CommentItem, .CommentContent, .CommentItemV2, .List-item"
        )

        for item in comment_items[:limit]:
            try:
                comment = self._parse_comment(item)
                if comment:
                    comments.append(comment)
            except Exception:
                continue

        return comments

    def _parse_comment(self, item) -> dict | None:
        """Parse a single comment."""
        author_el = item.query_selector(
            ".CommentItem-meta .UserLink-link, .CommentItemV2-meta .UserLink-link, "
            ".CommentAuthor, a.UserLink-link"
        )
        author = author_el.inner_text().strip() if author_el else ""

        content_el = item.query_selector(
            ".CommentItem-content, .CommentItemV2-content, .CommentContent, "
            "span.RichText"
        )
        content = content_el.inner_text().strip() if content_el else ""

        if not content:
            return None

        # Votes on comment
        vote_el = item.query_selector(
            "button[class*='Vote'], .CommentItem-voteCount"
        )
        votes = vote_el.inner_text().strip() if vote_el else "0"

        # Comment ID for reply
        comment_id = item.get_attribute("data-comment-id") or ""

        return {
            "author": author,
            "content": content,
            "votes": votes,
            "comment_id": comment_id,
        }


# ---------------------------------------------------------------------------
# Publish Article (图文内容)
# ---------------------------------------------------------------------------

class PublishArticleAction:
    """Publish an article (图文) to Zhihu."""

    def __init__(self, page: Page, context: BrowserContext):
        self.page = page
        self.context = context

    def publish(self, title: str, content: str, images: list[str] | None = None,
                tags: list[str] | None = None) -> dict:
        """Publish an article to Zhihu.

        Args:
            title: article title
            content: article body (plain text or HTML)
            images: list of image file paths or URLs
            tags: list of tags

        Returns:
            result dict with success status
        """
        page = self.page

        # Navigate to article editor
        page.goto("https://zhuanlan.zhihu.com/write", wait_until="networkidle")
        time.sleep(3)

        # Debug: screenshot + page text
        debug_dir = Path(__file__).parent.parent / "cookies"
        try:
            page.screenshot(path=str(debug_dir / "publish_debug.png"), full_page=False)
            logger.info(f"Publish page URL: {page.url}")
            body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")
            logger.info(f"Page text preview:\n{body_text[:500]}")
        except Exception:
            pass

        # Enter title — try multiple strategies
        title_input = None
        for selector in [
            "textarea[placeholder*='标题']",
            "input[placeholder*='标题']",
            "textarea[placeholder*='title']",
            "h1[contenteditable='true']",
            "[data-placeholder*='标题']",
            ".WriteIndex-titleInput textarea",
            ".WriteTitle-input",
            "textarea.WriteIndex-titleInput",
        ]:
            title_input = page.query_selector(selector)
            if title_input:
                logger.info(f"Title input found: {selector}")
                break

        if title_input:
            title_input.click()
            time.sleep(0.5)
            title_input.fill("")
            title_input.fill(title)
            logger.info(f"Title set: {title}")
        else:
            # Fallback: try clicking the first textarea/input on the page
            logger.warning("Title input not found with selectors, trying first textarea")
            all_textareas = page.query_selector_all("textarea, input[type='text']")
            if all_textareas:
                title_input = all_textareas[0]
                title_input.click()
                title_input.fill(title)
                logger.info("Title set via first textarea fallback")

        time.sleep(1)

        # Enter content — try multiple strategies
        editor = None
        for selector in [
            ".public-DraftEditor-content",
            "div[contenteditable='true']",
            "[role='textbox']",
            ".WriteIndex-contentEditable [contenteditable='true']",
            ".ql-editor",
            "div.DraftEditor-root",
        ]:
            editor = page.query_selector(selector)
            if editor:
                logger.info(f"Editor found: {selector}")
                break

        if editor:
            editor.click()
            time.sleep(0.5)
            # Clear existing content
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            # Type content
            page.keyboard.type(content, delay=5)
            logger.info("Content entered")
        else:
            # Fallback: try setting content via JavaScript
            logger.warning("Editor not found with selectors, trying JS fallback")
            try:
                page.evaluate("""(text) => {
                    const el = document.querySelector('[contenteditable="true"], .DraftEditor-editorContainer, .public-DraftEditor-content');
                    if (el) {
                        el.focus();
                        el.innerText = text;
                    }
                }""", content)
                logger.info("Content set via JS fallback")
            except Exception as e:
                logger.error(f"JS fallback failed: {e}")

        time.sleep(1)

        # Upload images if provided
        if images:
            self._upload_images(page, images)

        # Add tags if provided
        if tags:
            self._add_tags(page, tags)

        time.sleep(1)

        # Click publish button
        # Note: use text-is for exact match to avoid matching "发布设置"
        publish_btn = None
        for selector in [
            'button:text-is("发布")',
            'button:text-is("发表")',
            'button:text-is("确认发布")',
            'button.PublishButton:text-is("发布")',
            'button[class*="publish"]:not(:has-text("设置"))',
        ]:
            publish_btn = page.query_selector(selector)
            if publish_btn:
                logger.info(f"Publish button found: {selector}")
                # Check if button is visible and enabled
                is_visible = publish_btn.is_visible()
                is_enabled = publish_btn.is_enabled()
                btn_text = publish_btn.inner_text().strip()
                logger.info(f"Button text='{btn_text}', visible={is_visible}, enabled={is_enabled}")
                break

        if not publish_btn:
            # Fallback: find all buttons with "发布" text and pick the last one (usually at the bottom)
            all_publish_btns = page.query_selector_all('button')
            for btn in reversed(all_publish_btns):
                try:
                    text = btn.inner_text().strip()
                    if text == "发布":
                        publish_btn = btn
                        logger.info(f"Fallback: found exact '发布' button")
                        break
                except Exception:
                    continue

        if not publish_btn:
            return {"success": False, "message": "未找到发布按钮"}

        # Scroll button into view and click
        publish_btn.scroll_into_view_if_needed()
        time.sleep(0.5)

        # Try JS click first (more reliable), then fallback to Playwright click
        try:
            publish_btn.evaluate("el => el.click()")
            logger.info("Publish button clicked via JS")
        except Exception:
            publish_btn.click(force=True)
            logger.info("Publish button clicked via force click")
        time.sleep(3)

        # After clicking publish, a confirmation dialog usually appears
        # Try to click the confirm button in the dialog
        confirm_btn = None
        for selector in [
            'button:has-text("确认发布")',
            'button:has-text("确认")',
            'button:has-text("确定发布")',
            'button:has-text("发布文章")',
            '.Modal button:has-text("发布")',
            '.Modal button:has-text("确认")',
            '[role="dialog"] button:has-text("确认")',
            '[role="dialog"] button:has-text("发布")',
            'button[class*="confirm"]',
            'button[class*="Confirm"]',
        ]:
            confirm_btn = page.query_selector(selector)
            if confirm_btn:
                logger.info(f"Confirm button found: {selector}")
                confirm_btn.click()
                time.sleep(3)
                break

        if not confirm_btn:
            logger.info("No confirm dialog found, assuming direct publish")
            time.sleep(3)

        # Screenshot the result
        debug_dir = Path(__file__).parent.parent / "cookies"
        try:
            page.screenshot(path=str(debug_dir / "publish_result.png"), full_page=False)
            # Check if we see success indicators
            body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 300) : ''")
            logger.info(f"Page after publish:\n{body_text[:300]}")
        except Exception:
            pass

        save_browser_cookies(self.context)
        logger.info("Article publish flow completed")
        return {"success": True, "message": "文章发布流程完成，请到知乎主页确认"}

    def _upload_images(self, page: Page, images: list[str]) -> None:
        """Upload images to the article."""
        for img_path in images:
            try:
                # Check if it's a local file or URL
                if img_path.startswith(("http://", "https://")):
                    # For URLs, we need to download and use file input
                    logger.info(f"Processing image URL: {img_path}")
                    # Click image insert button, then use file chooser
                    with page.expect_file_chooser() as fc_info:
                        img_btn = page.query_selector(
                            'button[aria-label*="图片"], button:has-text("图片"), '
                            '.RichEditor-toolbar button:has-text("图")'
                        )
                        if img_btn:
                            img_btn.click()
                        else:
                            # Try tooltip trigger
                            page.click('button[aria-label="插入图片"]', timeout=3000)
                    file_chooser = fc_info.value
                    # Can't directly set URL to file chooser; skip URL images for now
                    logger.warning(f"URL images not directly supported, skipping: {img_path}")
                else:
                    # Local file
                    with page.expect_file_chooser() as fc_info:
                        img_btn = page.query_selector(
                            'button[aria-label*="图片"], button:has-text("图片"), '
                            '.RichEditor-toolbar button:has-text("图")'
                        )
                        if img_btn:
                            img_btn.click()
                        else:
                            page.click('button[aria-label="插入图片"]', timeout=3000)
                    file_chooser = fc_info.value
                    file_chooser.set_files(img_path)
                    logger.info(f"Image uploaded: {img_path}")
                    time.sleep(2)
            except Exception as e:
                logger.warning(f"Failed to upload image {img_path}: {e}")

    def _add_tags(self, page: Page, tags: list[str]) -> None:
        """Add tags to the article."""
        try:
            tag_btn = page.query_selector(
                'button:has-text("添加话题"), button[aria-label*="话题"]'
            )
            if tag_btn:
                tag_btn.click()
                time.sleep(1)
                for tag in tags:
                    search_input = page.query_selector(
                        'input[placeholder*="搜索话题"], .Popover-content input'
                    )
                    if search_input:
                        search_input.fill(tag)
                        time.sleep(1)
                        # Click first result
                        first_option = page.query_selector(
                            '.TopicItem, .Popover-item, .SearchResult-item'
                        )
                        if first_option:
                            first_option.click()
                            time.sleep(0.5)
                # Close topic selector
                page.keyboard.press("Escape")
        except Exception as e:
            logger.warning(f"Failed to add tags: {e}")


# ---------------------------------------------------------------------------
# Publish Video (视频内容)
# ---------------------------------------------------------------------------

class PublishVideoAction:
    """Publish a video to Zhihu."""

    def __init__(self, page: Page, context: BrowserContext):
        self.page = page
        self.context = context

    def publish(self, title: str, content: str, video_path: str,
                tags: list[str] | None = None) -> dict:
        """Publish a video post to Zhihu.

        Args:
            title: post title
            content: post description
            video_path: local path to video file
            tags: list of tags

        Returns:
            result dict
        """
        page = self.page

        # Navigate to write page
        page.goto("https://zhuanlan.zhihu.com/write", wait_until="networkidle")
        time.sleep(2)

        # Try to switch to video tab
        try:
            video_tab = page.query_selector(
                'a:has-text("写视频"), button:has-text("视频"), '
                'div[class*="tab"]:has-text("视频")'
            )
            if video_tab:
                video_tab.click()
                time.sleep(1)
        except Exception:
            pass

        # Upload video file
        try:
            with page.expect_file_chooser() as fc_info:
                upload_btn = page.query_selector(
                    'button:has-text("上传视频"), input[type="file"][accept*="video"]'
                )
                if upload_btn:
                    if upload_btn.get_attribute("type") == "file":
                        # Direct file input
                        upload_btn.set_input_files(video_path)
                    else:
                        upload_btn.click()
                        file_chooser = fc_info.value
                        file_chooser.set_files(video_path)
                else:
                    # Try finding any file input
                    file_input = page.query_selector('input[type="file"]')
                    if file_input:
                        file_input.set_input_files(video_path)
                    else:
                        file_chooser = fc_info.value
                        file_chooser.set_files(video_path)

            logger.info(f"Video file selected: {video_path}")
            # Wait for video upload (can take a while)
            time.sleep(10)
        except Exception as e:
            logger.error(f"Failed to upload video: {e}")
            return {"success": False, "message": f"视频上传失败: {e}"}

        # Enter title
        title_input = page.query_selector(
            "textarea[placeholder*='标题'], input[placeholder*='标题']"
        )
        if title_input:
            title_input.fill(title)

        # Enter description
        desc_input = page.query_selector(
            "textarea[placeholder*='描述'], div[contenteditable='true']"
        )
        if desc_input:
            desc_input.click()
            page.keyboard.type(content, delay=10)

        # Add tags
        if tags:
            try:
                for tag in tags:
                    tag_input = page.query_selector(
                        'input[placeholder*="话题"], input[placeholder*="标签"]'
                    )
                    if tag_input:
                        tag_input.fill(tag)
                        time.sleep(1)
                        page.keyboard.press("Enter")
            except Exception:
                pass

        time.sleep(2)

        # Publish
        publish_btn = page.query_selector(
            'button:has-text("发布"), button:has-text("发表")'
        )
        if publish_btn:
            publish_btn.click()
            time.sleep(5)
            save_browser_cookies(self.context)
            logger.info("Video published successfully")
            return {"success": True, "message": "视频发布成功"}

        return {"success": False, "message": "未找到发布按钮"}


# ---------------------------------------------------------------------------
# Post Comment
# ---------------------------------------------------------------------------

class PostCommentAction:
    """Post a comment on a Zhihu answer/article."""

    def __init__(self, page: Page, context: BrowserContext):
        self.page = page
        self.context = context

    def post_comment(self, url: str, content: str) -> dict:
        """Post a comment on the given Zhihu page.

        Args:
            url: URL of the answer/article
            content: comment text

        Returns:
            result dict
        """
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(2)

        # Find and click comment area
        comment_btn = page.query_selector(
            'button:has-text("评论"), button:has-text("写评论"), '
            'div.ContentItem-action:has-text("评论")'
        )
        if comment_btn:
            comment_btn.click()
            time.sleep(1)

        # Type comment
        comment_input = page.query_selector(
            'textarea[placeholder*="评论"], div[contenteditable="true"][role="textbox"], '
            '.CommentEditor-input textarea, .CommentEditor-input [contenteditable]'
        )
        if comment_input:
            comment_input.click()
            page.keyboard.type(content, delay=10)
            time.sleep(1)

            # Click submit button
            submit_btn = page.query_selector(
                'button:has-text("发布"), button:has-text("提交"), '
                'button.CommentEditor-submit, button[class*="submit"]'
            )
            if submit_btn:
                submit_btn.click()
                time.sleep(2)
                save_browser_cookies(self.context)
                logger.info("Comment posted successfully")
                return {"success": True, "message": "评论发布成功"}

        return {"success": False, "message": "评论发布失败"}


# ---------------------------------------------------------------------------
# Reply Comment
# ---------------------------------------------------------------------------

class ReplyCommentAction:
    """Reply to a specific comment on Zhihu."""

    def __init__(self, page: Page, context: BrowserContext):
        self.page = page
        self.context = context

    def reply_comment(self, url: str, comment_author: str, content: str, target_content: str = "") -> dict:
        """Reply to a comment identified by author name.

        Args:
            url: URL of the page
            comment_author: author of the comment to reply to
            content: reply text

        Returns:
            result dict
        """
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(2)

        # Open comments
        comment_btn = page.query_selector(
            'button:has-text("评论"), div.ContentItem-action:has-text("评论")'
        )
        if comment_btn:
            comment_btn.click()
            time.sleep(2)

        # Find the comment by author and click reply
        comment_items = page.query_selector_all(
            ".CommentItem, .CommentItemV2, .CommentContent"
        )
        target_comment = None
        for item in comment_items:
            if not item.query_selector('button:has-text("回复")'):
                continue
            author_el = item.query_selector(
                ".UserLink-link, .CommentAuthor, a[class*='author']"
            )
            if author_el and comment_author in author_el.inner_text():
                target_comment = item
                break
            if target_content and target_content in item.inner_text():
                target_comment = item
                break

        if not target_comment:
            if target_content:
                matches = page.get_by_text(target_content, exact=True)
                for i in range(matches.count()):
                    candidate = matches.nth(i)
                    try:
                        parent = candidate
                        for _ in range(8):
                            if parent.get_by_text("回复", exact=True).count():
                                target_comment = parent
                                break
                            parent = parent.locator("xpath=..")
                        if target_comment: break
                    except Exception:
                        pass
        if not target_comment:
            return {"success": False, "message": f"未找到用户 {comment_author} 的评论"}

        # Click reply button on that comment
        if hasattr(target_comment, 'element_handle'):
            target_comment = target_comment.element_handle()
        reply_btn = target_comment.query_selector(
            'button:has-text("回复"), button:has-text("回复他"), '
            'button[class*="reply"], span:has-text("回复")'
        )
        if reply_btn:
            reply_btn.click()
            time.sleep(1)

            # Type reply
            # The top-level composer also matches contenteditable; use the
            # editor focused by clicking this comment's Reply button.
            reply_input = page.locator('[contenteditable="true"]:focus, textarea:focus')
            if reply_input.count() != 1:
                return {"success": False, "message": "点击回复后未定位到唯一聚焦的回复输入框，未发送"}
            reply_input = reply_input.element_handle()
            if reply_input:
                reply_input.click()
                page.keyboard.type(content, delay=10)
                time.sleep(1)

                # Submit
                editor = page.locator('[contenteditable="true"]:focus, textarea:focus')
                form = editor
                submit_btn = None
                for _ in range(8):
                    form = form.locator('xpath=..')
                    buttons = form.get_by_role('button', name='发布', exact=True)
                    if buttons.count() == 1 and buttons.is_visible() and buttons.is_enabled():
                        submit_btn = buttons
                        break
                if submit_btn:
                    submit_btn.click()
                    try:
                        page.locator('.CommentContent').get_by_text(content, exact=True).first.wait_for(timeout=15000)
                        if reply_input.inner_text().strip():
                            return {"success": False, "message": "已点击发布但输入框仍有内容，发送状态待核对"}
                    except Exception:
                        return {"success": False, "message": "已点击发布但未确认结果，请核对页面后再重试"}
                    save_browser_cookies(self.context)
                    logger.info("Reply posted successfully")
                    return {"success": True, "message": "回复成功"}

        return {"success": False, "message": "回复失败"}


# ---------------------------------------------------------------------------
# User Profile
# ---------------------------------------------------------------------------

class UserProfileAction:
    """Get user profile information from Zhihu."""

    def __init__(self, page: Page):
        self.page = page

    def get_profile(self, user_url: str) -> dict:
        """Fetch user profile page.

        Args:
            user_url: URL like https://www.zhihu.com/people/xxx

        Returns:
            profile dict
        """
        page = self.page
        page.goto(user_url, wait_until="networkidle")
        time.sleep(2)

        # Username
        name_el = page.query_selector(
            ".ProfileHeader-name, .UserHeader-name, h1.ProfileHeader-name, "
            "span.ProfileHeader-name"
        )
        username = name_el.inner_text().strip() if name_el else ""

        # Bio
        bio_el = page.query_selector(
            ".ProfileHeader-headline, .UserHeader-headline, "
            "div.ProfileHeader-headline"
        )
        bio = bio_el.inner_text().strip() if bio_el else ""

        # Avatar
        avatar_el = page.query_selector(
            ".ProfileHeader-avatar img, .UserHeader-avatar img, "
            "img.Avatar, img[class*='avatar']"
        )
        avatar = avatar_el.get_attribute("src") if avatar_el else ""

        # Stats
        def get_stat(label: str) -> str:
            stat_el = page.query_selector(
                f"div:has(> a[href*='{label}']) strong, "
                f"span:has-text('{label}') + strong, "
                f"a[href*='{label}'] .NumberBoard-value"
            )
            return stat_el.inner_text().strip() if stat_el else "0"

        following = get_stat("following")
        followers = get_stat("followers")
        answers = get_stat("answers")
        articles = get_stat("posts")

        # Recent content
        recent_items = []
        items = page.query_selector_all(
            ".List-item, .ProfileMain-content .ContentItem, "
            ".Profile-activityItem"
        )
        for item in items[:10]:
            try:
                title_el = item.query_selector(
                    ".ContentItem-title a, .ContentItem-title span"
                )
                if title_el:
                    recent_items.append({
                        "title": title_el.inner_text().strip(),
                        "type": "回答/文章",
                    })
            except Exception:
                continue

        return {
            "username": username,
            "bio": bio,
            "avatar": avatar,
            "following": following,
            "followers": followers,
            "answers": answers,
            "articles": articles,
            "recent_content": recent_items,
            "url": user_url,
        }
