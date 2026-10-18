"""MCP tool definitions for zhihu-mcp.

Registers all tools with the FastMCP server.
"""

from __future__ import annotations

from fastmcp import FastMCP

from zhihu.service import ZhihuService
from zhihu.cookies import delete_cookies as _delete_cookies

HEADLESS = True  # Updated by main.py at startup


def create_mcp_server(headless: bool = True) -> FastMCP:
    """Create and configure the FastMCP server with all Zhihu tools."""
    global HEADLESS
    HEADLESS = headless

    mcp = FastMCP("zhihu-mcp", version="1.0.0")
    svc = ZhihuService()

    # ------------------------------------------------------------------
    # 1. Login & Auth
    # ------------------------------------------------------------------

    @mcp.tool(description="检查知乎登录状态。无参数，直接调用即可返回当前是否已登录及用户名。")
    def check_login_status() -> dict:
        """Check Zhihu login status."""
        return svc.check_login_status(headless=HEADLESS)

    @mcp.tool(description="获取知乎登录二维码。返回 Base64 编码的二维码图片，用户需使用知乎 App 扫码登录。")
    def get_login_qrcode() -> dict:
        """Get QR code for Zhihu login."""
        return svc.get_login_qrcode(headless=HEADLESS)

    @mcp.tool(description="删除 cookies 文件，重置登录状态。删除后需要重新扫码登录。")
    def delete_cookies() -> dict:
        """Delete saved cookies."""
        deleted = _delete_cookies()
        if deleted:
            return {"success": True, "message": "Cookies 已删除，请重新登录"}
        return {"success": False, "message": "没有找到 cookies 文件"}

    # ------------------------------------------------------------------
    # 2. Publish Article (图文)
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "发布图文内容到知乎。参数："
            "title（标题，必填）、"
            "content（正文内容，必填）、"
            "images（图片路径列表，可选，支持本地绝对路径）、"
            "tags（话题标签列表，可选）。"
            "示例：title='我的文章', content='正文内容...', images=['/path/to/img.jpg']"
        )
    )
    def publish_article(
        title: str,
        content: str,
        images: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Publish an article with images to Zhihu."""
        return svc.publish_article(title, content, images, tags, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 3. Publish Video (视频)
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "发布视频内容到知乎。参数："
            "title（标题，必填）、"
            "content（视频描述，必填）、"
            "video_path（本地视频文件绝对路径，必填）、"
            "tags（话题标签列表，可选）。"
            "仅支持本地视频文件，不支持 URL。"
        )
    )
    def publish_video(
        title: str,
        content: str,
        video_path: str,
        tags: list[str] | None = None,
    ) -> dict:
        """Publish a video to Zhihu."""
        return svc.publish_video(title, content, video_path, tags, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 4. Search
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "搜索知乎内容。参数："
            "keyword（搜索关键词，必填）、"
            "search_type（搜索类型：综合/问题/回答/文章/用户，默认综合）、"
            "limit（最大结果数，默认 20）。"
        )
    )
    def search_content(
        keyword: str,
        search_type: str = "综合",
        limit: int = 20,
    ) -> dict:
        """Search Zhihu content by keyword."""
        results = svc.search(keyword, search_type, limit, headless=HEADLESS)
        return {"count": len(results), "results": results}

    # ------------------------------------------------------------------
    # 5. Recommend Feed
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "获取知乎首页推荐列表。参数："
            "scroll_count（滚动加载次数，默认 3）、"
            "limit（最大返回条数，默认 20）。"
            "无需登录也可使用，但登录后推荐更精准。"
        )
    )
    def get_recommend_list(
        scroll_count: int = 3,
        limit: int = 20,
    ) -> list[dict]:
        """Get Zhihu homepage recommended feeds."""
        return svc.recommend(scroll_count, limit, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 6. Feed Detail
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "获取知乎帖子详情（包括互动数据和评论）。参数："
            "url（帖子完整 URL，必填，从搜索结果或推荐列表获取）、"
            "load_comments（是否加载评论，默认 true）、"
            "comment_limit（最大评论数，默认 20）。"
        )
    )
    def get_feed_detail(
        url: str,
        load_comments: bool = True,
        comment_limit: int = 20,
    ) -> dict:
        """Get Zhihu post detail with interactions and comments."""
        return svc.get_feed_detail(url, load_comments, comment_limit, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 7. Post Comment
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "发表评论到知乎帖子。参数："
            "url（帖子完整 URL，必填）、"
            "content（评论内容，必填）。"
            "需要先登录才能发表评论。"
        )
    )
    def post_comment(
        url: str,
        content: str,
    ) -> dict:
        """Post a comment on a Zhihu post."""
        return svc.post_comment(url, content, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 8. User Profile
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "获取知乎用户个人主页信息。参数："
            "user_url（用户主页完整 URL，必填，格式如 https://www.zhihu.com/people/xxx）。"
            "返回用户基本信息、粉丝数、关注数、最近内容等。"
        )
    )
    def get_user_profile(user_url: str) -> dict:
        """Get Zhihu user profile information."""
        return svc.get_user_profile(user_url, headless=HEADLESS)

    # ------------------------------------------------------------------
    # 9. Reply Comment
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "回复知乎帖子下的指定评论。参数："
            "url（帖子完整 URL，必填）、"
            "comment_author（要回复的评论作者昵称，必填）、"
            "content（回复内容，必填）。"
            "需要先登录才能回复评论。"
        )
    )
    def reply_comment(
        url: str,
        comment_author: str,
        content: str,
    ) -> dict:
        """Reply to a specific comment on Zhihu."""
        return svc.reply_comment(url, comment_author, content, headless=HEADLESS)

    return mcp
