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

# ---------- 本地原型/离线工具的 DeepSeek API（运营服务运行时不再使用） ----------
# 线上评论分析与回复统一由 REPLY_SERVICE_URL 指向的独立服务负责。
DEEPSEEK_API_KEY = "sk-你的DeepSeek API key"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# ---------- 分析守护进程 ----------
ANALYZE_INTERVAL = 30       # 轮询间隔(秒):事件驱动式快扫,抓到新评论尽快分析
ANALYZE_BATCH = 20           # 每轮每表处理条数,0=不限制;快扫建议限制小批量避免堆积时拖太久
ANALYZE_FULL_SWEEP = 3600    # 全量补扫间隔(秒),周期性把快扫批次外遗留的待分析清完
ANALYZE_MAX_AGE_DAYS = 90    # 自动分析只处理最近 N 天内的评论(0=不限制);手动分析不受此限制
ANALYZE_MAX_TOKENS = 200
ANALYZE_TEMPERATURE = 0.85

# ---------- 独立评论决策与回复服务 ----------
# 该服务必须一次返回结构化分析和回复决策；本项目不会回退为直连模型。
REPLY_SERVICE_URL = "http://127.0.0.1:5100"
REPLY_SERVICE_API_KEY = ""
REPLY_SERVICE_TIMEOUT_SECONDS = 70

# ---------- 常驻回复调度与待确认核验 ----------
# 审核通过的任务进入 queued，由 5000 服务内的常驻调度器领取；服务重启后自动恢复。
REPLY_DISPATCH_INTERVAL_SECONDS = 10
# 写入结果不明确时，延迟回扫；核验只会确认或保留待人工处理，绝不自动重发。
REPLY_VERIFY_DELAY_SECONDS = 1800

# ---------- 统一写操作风控（保守默认）----------
REPLY_MIN_INTERVAL_SECONDS = 120
REPLY_HOURLY_LIMIT = 8
REPLY_DAILY_LIMIT = 30
REPLY_ACTIVE_HOURS = "09:00-22:00"
REPLY_RISK_COOLDOWN_SECONDS = 3600

# ---------- 飞书负面预警(自建应用,留空则不发送)----------
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""
FEISHU_CHAT_ID = ""        # 目标群聊 chat_id(机器人需已入群)
# 商单创建后的账户通知。留空时先复用 FEISHU_CHAT_ID 作为测试群。
FEISHU_DEAL_NOTIFY_CHAT_ID = ""
# 对外可访问的登录页地址；留空时由网页请求的当前地址自动生成。
SCRM_LOGIN_URL = ""

# ---------- 评论表清单(需 AI 分析的表)----------
COMMENT_TABLES = [
    "bilibili_video_comment",
    "channels_video_comment",
    "douyin_aweme_comment",
    "kuaishou_video_comment",
    "weibo_note_comment",
    "xhs_note_comment",
    "zhihu_comment",
    "tieba_comment",
    "reddit_comment",
]

CONTENT_TABLES = [
    "bilibili_video", "channels_video", "douyin_aweme", "kuaishou_video",
    "weibo_note", "xhs_note", "zhihu_content", "tieba_note", "reddit_post",
]

# ---------- 新增分析字段定义(迁移用)----------
COMMENT_NEW_COLUMNS = [
    ("analyzed", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已分析(0:否 1:是)'"),
    ("analysis_claimed_ts", "BIGINT NULL COMMENT '分析任务领取时间；异常超时后可重试'"),
    ("negative_alert", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '负面预警(0:正常 1:负面)'"),
    ("purchase_intent", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '购买意向(0:无 1:有)'"),
    ("comment_type", "VARCHAR(64) NULL COMMENT '评论类型'"),
    ("reply_suggestion", "TEXT NULL COMMENT 'AI生成的回复推荐'"),
    ("handled", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '人工已处理(0:否 1:是,已处理不再自动分析)'"),
    ("no_action", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '无需操作(0:否 1:是)'"),
    ("reviewed", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '人工已审核(0:否 1:是)'"),
    ("human_review", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '系统建议转人工(0:否 1:是)'"),
    ("delete_requested", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '请求自动删除(0:否 1:是)'"),
    ("high_negative", "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '高级负向(0:否 1:是,高风险评论类型自动标注)'"),
]

# 飞书负面预警发送开关；不影响评论分析及询单同步
FEISHU_ALERT_ENABLED = False
