"""Service layer for Zhihu MCP — bridges browser actions to MCP tools."""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from browser.manager import create_browser, save_browser_cookies
from zhihu.actions import (
    LoginAction,
    CheckLoginAction,
    SearchAction,
    RecommendFeedAction,
    FeedDetailAction,
    PublishArticleAction,
    PublishVideoAction,
    PostCommentAction,
    ReplyCommentAction,
    UserProfileAction,
)


class ZhihuService:
    """High-level operations for Zhihu automation."""

    # --- Login ---

    def get_login_qrcode(self, headless: bool = True) -> dict:
        """Get QR code for Zhihu login.

        Returns:
            dict: {"qrcode_base64": str, "is_logged_in": bool}
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = LoginAction(page, context)
            result = action.fetch_qrcode()

            if result["is_logged_in"]:
                save_browser_cookies(context)
                return result

            # Start waiting for scan in the same session
            # We return the QR code and rely on check_login_status for polling
            return result

    def login_wait(self, headless: bool = True, timeout: int = 240) -> dict:
        """Open browser, show QR code, wait for user to scan.

        Args:
            headless: run headless or not
            timeout: seconds to wait for scan

        Returns:
            dict: {"success": bool, "message": str}
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = LoginAction(page, context)
            qr = action.fetch_qrcode()

            if qr["is_logged_in"]:
                save_browser_cookies(context)
                logger.info("Already logged in — cookies saved")
                return {"success": True, "message": "已经登录，cookies 已保存"}

            logger.info(f"Please scan the QR code (timeout: {timeout}s)")
            success = action.wait_for_login(timeout)
            if success:
                return {"success": True, "message": "登录成功"}
            return {"success": False, "message": "登录超时或二维码已失效"}

    def check_login_status(self, headless: bool = True) -> dict:
        """Check if currently logged in to Zhihu.

        Returns:
            dict: {"logged_in": bool, "username": str | None}
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = CheckLoginAction(page)
            return action.check()

    # --- Search ---

    def search(self, keyword: str, search_type: str = "综合", limit: int = 20,
               headless: bool = True) -> list[dict]:
        """Search Zhihu content.

        Args:
            keyword: search keyword
            search_type: 综合 | 问题 | 回答 | 文章 | 用户
            limit: max results

        Returns:
            list of result dicts
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = SearchAction(page)
            results = action.search(keyword, search_type, limit)

        # Save results as markdown
        if results:
            self._save_search_results_md(keyword, results)

        return results

    @staticmethod
    def _sanitize_filename(name: str, max_len: int = 50) -> str:
        """Sanitize a string for use as a filename."""
        invalid_chars = '<>:"/\\|?*\n\r\t'
        for ch in invalid_chars:
            name = name.replace(ch, "_")
        return name.strip(" .")[:max_len] or "untitled"

    @staticmethod
    def _save_search_results_md(keyword: str, results: list[dict]) -> Path:
        """Save search results to a markdown file."""
        output_dir = Path(__file__).parent.parent / "search_results"
        output_dir.mkdir(exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_keyword = ZhihuService._sanitize_filename(keyword, 30)
        filename = f"{safe_keyword}_{ts}.md"
        filepath = output_dir / filename

        lines = [
            f"# 搜索结果: {keyword}\n",
            f"> 搜索时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  \n",
            f"> 共 {len(results)} 条结果\n",
            "---\n",
        ]

        for i, item in enumerate(results, 1):
            title = item.get("title", "无标题")
            link = item.get("link", "")
            author = item.get("author", "")
            votes = item.get("votes", "0")
            excerpt = item.get("excerpt", "")

            lines.append(f"## {i}. {title}\n")
            if author:
                lines.append(f"**作者**: {author}  ")
            lines.append(f"**点赞**: {votes}  ")
            if link:
                lines.append(f"**链接**: [{link}]({link})  ")
            lines.append("")
            if excerpt:
                lines.append(f"{excerpt}\n")
            lines.append("---\n")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Search results saved to {filepath}")
        return filepath

    def _save_feed_detail_md(self, detail: dict) -> Path:
        """Save feed detail to a markdown file. Named as {title}_{date}.md"""
        output_dir = Path(__file__).parent.parent / "search_results"
        output_dir.mkdir(exist_ok=True)

        title = detail.get("title", "无标题")
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_title = self._sanitize_filename(title, 50)
        filename = f"{safe_title}_{ts}.md"
        filepath = output_dir / filename

        author = detail.get("author", "")
        content = detail.get("content", "")
        votes = detail.get("votes", "0")
        comment_count = detail.get("comment_count", "0")
        url = detail.get("url", "")
        comments = detail.get("comments", [])

        lines = [
            f"# {title}\n",
        ]
        if author:
            lines.append(f"**作者**: {author}  \n")
        lines.append(f"**点赞**: {votes}  ")
        lines.append(f"**评论数**: {comment_count}  ")
        if url:
            lines.append(f"**链接**: [{url}]({url})  ")
        lines.append(f"\n**获取时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}  ")
        lines.append("\n---\n")
        lines.append("## 正文\n")
        lines.append(f"{content}\n")

        if comments:
            lines.append("\n---\n")
            lines.append("## 评论\n")
            for i, c in enumerate(comments, 1):
                c_author = c.get("author", "")
                c_content = c.get("content", "")
                c_votes = c.get("votes", "0")
                lines.append(f"### {i}. {c_author}\n")
                lines.append(f"{c_content}\n")
                if c_votes != "0":
                    lines.append(f"点赞: {c_votes}\n")
                lines.append("")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Feed detail saved to {filepath}")
        return filepath

    # --- Recommend ---

    def recommend(self, scroll_count: int = 3, limit: int = 20,
                  headless: bool = True) -> list[dict]:
        """Get recommended feeds from homepage.

        Returns:
            list of feed dicts
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = RecommendFeedAction(page)
            return action.get_recommend_list(scroll_count, limit)

    # --- Feed Detail ---

    def get_feed_detail(self, url: str, load_comments: bool = True,
                        comment_limit: int = 20, headless: bool = True) -> dict:
        """Get detail of a Zhihu answer/article.

        Args:
            url: full URL to the content
            load_comments: whether to load comments
            comment_limit: max comments

        Returns:
            detail dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = FeedDetailAction(page)
            detail = action.get_detail(url, load_comments, comment_limit)

        # Save detail as markdown
        if detail.get("title"):
            self._save_feed_detail_md(detail)

        return detail

    # --- Publish ---

    def publish_article(self, title: str, content: str,
                        images: list[str] | None = None,
                        tags: list[str] | None = None,
                        headless: bool = True) -> dict:
        """Publish an article to Zhihu.

        Args:
            title: article title
            content: article body
            images: image paths or URLs
            tags: topic tags

        Returns:
            result dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = PublishArticleAction(page, context)
            return action.publish(title, content, images, tags)

    def publish_video(self, title: str, content: str, video_path: str,
                      tags: list[str] | None = None,
                      headless: bool = True) -> dict:
        """Publish a video to Zhihu.

        Args:
            title: video title
            content: video description
            video_path: local video file path
            tags: topic tags

        Returns:
            result dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = PublishVideoAction(page, context)
            return action.publish(title, content, video_path, tags)

    # --- Comments ---

    def post_comment(self, url: str, content: str,
                     headless: bool = True) -> dict:
        """Post a comment on a Zhihu page.

        Args:
            url: page URL
            content: comment text

        Returns:
            result dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = PostCommentAction(page, context)
            return action.post_comment(url, content)

    def reply_comment(self, url: str, comment_author: str, content: str,
                      headless: bool = True, target_content: str = "") -> dict:
        """Reply to a comment.

        Args:
            url: page URL
            comment_author: author of comment to reply to
            content: reply text

        Returns:
            result dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = ReplyCommentAction(page, context)
            return action.reply_comment(url, comment_author, content, target_content=target_content)

    # --- User Profile ---

    def get_user_profile(self, user_url: str,
                         headless: bool = True) -> dict:
        """Get user profile.

        Args:
            user_url: user profile URL

        Returns:
            profile dict
        """
        with create_browser(headless=headless) as (browser, context, page):
            action = UserProfileAction(page)
            return action.get_profile(user_url)
