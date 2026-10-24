"""多平台评论/内容表的统一映射和 SCRM 展示常量。"""
from config import COMMENT_TABLES


# cid=评论表中关联内容的业务 ID 列；其余字段为统一查询所需的列或 SQL 表达式。
PLATFORM_MAP = {
    "bilibili_video_comment": dict(platform="B站", ctable="bilibili_video", cid="video_id", nick="nickname", time="create_time", likes="like_count", ctitle="title", curl="video_url", ccover="video_cover_url", cnick="`nickname`", ccomment="video_comment", cduration="duration", cview="video_play_count", clike="liked_count", ccollect="collected_count", cshare="NULL", cpubtime="create_time"),
    "channels_video_comment": dict(platform="视频号", ctable="channels_video", cid="video_id", nick="nickname", time="create_time", likes="like_count", ctitle="title", curl="CONCAT('https://channels.weixin.qq.com/web/pages/feed?eid=', REPLACE(COALESCE(NULLIF(`export_id`, ''), `video_id`), '/', '%%2F'))", ccover="cover_url", cnick="CONCAT('视频号创作者#', LEFT(creator_hash, 6))", ccomment="comment_count", cduration="video_play_len", cview="read_count", clike="like_count", ccollect="fav_count", cshare="forward_count", cpubtime="create_time"),
    "douyin_aweme_comment": dict(platform="抖音", ctable="douyin_aweme", cid="aweme_id", nick="nickname", time="create_time", likes="like_count", ctitle="title", curl="aweme_url", ccover="cover_url", cnick="`nickname`", ccomment="comment_count", cduration="NULL", cview="NULL", clike="liked_count", ccollect="collected_count", cshare="share_count", cpubtime="create_time"),
    "kuaishou_video_comment": dict(platform="快手", ctable="kuaishou_video", cid="video_id", nick="nickname", time="create_time", likes="like_count", ctitle="title", curl="video_url", ccover="video_cover_url", cnick="`nickname`", ccomment="NULL", cduration="NULL", cview="viewd_count", clike="liked_count", ccollect="NULL", cshare="NULL", cpubtime="create_time"),
    "weibo_note_comment": dict(platform="微博", ctable="weibo_note", cid="note_id", nick="nickname", time="create_time", likes="like_count", ctitle="content", curl="note_url", ccover="NULL", cnick="`nickname`", ccomment="comments_count", cduration="NULL", cview="NULL", clike="liked_count", ccollect="NULL", cshare="shared_count", cpubtime="create_time"),
    "xhs_note_comment": dict(platform="小红书", ctable="xhs_note", cid="note_id", nick="nickname", time="create_time", likes="like_count", ctitle="title", curl="note_url", ccover="NULL", cnick="`nickname`", ccomment="comment_count", cduration="NULL", cview="NULL", clike="liked_count", ccollect="collected_count", cshare="share_count", cpubtime="time"),
    "zhihu_comment": dict(platform="知乎", ctable="zhihu_content", cid="content_id", nick="user_nickname", time="add_ts", likes="like_count", ctitle="title", curl="content_url", ccover="NULL", cnick="`user_nickname`", ccomment="comment_count", cduration="NULL", cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="created_time"),
    "tieba_comment": dict(platform="贴吧", ctable="tieba_note", cid="note_id", nick="user_nickname", time="add_ts", likes="0", ctitle="title", curl="note_url", ccover="NULL", cnick="`user_nickname`", ccomment="total_replay_num", cduration="NULL", cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="publish_time"),
    "reddit_comment": dict(platform="Reddit", ctable="reddit_post", cid="post_id", nick="nickname", time="add_ts", likes="score", ctitle="title", curl="post_url", ccover="NULL", cnick="CONCAT('reddit#', LEFT(creator_hash, 6))", ccomment="comment_count", cduration="NULL", cview="NULL", clike="NULL", ccollect="NULL", cshare="NULL", cpubtime="created_utc"),
}

COMMENT_TABLES_ENABLED = [table for table in COMMENT_TABLES if table in PLATFORM_MAP]
PLATFORM_OF = {table: PLATFORM_MAP[table]["platform"] for table in COMMENT_TABLES_ENABLED}

LEAD_STATUS = [("new", "待联系", "#6b7280"), ("contacted", "已联系", "#4f46e5"), ("following", "跟进中", "#0d9488"), ("feedback", "客户反馈", "#d97706"), ("won", "已成交", "#16a34a"), ("lost", "已流失", "#9ca3af")]
LEAD_STATUS_MAP = {status: label for status, label, _ in LEAD_STATUS}
BRAND_STATUS = [("new", "新品牌", "#6b7280"), ("touching", "已触达", "#4f46e5"), ("intent", "有意向", "#d97706"), ("cooperate", "合作中", "#16a34a"), ("lost", "暂未合作", "#9ca3af")]
BRAND_STATUS_MAP = {status: label for status, label, _ in BRAND_STATUS}
TEAM = ["希乙", "路飞", "晓敏", "陈晨"]
KPI_PLATFORMS = ["B站", "抖音", "视频号", "小红书", "快手", "微博", "知乎"]
