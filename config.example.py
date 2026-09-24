# -*- coding: utf-8 -*-
"""配置文件模板
使用方法:复制本文件为 config.py,填入你自己的 MySQL 密码与 DeepSeek API key
注意:config.py 已被 .gitignore 排除,不会被提交,请勿手动上传
"""
import pymysql
from pymysql.cursors import DictCursor

# ---------- MySQL ----------
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "你的MySQL密码",
    "db": "media_crawler_pro",
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
}

# ---------- DeepSeek API ----------
DEEPSEEK_API_KEY = "sk-你的DeepSeek API key"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# ---------- 分析守护进程 ----------
ANALYZE_INTERVAL = 8        # 轮询间隔(秒)
ANALYZE_BATCH = 5            # 每轮每表处理条数
ANALYZE_MAX_TOKENS = 200
ANALYZE_TEMPERATURE = 0.85

# ---------- 评论表清单(需 AI 分析的表)----------
COMMENT_TABLES = [
    "bilibili_video_comment",
    "douyin_aweme_comment",
    "kuaishou_video_comment",
    "weibo_note_comment",
    "xhs_note_comment",
    "zhihu_comment",
    "tieba_comment",
    "reddit_comment",
]

# ---------- 新增分析字段定义(迁移用)----------
COMMENT_NEW_COLUMNS = [
    ("analyzed", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已分析(0:否 1:是)'"),
    ("negative_alert", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '负面预警(0:正常 1:负面)'"),
    ("purchase_intent", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '购买意向(0:无 1:有)'"),
    ("reply_suggestion", "TEXT NULL COMMENT 'AI生成的回复推荐'"),
]
