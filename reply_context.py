# -*- coding: utf-8 -*-
"""Resolve a stored comment to the independently hosted reply-service contract."""
from db import get_conn


SOURCES = {
    "bilibili_video_comment": ("B站", "bilibili_video", "video_id", "title", "`nickname`"),
    "channels_video_comment": ("视频号", "channels_video", "video_id", "title", "CONCAT('视频号创作者#', LEFT(creator_hash, 6))"),
    "douyin_aweme_comment": ("抖音", "douyin_aweme", "aweme_id", "title", "`nickname`"),
    "kuaishou_video_comment": ("快手", "kuaishou_video", "video_id", "title", "`nickname`"),
    "weibo_note_comment": ("微博", "weibo_note", "note_id", "content", "`nickname`"),
    "xhs_note_comment": ("小红书", "xhs_note", "note_id", "title", "`nickname`"),
    "zhihu_comment": ("知乎", "zhihu_content", "content_id", "title", "`user_nickname`"),
    "tieba_comment": ("贴吧", "tieba_note", "note_id", "title", "`user_nickname`"),
    "reddit_comment": ("Reddit", "reddit_post", "post_id", "title", "CONCAT('reddit#', LEFT(creator_hash, 6))"),
}


def resolve_comment_context(table, rid):
    """Return analysis context for any stored comment, even without an active UP binding."""
    spec = SOURCES.get(table)
    if not spec:
        return None
    platform, content_table, content_id_col, title_col, author_expr = spec
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, CAST(comment_id AS CHAR) comment_id, CAST(content AS CHAR) content, "
                f"CAST(parent_comment_id AS CHAR) parent_comment_id, CAST(`{content_id_col}` AS CHAR) content_id "
                f"FROM `{table}` WHERE id=%s", (rid,))
            comment = cur.fetchone()
            if not comment or not (comment.get("content") or "").strip():
                return None
            cur.execute(
                f"SELECT CAST(`{title_col}` AS CHAR) title, CAST({author_expr} AS CHAR) upname "
                f"FROM `{content_table}` WHERE `{content_id_col}`=%s LIMIT 1", (comment["content_id"],))
            content = cur.fetchone() or {}
            upname = (content.get("upname") or "").strip()
            subject = None
            if upname:
                cur.execute(
                    "SELECT `ID` AS platform_account_id, `UP主ID` AS up_id FROM `平台运营账号` "
                    "WHERE `平台`=%s AND `昵称`=%s AND `状态`='active' AND `UP主ID` IS NOT NULL LIMIT 1",
                    (platform, upname))
                subject = cur.fetchone()
            parent_context = ""
            if comment.get("parent_comment_id"):
                cur.execute(f"SELECT CAST(content AS CHAR) content FROM `{table}` WHERE comment_id=%s LIMIT 1", (comment["parent_comment_id"],))
                parent = cur.fetchone() or {}
                parent_context = (parent.get("content") or "")[:500]
    return {
        "comment_id": comment.get("comment_id") or f"{table}:{rid}", "comment": comment["content"][:2000],
        "platform": platform, "up_id": subject.get("up_id") if subject else None,
        "platform_account_id": subject.get("platform_account_id") if subject else None, "src_table": table,
        "src_pk": rid, "content_title": (content.get("title") or "")[:500],
        "parent_context": parent_context,
    }


def resolve_reply_context(table, rid):
    """Return a context only when automatic reply has a valid active UP binding."""
    context = resolve_comment_context(table, rid)
    return context if context and context.get("up_id") not in (None, "") else None
