# -*- coding: utf-8 -*-
"""知识库模块 · 底表结构与初始化
依据《评论自动回复系统-知识库结构与技术路径》§1/附录 A 落地:
- L0 策略层: 复用 UP主管理 / persona_pack / routing_matrix / guardrails
- L1 事实层: model_facts(护城河, specs 必须带 condition 三元组)
- L1.5 类目能力层: category_pain_points / category_pain_point_params
- L2 知识层: glossary / misconception
- L3 语料层: banned_phrase
- L4 运营层: reply_log(retrieved 必存, 四闸结果入 detector)
所有 JSON 字段在 Python 侧用 dict/list 读写, 数据库存 JSON 文本。
"""
import json
import logging
import re
import time
from datetime import datetime

from db import get_conn

logger = logging.getLogger("knowledge")

# ---------- DDL(知识库底表, 幂等)----------
KB_TABLES = {
    # L0 策略
    "persona_pack": """
        CREATE TABLE IF NOT EXISTS `persona_pack` (
          `up_id`          BIGINT      NOT NULL COMMENT '关联 UP主管理.id',
          `params`         JSON        NOT NULL COMMENT '人设参数, 禁止提示词写自然语言',
          `fingerprint`    JSON        NOT NULL COMMENT '账号指纹, 生成后校验用',
          `few_shot_slots` TINYINT     NOT NULL DEFAULT 3 COMMENT '注入几条真人文案',
          `version`        VARCHAR(16) NOT NULL DEFAULT 'v1',
          `updated_ts`     INT         NOT NULL,
          PRIMARY KEY (`up_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L0 人设参数包'
    """,
    "routing_matrix": """
        CREATE TABLE IF NOT EXISTS `routing_matrix` (
          `id`            INT AUTO_INCREMENT,
          `comment_type`  VARCHAR(32) NOT NULL COMMENT '10 类评论之一',
          `account_level` ENUM('L1','L2','L3','L4','L5') NOT NULL,
          `reply_rate`    DECIMAL(3,2) NOT NULL DEFAULT 0.70 COMMENT '该组合回复率',
          `target_len`    VARCHAR(16) NULL COMMENT '长度区间, 如 20-40, 覆盖 persona 默认',
          `human_required` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否强制人工',
          `extra_rule`    JSON        NULL COMMENT '可校验规则, 如 must_ask_back/must_have_sequence',
          PRIMARY KEY (`id`),
          UNIQUE KEY `uk_type_level` (`comment_type`,`account_level`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L0 路由矩阵'
    """,
    "guardrails": """
        CREATE TABLE IF NOT EXISTS `guardrails` (
          `rule_id`   VARCHAR(16) NOT NULL,
          `gtype`     VARCHAR(16) NOT NULL COMMENT '价格/安全/健康/竞品人格/施工/承诺/未测型号',
          `trigger_pattern` TEXT    NOT NULL COMMENT '正则或触发词(JSON 数组)',
          `action`    ENUM('block','rewrite','redirect') NOT NULL COMMENT 'block 进人工 / rewrite 二次生成 / redirect 标准引导',
          `rewrite_to` TEXT       NULL COMMENT 'rewrite/redirect 时的替换文本',
          `note`      VARCHAR(255) NULL,
          `enabled`   TINYINT(1)  NOT NULL DEFAULT 1,
          PRIMARY KEY (`rule_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L0 硬红线'
    """,
    # L1 事实(护城河)
    "model_facts": """
        CREATE TABLE IF NOT EXISTS `model_facts` (
          `model_id`     VARCHAR(64) NOT NULL COMMENT '内部 ID, 如 MDS-FZ-35T2W',
          `brand`        VARCHAR(32) NOT NULL,
          `model_name`   VARCHAR(128) NOT NULL COMMENT '完整型号串',
          `aliases`      JSON        NOT NULL COMMENT '俗称/缩写/错拼, 用于匹配评论',
          `category`     VARCHAR(16) NOT NULL COMMENT '空调/冰箱/洗护/清洁/净水/空净',
          `launch_date`  DATE        NULL,
          `price_min`    MEDIUMINT UNSIGNED NULL COMMENT '价格区间下限（元），不存实时售价',
          `price_max`    MEDIUMINT UNSIGNED NULL COMMENT '价格区间上限（元），不存实时售价',
          `price_step`   ENUM('300','500') NULL COMMENT '价格区间颗粒度（元）',
          `specs`        JSON        NOT NULL COMMENT '三元组: value+unit+condition(+standard)',
          `known_issues` JSON        NULL COMMENT '已知问题, 带出现条件',
          `tested_by`    JSON        NOT NULL COMMENT '哪些号测过/怎么测的, 防冒用亲历',
          `source`       JSON        NOT NULL COMMENT '来源: 官网/说明书/自测/国标备案',
          `confidence`   ENUM('high','mid','low') NOT NULL DEFAULT 'mid',
          `verified_at`  DATE        NOT NULL COMMENT '核实日期',
          `expires_at`   DATE        NULL COMMENT '过期自动降级 reference_only',
          `state`        ENUM('pending','usable','reference_only') NOT NULL DEFAULT 'usable' COMMENT 'pending=待人工确认(摄取管道写入)/usable=可用/reference_only=仅参考',
          PRIMARY KEY (`model_id`),
          KEY `idx_mf_brand_cat` (`brand`,`category`),
          KEY `idx_mf_name` (`model_name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L1 型号事实表(核心)'
    """,
    # L1.5 类目能力：不绑定品牌或型号。一个痛点可以由 1-N 个参数共同佐证。
    "category_pain_points": """
        CREATE TABLE IF NOT EXISTS `category_pain_points` (
          `pain_point_id` VARCHAR(64) NOT NULL COMMENT '稳定主键，如 air_cleaning_noise',
          `category`      VARCHAR(32) NOT NULL COMMENT '产品类目；与品牌、型号无关',
          `pain_point`    VARCHAR(128) NOT NULL COMMENT '用户感知的问题/顾虑',
          `description`   TEXT         NULL COMMENT '痛点边界与适用场景',
          `trigger_words` JSON         NOT NULL COMMENT '用户表达触发词，含同义说法',
          `reply_guidance` TEXT        NULL COMMENT '回复时应解释的结论或追问方向',
          `star_rating`   TINYINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '痛点星级，1-5 星',
          `priority`      INT          NOT NULL DEFAULT 50 COMMENT '多条命中时的排序权重',
          `state`         ENUM('usable','disabled') NOT NULL DEFAULT 'usable',
          `created_ts`    INT          NOT NULL,
          `updated_ts`    INT          NOT NULL,
          PRIMARY KEY (`pain_point_id`),
          KEY `idx_cpp_category_state` (`category`,`state`,`priority`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L1.5 产品类目痛点库（品牌型号无关）'
    """,
    "category_pain_point_params": """
        CREATE TABLE IF NOT EXISTS `category_pain_point_params` (
          `mapping_id`       BIGINT AUTO_INCREMENT,
          `pain_point_id`    VARCHAR(64) NOT NULL COMMENT '关联 category_pain_points.pain_point_id',
          `param_key`        VARCHAR(64) NOT NULL COMMENT '对齐 model_facts.specs 的参数键',
          `param_name`       VARCHAR(64) NOT NULL COMMENT '面向运营的参数中文名',
          `param_aliases`    JSON        NULL COMMENT '评论中识别该参数的别名',
          `evidence_rule`    TEXT        NOT NULL COMMENT '该参数如何佐证或反证痛点，含必要条件',
          `interpretation`   TEXT        NULL COMMENT '生成回复时如何把参数翻译成用户感受',
          `sort_order`       SMALLINT   NOT NULL DEFAULT 10,
          `state`            ENUM('pending','usable','disabled') NOT NULL DEFAULT 'usable',
          PRIMARY KEY (`mapping_id`),
          UNIQUE KEY `uk_cppp` (`pain_point_id`,`param_key`),
          KEY `idx_cppp_pain_state` (`pain_point_id`,`state`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='类目痛点参数证据映射（1个痛点对应N个参数）'
    """,
    "model_pain_point_matches": """
        CREATE TABLE IF NOT EXISTS `model_pain_point_matches` (
          `match_id`        BIGINT AUTO_INCREMENT,
          `model_id`        VARCHAR(64) NOT NULL COMMENT '关联 model_facts.model_id',
          `pain_point_id`   VARCHAR(64) NOT NULL COMMENT '关联 category_pain_points.pain_point_id',
          `matched_params`  JSON        NOT NULL COMMENT '命中的参数键、参数名与该型号实际参数值',
          `star_rating`     TINYINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '该产品下痛点星级，1-5 星',
          `standard_reply`  TEXT        NULL COMMENT '该产品的标准回复',
          `match_source`    ENUM('auto','manual') NOT NULL DEFAULT 'auto',
          `note`            TEXT        NULL COMMENT '人工补充的适用条件或说明',
          `state`           ENUM('usable','disabled') NOT NULL DEFAULT 'usable',
          `created_ts`      INT         NOT NULL,
          `updated_ts`      INT         NOT NULL,
          PRIMARY KEY (`match_id`),
          UNIQUE KEY `uk_mppm` (`model_id`,`pain_point_id`),
          KEY `idx_mppm_model_state` (`model_id`,`state`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='产品参数自动匹配的痛点结果，可人工维护'
    """,
    # L2 知识
    "glossary": """
        CREATE TABLE IF NOT EXISTS `glossary` (
          `term`         VARCHAR(64) NOT NULL,
          `alias`        JSON        NULL,
          `unit`         VARCHAR(16) NULL,
          `define`       TEXT        NOT NULL,
          `read`         TEXT        NULL COMMENT '怎么读这个数/怎么看这个指标',
          `trap`         TEXT        NULL COMMENT '常见误读',
          `level`        VARCHAR(16) NOT NULL DEFAULT 'banned' COMMENT 'banned 级别一律不可见',
          `standard_version` VARCHAR(32) NULL COMMENT '所属标准版本, 换版时批量定位',
          `account_visibility` JSON NULL COMMENT '哪些号可以说, NULL=全部',
          `updated_at`   DATE        NULL,
          PRIMARY KEY (`term`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L2 术语库'
    """,
    "misconception": """
        CREATE TABLE IF NOT EXISTS `misconception` (
          `topic_id`          VARCHAR(32) NOT NULL,
          `trigger_words`     JSON        NOT NULL COMMENT '触发词',
          `trigger_synonyms`  JSON        NULL COMMENT '同义扩展, 用于召回',
          `priority`          INT         NOT NULL DEFAULT 50 COMMENT '命中多条时取 priority 高的, 最多取 top-2',
          `truth`             TEXT        NOT NULL,
          `condition`         TEXT        NULL,
          `exception`         TEXT        NULL,
          `tpl_by_level`      JSON        NOT NULL COMMENT '按账号层级的回复模板',
          `l_level_ok`        JSON        NULL COMMENT '哪些层级可用, NULL=全部',
          `needs_human`       TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '标准解读争议等强制人工',
          `verified_at`       DATE        NOT NULL,
          PRIMARY KEY (`topic_id`),
          KEY `idx_mis_priority` (`priority`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L2 认知纠正库(上限 80 条)'
    """,
    # L3 语料
    "banned_phrase": """
        CREATE TABLE IF NOT EXISTS `banned_phrase` (
          `phrase_id`  INT AUTO_INCREMENT,
          `phrase`     VARCHAR(128) NOT NULL,
          `scope`      ENUM('global','account') NOT NULL DEFAULT 'global',
          `up_id`       BIGINT       NOT NULL DEFAULT 0 COMMENT "scope=account 时关联 UP主管理.id, 0=全局",
          `note`       VARCHAR(255) NULL,
          `created_ts` INT          NOT NULL,
          PRIMARY KEY (`phrase_id`),
          UNIQUE KEY `uk_bp` (`phrase`,`scope`,`up_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L3 禁用句(全局级+账号级防串号)'
    """,
    # L4 运营
    "reply_log": """
        CREATE TABLE IF NOT EXISTS `reply_log` (
          `reply_id`       VARCHAR(64) NOT NULL,
          `comment_id`     VARCHAR(64) NOT NULL COMMENT '平台评论 ID',
          `src_table`      VARCHAR(64) NULL COMMENT '关联评论表(衔接现有系统)',
          `src_pk`         INT         NULL,
          `up_id`          BIGINT       NOT NULL COMMENT '关联 UP主管理.id',
          `platform_account_id` VARCHAR(255) NULL COMMENT '关联平台运营账号.ID',
          `comment_type`   VARCHAR(32) NULL COMMENT '10 类判定结果',
          `type_conf`      DECIMAL(4,3) NULL COMMENT '分类置信度',
          `retrieved`      JSON        NOT NULL COMMENT '命中知识条目 ID 列表, 必存, 排障唯一入口',
          `prompt_version` VARCHAR(32) NULL,
          `model`          VARCHAR(32) NULL,
          `draft`          TEXT        NULL COMMENT '模型原始输出',
          `detector`       JSON        NULL COMMENT '四道闸逐项结果',
          `action`         ENUM('auto_sent','human_edited','rejected','abandoned') NULL,
          `final_text`     TEXT        NULL,
          `rejection_code` VARCHAR(4)  NULL COMMENT 'R01-R12 固定码',
          `editor`         VARCHAR(32) NULL,
          `latency_ms`     INT         NULL,
          `created_at`     INT         NOT NULL,
          PRIMARY KEY (`reply_id`),
          KEY `idx_rl_code` (`rejection_code`,`created_at`),
          KEY `idx_rl_up` (`up_id`,`created_at`),
          KEY `idx_rl_comment` (`src_table`,`src_pk`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='L4 回复日志(含 retrieved 与四闸结果)'
    """,
}

# ---------- 种子数据 ----------
# 硬红线 G-01~G-07(文档 §2.4);trigger_pattern 为 JSON 数组, 匹配侧自行决定正则或包含
GUARDRAIL_SEEDS = [
    ("G-01", "价格", ["多少钱", "降价", "哪个平台", "最低价", "便宜"],
     "rewrite", "价格我不跟,行情天天变,建议大促节点自己去平台看",
     "价格类统一改写,不报价不跟价"),
    ("G-02", "安全", ["漏电", "起火", "燃气", "触电"],
     "block", None, "安全类一律 block 进人工"),
    ("G-03", "健康", ["治", "鼻炎", "过敏", "孕妇", "婴儿", "杀菌治病"],
     "block", None, "健康宣称类 block 进人工"),
    ("G-04", "竞品人格", None,
     "block", None, "品牌+垃圾/骗子/黑心得组合命中,见 trigger_detail"),
    ("G-05", "施工", ["改电路", "自己装", "接线"],
     "rewrite", "这类涉及电路的还是建议找品牌官方售后,别自己动",
     "施工类引导品牌售后"),
    ("G-06", "承诺", ["绝对", "保证", "一定能", "包修好"],
     "block", None, "绝对化承诺 block"),
    ("G-07", "未测型号", None,
     "rewrite", "这台我没测过,只能跟你说说参数层面的事",
     "命中 model_facts 之外的型号+断言语气时降级;判定逻辑见事实闸"),
]

# 路由矩阵初始 17 类 × 默认回复率；账号级参数包可在运行时进一步覆盖。
COMMENT_TYPES = [
    "求推荐", "参数求解释", "故障求助", "已购反馈", "价格质疑", "恰饭质疑",
    "专业质疑", "恶意质疑", "品类争论", "灌水玩梗", "无关灌水",
    "导购广告", "竞品咨询", "竞品对比", "竞品引流", "攻击引战", "产品差评",
]

ROUTING_SEEDS = [
    # (comment_type, extra_rule) —— 规则必须可校验化
    ("灌水玩梗",     json.dumps({"max_len": 10}, ensure_ascii=False)),
    ("无关灌水",     json.dumps({"action": "no_reply", "moderation": "ignore"}, ensure_ascii=False)),
    ("求推荐",       json.dumps({"must_ask_back": True}, ensure_ascii=False)),
    ("故障求助",     json.dumps({"must_have_sequence": True}, ensure_ascii=False)),
    ("价格质疑",     json.dumps({"no_price": True}, ensure_ascii=False)),
    ("恰饭质疑",     json.dumps({"human_required": True}, ensure_ascii=False)),
    ("专业质疑",     json.dumps({"human_required": True}, ensure_ascii=False)),
    ("恶意质疑",     json.dumps({"human_required": True, "action": "hold"}, ensure_ascii=False)),
    ("品类争论",     json.dumps({"only_state_not_debate": True}, ensure_ascii=False)),
    ("导购广告",     json.dumps({"action": "no_reply", "moderation": "review_or_hide"}, ensure_ascii=False)),
    ("竞品咨询",     json.dumps({"must_compare_fairly": True}, ensure_ascii=False)),
    ("竞品对比",     json.dumps({"only_state_not_attack": True}, ensure_ascii=False)),
    ("竞品引流",     json.dumps({"action": "no_reply", "moderation": "review_or_hide"}, ensure_ascii=False)),
    ("攻击引战",     json.dumps({"action": "no_reply", "max_len": 0}, ensure_ascii=False)),
    ("产品差评",     json.dumps({"human_required": True}, ensure_ascii=False)),
]

DEFAULT_REPLY_RATES = {
    "求推荐": 0.90, "参数求解释": 0.80, "故障求助": 0.95, "已购反馈": 1.00,
    "价格质疑": 0.60, "恰饭质疑": 0.00, "专业质疑": 0.00, "恶意质疑": 0.00,
    "品类争论": 0.30, "灌水玩梗": 0.50, "无关灌水": 0.00, "导购广告": 0.00,
    "竞品咨询": 0.80, "竞品对比": 0.60, "竞品引流": 0.00, "攻击引战": 0.10,
    "产品差评": 0.00,
}

# 全局禁用句种子(客套/总结升华/三连排比, 文档 §5.3;账号级留空由各号主笔补)
BANNED_SEEDS = [
    "感谢支持", "感谢关注", "谢谢大家", "喜欢的点个关注",
    "希望对你有帮助", "希望我的回答对你有帮助",
    "祝大家", "祝生活愉快", "祝好运",
    "综上所述", "总的来说", "总而言之", "不难看出",
    "不仅……而且", "一方面……另一方面",
]


def _create_kb_tables(cur):
    for name, ddl in KB_TABLES.items():
        cur.execute(ddl)
        logger.info("知识库表已就绪: %s", name)


def _ensure_up_creator_reply_columns(cur):
    """运行配置归属既有 UP主管理，避免新建平行账号主数据。"""
    columns = {
        "回复覆盖品类": "JSON NULL COMMENT '可自动回复的品类，知识检索硬过滤'",
        "自动回复状态": "ENUM('disabled','pilot','active') NOT NULL DEFAULT 'disabled' COMMENT 'disabled关闭/pilot全审/active按抽检'",
        "回复日上限": "INT NOT NULL DEFAULT 100 COMMENT '每日自动回复上限'",
        "Skill版本": "VARCHAR(16) NULL COMMENT '当前已发布人设参数包版本'",
        "Skill更新时间": "INT NULL COMMENT '当前人设参数包发布时间'",
    }
    for name, definition in columns.items():
        cur.execute("SHOW COLUMNS FROM `UP主管理` LIKE %s", (name,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE `UP主管理` ADD COLUMN `{name}` {definition}")
            logger.info("UP主管理已补充回复字段: %s", name)


def _migrate_kb_subject_keys(cur):
    """将早期空表的字符串账号键迁移为 UP主管理主键。"""
    cur.execute("SHOW COLUMNS FROM `persona_pack` LIKE 'up_id'")
    has_up_id = bool(cur.fetchone())
    cur.execute("SHOW COLUMNS FROM `persona_pack` LIKE 'account_id'")
    has_account_id = bool(cur.fetchone())
    if has_account_id and not has_up_id:
        cur.execute("SELECT COUNT(*) AS n FROM `persona_pack`")
        if cur.fetchone()["n"]:
            raise RuntimeError("persona_pack 含旧 account_id 数据，需先人工映射后再迁移")
        cur.execute("ALTER TABLE `persona_pack` DROP PRIMARY KEY")
        cur.execute("ALTER TABLE `persona_pack` DROP COLUMN `account_id`")
        cur.execute("ALTER TABLE `persona_pack` ADD COLUMN `up_id` BIGINT NOT NULL FIRST")
        cur.execute("ALTER TABLE `persona_pack` ADD PRIMARY KEY (`up_id`)")
        logger.info("persona_pack 已迁移为 UP主管理主键关联")

    # 兼容已有禁用句表：保留旧 account_id 列以免丢失历史配置，新增主键关联列。
    cur.execute("SHOW COLUMNS FROM `banned_phrase` LIKE 'up_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE `banned_phrase` ADD COLUMN `up_id` BIGINT NOT NULL DEFAULT 0 AFTER `account_id`")
        cur.execute("ALTER TABLE `banned_phrase` ADD KEY `idx_bp_up` (`up_id`)")
        logger.info("banned_phrase 已补充 up_id 兼容列")

    cur.execute("SHOW COLUMNS FROM `reply_log` LIKE 'up_id'")
    if not cur.fetchone():
        cur.execute("SELECT COUNT(*) AS n FROM `reply_log`")
        if cur.fetchone()["n"]:
            raise RuntimeError("reply_log 含旧 account_id 数据，需先人工映射后再迁移")
        cur.execute("ALTER TABLE `reply_log` DROP PRIMARY KEY")
        cur.execute("ALTER TABLE `reply_log` DROP INDEX `idx_rl_account`")
        cur.execute("ALTER TABLE `reply_log` DROP COLUMN `account_id`")
        cur.execute("ALTER TABLE `reply_log` ADD COLUMN `up_id` BIGINT NOT NULL AFTER `src_pk`")
        cur.execute("ALTER TABLE `reply_log` ADD COLUMN `platform_account_id` VARCHAR(255) NULL AFTER `up_id`")
        cur.execute("ALTER TABLE `reply_log` ADD PRIMARY KEY (`reply_id`)")
        cur.execute("ALTER TABLE `reply_log` ADD KEY `idx_rl_up` (`up_id`,`created_at`)")
        logger.info("reply_log 已迁移为 UP主管理主键关联")


def _seed_kb(cur):
    now = int(time.time())
    # guardrails: INSERT IGNORE 幂等
    for rule_id, gtype, triggers, action, rewrite_to, note in GUARDRAIL_SEEDS:
        cur.execute(
            "INSERT IGNORE INTO `guardrails` "
            "(`rule_id`,`gtype`,`trigger_pattern`,`action`,`rewrite_to`,`note`,`enabled`) "
            "VALUES (%s,%s,%s,%s,%s,%s,1)",
            (rule_id, gtype,
             json.dumps(triggers, ensure_ascii=False) if triggers else None,
             action, rewrite_to, note))
    # routing_matrix: 每个 type × 5 个层级, extra_rule 优先取种子
    extra_map = {t: r for t, r in ROUTING_SEEDS}
    for ctype in COMMENT_TYPES:
        for lvl in ("L1", "L2", "L3", "L4", "L5"):
            cur.execute(
                "INSERT IGNORE INTO `routing_matrix` "
                "(`comment_type`,`account_level`,`reply_rate`,`target_len`,`human_required`,`extra_rule`) "
                "VALUES (%s,%s,%s,NULL,%s,%s)",
                (ctype, lvl, DEFAULT_REPLY_RATES[ctype],
                 1 if ctype in ("恰饭质疑", "专业质疑", "恶意质疑", "产品差评") else 0,
                 extra_map.get(ctype)))
    # banned_phrase 全局级
    for ph in BANNED_SEEDS:
        cur.execute(
            "INSERT IGNORE INTO `banned_phrase` (`phrase`,`scope`,`up_id`,`note`,`created_ts`) "
            "VALUES (%s,'global',0,NULL,%s)", (ph, now))


def migrate_routing_matrix(cur):
    """用已批准的 14 类初始路由替换 P0 备份后的旧 10 类种子。"""
    cur.execute("SELECT COUNT(*) AS n FROM `routing_matrix`")
    existing = cur.fetchone()["n"]
    cur.execute("DELETE FROM `routing_matrix`")
    extra_map = {t: r for t, r in ROUTING_SEEDS}
    for ctype in COMMENT_TYPES:
        for lvl in ("L1", "L2", "L3", "L4", "L5"):
            cur.execute(
                "INSERT INTO `routing_matrix` "
                "(`comment_type`,`account_level`,`reply_rate`,`target_len`,`human_required`,`extra_rule`) "
                "VALUES (%s,%s,%s,NULL,%s,%s)",
                (ctype, lvl, DEFAULT_REPLY_RATES[ctype],
                 1 if ctype in ("恰饭质疑", "专业质疑", "恶意质疑", "产品差评") else 0,
                 extra_map.get(ctype)))
    logger.info("路由矩阵已从 %s 条旧配置迁移为 %s 条", existing, len(COMMENT_TYPES) * 5)


def migrate_model_fact_price_range(cur):
    """将旧的文字价位段升级为可筛选的上下限区间。"""
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='model_facts'")
    names = {row["COLUMN_NAME"] for row in cur.fetchall()}
    if "price_band" in names and "price_min" not in names:
        cur.execute("ALTER TABLE `model_facts` CHANGE COLUMN `price_band` `price_min` "
                    "MEDIUMINT UNSIGNED NULL COMMENT '价格区间下限（元），不存实时售价'")
        names.remove("price_band")
        names.add("price_min")
    if "price_max" not in names:
        cur.execute("ALTER TABLE `model_facts` ADD COLUMN `price_max` MEDIUMINT UNSIGNED NULL "
                    "COMMENT '价格区间上限（元），不存实时售价' AFTER `price_min`")
    if "price_step" not in names:
        cur.execute("ALTER TABLE `model_facts` ADD COLUMN `price_step` ENUM('300','500') NULL "
                    "COMMENT '价格区间颗粒度（元）' AFTER `price_max`")


def migrate_model_fact_state_pending(cur):
    """state 枚举补充 pending：摄取管道先落待确认，人工核实后在 KB 页翻 usable。"""
    cur.execute("SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='model_facts' AND COLUMN_NAME='state'")
    row = cur.fetchone()
    if row and "pending" not in (row["COLUMN_TYPE"] or ""):
        cur.execute("ALTER TABLE `model_facts` MODIFY COLUMN `state` "
                    "ENUM('pending','usable','reference_only') NOT NULL DEFAULT 'usable' "
                    "COMMENT 'pending=待人工确认(摄取管道写入)/usable=可用/reference_only=仅参考'")
        logger.info("model_facts.state 已补充 pending 枚举值")


def migrate_pain_point_param_state_pending(cur):
    """AI 生成的参数映射先进入 pending，审核后才可用于产品和回复匹配。"""
    cur.execute("SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='category_pain_point_params' AND COLUMN_NAME='state'")
    row = cur.fetchone()
    if row and "pending" not in (row["COLUMN_TYPE"] or ""):
        cur.execute("ALTER TABLE `category_pain_point_params` MODIFY COLUMN `state` "
                    "ENUM('pending','usable','disabled') NOT NULL DEFAULT 'usable'")
        logger.info("category_pain_point_params.state 已补充 pending 枚举值")


def migrate_pain_point_star_rating(cur):
    """为历史痛点表补充 1-5 星级，默认三星。"""
    cur.execute("SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='category_pain_points' "
                "AND COLUMN_NAME='star_rating'")
    if not cur.fetchone()["c"]:
        cur.execute("ALTER TABLE `category_pain_points` ADD COLUMN `star_rating` "
                    "TINYINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '痛点星级，1-5 星' "
                    "AFTER `reply_guidance`")
        logger.info("category_pain_points.star_rating 已补充")


def migrate_product_pain_point_template_fields(cur):
    """产品痛点匹配存放产品专属星级和标准回复，避免编辑商单污染全品类。"""
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='model_pain_point_matches'")
    names = {row["COLUMN_NAME"] for row in cur.fetchall()}
    if "star_rating" not in names:
        cur.execute("ALTER TABLE `model_pain_point_matches` ADD COLUMN `star_rating` "
                    "TINYINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '该产品下痛点星级，1-5 星' "
                    "AFTER `matched_params`")
    if "standard_reply" not in names:
        cur.execute("ALTER TABLE `model_pain_point_matches` ADD COLUMN `standard_reply` TEXT NULL "
                    "COMMENT '该产品的标准回复' AFTER `star_rating`")


def init_kb():
    """启动时幂等建表 + 种子数据"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_up_creator_reply_columns(cur)
                _create_kb_tables(cur)
                _migrate_kb_subject_keys(cur)
                migrate_model_fact_price_range(cur)
                migrate_model_fact_state_pending(cur)
                migrate_pain_point_param_state_pending(cur)
                migrate_pain_point_star_rating(cur)
                migrate_product_pain_point_template_fields(cur)
                _seed_kb(cur)
            conn.commit()
        logger.info("知识库表结构与种子数据已就绪")
    except Exception:
        logger.exception("知识库初始化失败")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_kb()


# ============================================================
# 管理页面与 API(通用元数据驱动)
# ============================================================
from flask import Blueprint, jsonify, render_template, request

kb_bp = Blueprint("kb", __name__, url_prefix="/scrm")

# 表层级元数据; 列定义运行时从 information_schema 读取
KB_META = {
    "persona_pack":     {"title": "人设参数包", "layer": "L0 策略"},
    "routing_matrix":   {"title": "路由矩阵", "layer": "L0 策略"},
    "guardrails":       {"title": "硬红线", "layer": "L0 策略"},
    "model_facts":      {"title": "型号事实表", "layer": "L1 事实"},
    "category_pain_points": {"title": "产品类目痛点表", "layer": "L1.5 类目能力"},
    "category_pain_point_params": {"title": "痛点参数证据表", "layer": "L1.5 类目能力"},
    "model_pain_point_matches": {"title": "产品痛点匹配结果", "layer": "L1.5 类目能力"},
    "glossary":         {"title": "术语库", "layer": "L2 知识"},
    "misconception":    {"title": "认知纠正库", "layer": "L2 知识"},
    "banned_phrase":    {"title": "禁用句", "layer": "L3 语料"},
    "reply_log":        {"title": "回复日志", "layer": "L4 运营", "readonly": True},
}

# 统一将数据库英文字段以“中文（英文）”交给管理页面；未知字段仍保留中文兜底。
FIELD_LABELS = {
    "up_id": "UP主编号", "params": "人设参数", "fingerprint": "账号指纹", "few_shot_slots": "真人示例条数", "version": "版本", "updated_ts": "更新时间戳",
    "id": "编号", "comment_type": "评论类型", "account_level": "账号等级", "reply_rate": "回复比例", "target_len": "目标字数", "human_required": "强制人工", "extra_rule": "补充规则",
    "rule_id": "规则编号", "gtype": "规则类型", "trigger_pattern": "触发词/模式", "action": "处理动作", "rewrite_to": "改写指引", "note": "备注", "enabled": "是否启用",
    "model_id": "型号编号", "brand": "品牌", "model_name": "官方型号", "aliases": "别名/简称", "category": "产品品类", "launch_date": "上市日期", "price_min": "价格下限（元）", "price_max": "价格上限（元）", "price_step": "价格颗粒度（元）", "specs": "核心参数", "known_issues": "已知问题", "tested_by": "实测信息", "source": "来源证据", "confidence": "可信等级", "verified_at": "核验日期", "expires_at": "失效日期", "state": "使用状态",
    "pain_point_id": "痛点编号", "pain_point": "用户痛点", "description": "痛点边界/场景", "trigger_words": "用户触发词", "reply_guidance": "回复指引", "star_rating": "痛点星级（1-5）", "priority": "优先级", "created_ts": "创建时间戳", "updated_ts": "更新时间戳",
    "mapping_id": "映射编号", "param_key": "参数键", "param_name": "参数名称", "param_aliases": "参数别名", "evidence_rule": "参数佐证规则", "interpretation": "用户感受翻译", "sort_order": "排序",
    "match_id": "匹配编号", "matched_params": "命中参数及参数值", "star_rating": "痛点星级（1-5）", "standard_reply": "产品标准回复", "match_source": "匹配来源", "note": "人工说明",
    "term": "术语", "alias": "别名", "unit": "单位", "define": "定义", "read": "指标解读", "trap": "常见误读", "level": "可见等级", "standard_version": "标准版本", "account_visibility": "账号可见范围", "updated_at": "更新日期",
    "topic_id": "主题编号", "trigger_words": "触发词", "trigger_synonyms": "触发词同义扩展", "priority": "优先级", "truth": "正确结论", "condition": "适用条件", "exception": "例外情况", "tpl_by_level": "分等级回复模板", "l_level_ok": "可用账号等级", "needs_human": "是否人工处理",
    "phrase_id": "禁用句编号", "phrase": "禁用表达", "scope": "适用范围", "created_ts": "创建时间戳",
    "reply_id": "回复编号", "comment_id": "平台评论编号", "src_table": "来源评论表", "src_pk": "来源记录编号", "platform_account_id": "平台运营账号编号", "type_conf": "分类置信度", "retrieved": "命中知识条目", "prompt_version": "Skill版本", "model": "生成模型", "draft": "生成草稿", "detector": "质量闸结果", "final_text": "最终回复", "rejection_code": "拒绝码", "editor": "编辑人", "latency_ms": "耗时（毫秒）", "created_at": "创建时间戳",
}


def _check_table(table):
    if table not in KB_META:
        return None
    return table


def _columns(cur, table):
    """information_schema 取列定义: 名称/类型/可空/默认值/主键/自增/枚举选项"""
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
        "       COLUMN_KEY, EXTRA, COLUMN_COMMENT "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
        (table,))
    cols = []
    for r in cur.fetchall():
        enum_opts = None
        if r["DATA_TYPE"] == "enum":
            enum_opts = [v.strip("'") for v in
                         r["COLUMN_TYPE"][5:-1].split("','")]
        cols.append({
            "name": r["COLUMN_NAME"], "type": r["DATA_TYPE"],
            "label": FIELD_LABELS.get(r["COLUMN_NAME"], "字段"),
            "nullable": r["IS_NULLABLE"] == "YES",
            "default": r["COLUMN_DEFAULT"], "pk": r["COLUMN_KEY"] == "PRI",
            "auto": "auto_increment" in (r["EXTRA"] or ""),
            "enum": enum_opts, "comment": r["COLUMN_COMMENT"] or "",
        })
    return cols


def _pk_of(cols):
    return next((c["name"] for c in cols if c["pk"]), None)


def _parse_value(col, raw):
    """表单字符串 → Python 值; JSON 列校验合法性(客户端已发 JSON 类型时直接序列化)"""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if col["type"] == "json":
        if isinstance(raw, (dict, list)):
            return json.dumps(raw, ensure_ascii=False)
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False)
        except ValueError:
            raise ValueError(f"字段 {col['name']} 不是合法 JSON")
    if col["type"] in ("int", "bigint", "tinyint", "smallint"):
        return int(raw)
    if col["type"] in ("decimal", "float", "double"):
        return float(raw)
    return raw


def _validate_model_price_range(body):
    """价格范围只存区间，不接受实时价；边界需按 300/500 元档位对齐。"""
    lower, upper, step = body.get("price_min"), body.get("price_max"), body.get("price_step")
    if lower in (None, "") and upper in (None, "") and step in (None, ""):
        return None
    if lower in (None, "") or upper in (None, "") or str(step) not in {"300", "500"}:
        return "价格区间需同时填写下限、上限和 300/500 元颗粒度"
    try:
        lower, upper, step = int(lower), int(upper), int(step)
    except (TypeError, ValueError):
        return "价格区间必须为整数金额"
    if lower < 0 or upper <= lower:
        return "价格上限必须大于下限"
    if lower % step or upper % step:
        return f"价格上下限须按 {step} 元颗粒度对齐"
    return None


def _validate_pain_point_star_rating(body):
    """痛点星级仅允许 1-5，避免通用知识库编辑绕过页面下拉限制。"""
    if "star_rating" not in body or body.get("star_rating") in (None, ""):
        return None
    try:
        stars = int(body["star_rating"])
    except (TypeError, ValueError):
        return "痛点星级必须为 1-5 的整数"
    if not 1 <= stars <= 5:
        return "痛点星级必须为 1-5"
    body["star_rating"] = stars
    return None


def _row_for_display(row, cols):
    """JSON 列解析为对象方便前端展示, date 转 ISO, 过长截断"""
    out = {}
    for c in cols:
        v = row.get(c["name"])
        if c["type"] == "json" and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        if isinstance(v, str) and len(v) > 120:
            v = v[:120] + "…"
        out[c["name"]] = v
    return out


@kb_bp.get("/kb")
def page_kb():
    return render_template("scrm/kb.html", active="kb")


# ============================================================
# 文档同步入库: 读源 → DeepSeek 结构化提取 → 预览确认 → 写入
# 支持表: model_facts / category_pain_points / category_pain_point_params / glossary / misconception / banned_phrase / guardrails
# ============================================================
import hashlib

import requests

import param_ingest

INGEST_TABLES = {
    "model_facts":   {"title": "型号事实表", "hint": (
        "每行 = 一个产品型号的事实条目。字段: brand(品牌,必填), model_name(完整型号,必填), "
        "aliases(俗称数组), category(空调/冰箱/洗护/清洁/净水/空净 之一), launch_date(YYYY-MM-DD), "
        "price_min/price_max(整数元), price_step(300或500), "
        "specs(对象: 键→{value,unit,condition}, 只提取原文写明的参数, 严禁推算), known_issues(数组)")},
    "category_pain_points": {"title": "产品类目痛点表", "hint": (
        "每行 = 一个不绑定品牌或型号的类目痛点。字段: category(产品类目,必填), pain_point(用户感知的顾虑,必填), "
        "trigger_words(用户可能使用的触发词/同义说法数组,必填), description(适用场景与边界), "
        "reply_guidance(回复时应解释或追问的方向), priority(0-100)。pain_point_id 可留空，系统会按类目和痛点生成稳定编号")},
    "category_pain_point_params": {"title": "痛点参数证据表", "hint": (
        "每行 = 已有痛点的一条参数佐证。字段: pain_point_id(必填，必须是产品类目痛点表中已有的编号), "
        "param_key(与型号事实表 specs 的参数键一致,必填), param_name(参数中文名,必填), "
        "param_aliases(用户对该参数的别名数组), evidence_rule(该参数如何佐证/反证痛点及必要条件,必填), "
        "interpretation(如何翻译成用户感受), sort_order。不要编造参数阈值或结论")},
    "glossary":      {"title": "术语库", "hint": (
        "每行 = 一个行业/产品术语。字段: term(术语,必填), alias(别名数组), unit(单位), "
        "define(定义,必填), read(怎么读这个数/怎么看这个指标), trap(常见误读), standard_version(所属标准版本)")},
    "misconception": {"title": "认知纠正库", "hint": (
        "每行 = 一个常见误解的纠正。字段: trigger_words(触发词数组,必填), trigger_synonyms(同义扩展数组), "
        "priority(0-100, 默认50), truth(正确结论,必填), condition(适用条件), exception(例外情况), needs_human(布尔)")},
    "banned_phrase": {"title": "禁用句", "hint": (
        "每行 = 一句禁止账号使用的表达。字段: phrase(禁用表达,必填), note(备注)。scope 固定 global、up_id 固定 0, 无需输出")},
    "guardrails":    {"title": "硬红线", "hint": (
        "每行 = 一条拦截规则。字段: gtype(规则类型: 价格/安全/健康/竞品人格/施工/承诺/未测型号), "
        "trigger_pattern(触发词数组,必填), action(block=进人工/rewrite=改写/redirect=标准引导,必填), "
        "rewrite_to(action=rewrite/redirect 时的标准话术), note(备注)")},
}

INGEST_PROMPT = """你是知识库资料结构化助手。从下面的资料原文中提取可以写入知识库表 `{table}`（{title}）的记录, 只输出 JSON 对象。

目标表说明: {hint}

要求:
- 只提取原文明确支持的内容, 严禁编造、推算或脑补
- rows 是对象数组, 键名必须使用上面列出的字段名; 原文没有的字段直接省略
- 数组一律用 JSON 数组, 不要用顿号分隔的字符串
- 原文确实没有可提取内容时输出 {{"rows": []}}

资料原文:
---
{content}
---"""


def _kb_ingest_llm_rows(target, content):
    """DeepSeek 通用提取: 返回 rows 对象数组。"""
    if len(content) > 60000:
        content = content[:60000] + "\n…(超长截断)"
    meta = INGEST_TABLES[target]
    prompt = INGEST_PROMPT.format(table=target, title=meta["title"],
                                  hint=meta["hint"], content=content)
    resp = param_ingest.SESSION.post(
        f"{param_ingest.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {param_ingest.DEEPSEEK_API_KEY}"},
        json={"model": param_ingest.DEEPSEEK_MODEL,
              "messages": [{"role": "system", "content": "只输出合法 JSON, 不要输出任何解释或代码块标记。"},
                           {"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"}, "temperature": 0.1, "max_tokens": 4000},
        timeout=180)
    resp.raise_for_status()
    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except ValueError as exc:
        raise ValueError("LLM 返回的不是合法 JSON") from exc
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("LLM 返回缺少 rows 数组")
    return [r for r in rows if isinstance(r, dict)]


def _json_list(v, limit=30):
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            v = parsed if isinstance(parsed, list) else v
        except ValueError:
            pass
        if isinstance(v, str):
            v = re.split(r"[,，;；\n]+", v)
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:128] for x in v if str(x).strip()][:limit]


def _json_object(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            value = json.loads(v)
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
    return {}


def _model_pain_point_categories(model):
    """将一级清洁类目细分为可匹配的产品类目，避免清洁/扫地机器人命名差异造成漏配。"""
    categories = {str(model.get("category") or "").strip()}
    name = str(model.get("model_name") or "")
    if "清洁" in categories and re.search(r"扫地|扫拖|机器人", name, re.I):
        categories.add("扫地机器人")
    return [category for category in categories if category]


def _sync_model_pain_point_matches(cur, model_id):
    """按产品已录入的 specs 自动落库痛点匹配；人工维护过的记录永不被自动覆盖。"""
    cur.execute("SELECT model_id,model_name,category,specs FROM model_facts WHERE model_id=%s LIMIT 1", (model_id,))
    model = cur.fetchone()
    if not model:
        return 0
    specs = _json_object(model.get("specs"))
    normal_specs = {str(key).strip().lower(): (key, value) for key, value in specs.items()}
    categories = _model_pain_point_categories(model)
    placeholders = ", ".join(["%s"] * len(categories))
    cur.execute(f"""SELECT P.pain_point_id,M.mapping_id,M.param_key,M.param_name,M.param_aliases
                   FROM category_pain_points P
                   JOIN category_pain_point_params M ON M.pain_point_id=P.pain_point_id
                   WHERE P.category IN ({placeholders}) AND P.state='usable' AND M.state='usable'
                   ORDER BY P.pain_point_id,M.sort_order,M.mapping_id""", categories)
    grouped = {}
    for mapping in cur.fetchall():
        aliases = [mapping["param_key"]] + _json_list(mapping.get("param_aliases"))
        candidates = {str(item).strip().lower() for item in aliases if str(item).strip()}
        hits = [(raw_key, spec_value) for norm_key, (raw_key, spec_value) in normal_specs.items()
                if norm_key in candidates]
        if hits:
            grouped.setdefault(mapping["pain_point_id"], []).append({
                "mapping_id": mapping["mapping_id"], "param_key": mapping["param_key"],
                "param_name": mapping["param_name"],
                "model_specs": [{"key": raw_key, "value": spec_value} for raw_key, spec_value in hits],
            })
    cur.execute("SELECT pain_point_id,match_source FROM model_pain_point_matches WHERE model_id=%s", (model_id,))
    existing = {row["pain_point_id"]: row["match_source"] for row in cur.fetchall()}
    now = int(time.time())
    for pain_point_id, matched_params in grouped.items():
        if existing.get(pain_point_id) == "manual":
            continue
        payload = json.dumps(matched_params, ensure_ascii=False)
        cur.execute("""INSERT INTO model_pain_point_matches
                       (model_id,pain_point_id,matched_params,match_source,state,created_ts,updated_ts)
                       VALUES (%s,%s,%s,'auto','usable',%s,%s)
                       ON DUPLICATE KEY UPDATE matched_params=VALUES(matched_params),match_source='auto',
                           state='usable',updated_ts=VALUES(updated_ts)""",
                    (model_id, pain_point_id, payload, now, now))
    stale_auto = [pain_id for pain_id, source in existing.items()
                  if source == "auto" and pain_id not in grouped]
    if stale_auto:
        placeholders = ", ".join(["%s"] * len(stale_auto))
        cur.execute(f"DELETE FROM model_pain_point_matches WHERE model_id=%s AND match_source='auto' "
                    f"AND pain_point_id IN ({placeholders})", [model_id, *stale_auto])
    return len(grouped)


def sync_model_pain_point_matches(model_id):
    """对外同步入口，供商单参数同步与知识库确认流程调用。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            count = _sync_model_pain_point_matches(cur, model_id)
        conn.commit()
    return count


def _norm_glossary(r, today):
    term = str(r.get("term") or "").strip()[:64]
    define = str(r.get("define") or "").strip()
    if not term or not define:
        raise ValueError("术语库行缺少 term 或 define")
    row = {"term": term, "define": define, "updated_at": today}
    alias = _json_list(r.get("alias"))
    if alias:
        row["alias"] = json.dumps(alias, ensure_ascii=False)
    for key, limit in (("unit", 16), ("standard_version", 32)):
        v = str(r.get(key) or "").strip()
        if v:
            row[key] = v[:limit]
    for key in ("read", "trap"):
        v = str(r.get(key) or "").strip()
        if v:
            row[key] = v
    return row


def _norm_misconception(r, today):
    trig = _json_list(r.get("trigger_words"))
    truth = str(r.get("truth") or "").strip()
    if not trig or not truth:
        raise ValueError("认知纠正库行缺少 trigger_words 或 truth")
    topic_id = str(r.get("topic_id") or "").strip()[:32]
    if not topic_id:
        topic_id = "MIS-" + hashlib.md5(("|".join(trig) + truth).encode("utf-8")).hexdigest()[:8].upper()
    row = {"topic_id": topic_id, "trigger_words": json.dumps(trig, ensure_ascii=False),
           "truth": truth, "verified_at": today}
    syn = _json_list(r.get("trigger_synonyms"))
    if syn:
        row["trigger_synonyms"] = json.dumps(syn, ensure_ascii=False)
    try:
        row["priority"] = max(0, min(100, int(r.get("priority") or 50)))
    except (TypeError, ValueError):
        row["priority"] = 50
    for key in ("condition", "exception"):
        v = str(r.get(key) or "").strip()
        if v:
            row[key] = v
    tpl = r.get("tpl_by_level")
    row["tpl_by_level"] = json.dumps(tpl, ensure_ascii=False) if isinstance(tpl, dict) and tpl \
        else json.dumps({"待补充": ""}, ensure_ascii=False)
    row["needs_human"] = 1 if r.get("needs_human") in (1, "1", True, "true", "是") else 0
    return row


def _norm_banned_phrase(r, now_ts):
    phrase = str(r.get("phrase") or "").strip()[:128]
    if not phrase:
        raise ValueError("禁用句行缺少 phrase")
    row = {"phrase": phrase, "scope": "global", "up_id": 0, "created_ts": int(now_ts)}
    note = str(r.get("note") or "").strip()
    if note:
        row["note"] = note[:255]
    return row


def _norm_guardrails(r):
    gtype = str(r.get("gtype") or "").strip()[:16]
    trig = _json_list(r.get("trigger_pattern"))
    action = str(r.get("action") or "").strip()
    if not gtype or not trig or action not in ("block", "rewrite", "redirect"):
        raise ValueError("硬红线行需包含 gtype、trigger_pattern 和 action(block/rewrite/redirect)")
    rule_id = str(r.get("rule_id") or "").strip()[:32]
    if not rule_id:
        rule_id = "G-" + hashlib.md5(("|".join(trig) + gtype).encode("utf-8")).hexdigest()[:6].upper()
    row = {"rule_id": rule_id, "gtype": gtype,
           "trigger_pattern": json.dumps(trig, ensure_ascii=False), "action": action, "enabled": 1}
    for key, limit in (("rewrite_to", None), ("note", 255)):
        v = str(r.get(key) or "").strip()
        if v:
            row[key] = v[:limit] if limit else v
    return row


def _norm_category_pain_point(r, now_ts):
    category = str(r.get("category") or "").strip()[:32]
    pain_point = str(r.get("pain_point") or "").strip()[:128]
    trigger_words = _json_list(r.get("trigger_words"))
    if not category or not pain_point or not trigger_words:
        raise ValueError("产品类目痛点行需包含 category、pain_point 和 trigger_words")
    pain_point_id = str(r.get("pain_point_id") or "").strip()[:64]
    if not pain_point_id:
        digest = hashlib.md5(f"{category}|{pain_point}".encode("utf-8")).hexdigest()[:12].upper()
        pain_point_id = f"CPP-{digest}"
    try:
        priority = max(0, min(100, int(r.get("priority") or 50)))
    except (TypeError, ValueError):
        priority = 50
    row = {"pain_point_id": pain_point_id, "category": category, "pain_point": pain_point,
           "trigger_words": json.dumps(trigger_words, ensure_ascii=False), "priority": priority,
           "state": "usable", "created_ts": int(now_ts), "updated_ts": int(now_ts)}
    for key in ("description", "reply_guidance"):
        value = str(r.get(key) or "").strip()
        if value:
            row[key] = value
    return row


def _norm_category_pain_point_param(r):
    pain_point_id = str(r.get("pain_point_id") or "").strip()[:64]
    param_key = str(r.get("param_key") or "").strip()[:64]
    param_name = str(r.get("param_name") or "").strip()[:64]
    evidence_rule = str(r.get("evidence_rule") or "").strip()
    if not pain_point_id or not param_key or not param_name or not evidence_rule:
        raise ValueError("痛点参数证据行需包含 pain_point_id、param_key、param_name 和 evidence_rule")
    try:
        sort_order = max(0, min(32767, int(r.get("sort_order") or 10)))
    except (TypeError, ValueError):
        sort_order = 10
    row = {"pain_point_id": pain_point_id, "param_key": param_key, "param_name": param_name,
           "evidence_rule": evidence_rule, "sort_order": sort_order, "state": "usable"}
    aliases = _json_list(r.get("param_aliases"))
    if aliases:
        row["param_aliases"] = json.dumps(aliases, ensure_ascii=False)
    interpretation = str(r.get("interpretation") or "").strip()
    if interpretation:
        row["interpretation"] = interpretation
    return row


_INGEST_NORMALIZERS = {
    "category_pain_points": lambda r, ctx: _norm_category_pain_point(r, ctx["now_ts"]),
    "category_pain_point_params": lambda r, ctx: _norm_category_pain_point_param(r),
    "glossary": lambda r, ctx: _norm_glossary(r, ctx["today"]),
    "misconception": lambda r, ctx: _norm_misconception(r, ctx["today"]),
    "banned_phrase": lambda r, ctx: _norm_banned_phrase(r, ctx["now_ts"]),
    "guardrails": lambda r, ctx: _norm_guardrails(r),
}


@kb_bp.post("/api/kb/pain-point-mappings/suggest")
def api_pain_point_mapping_suggest():
    """按类目用已入库痛点与参数键生成候选映射；所有结果均为 pending，必须人工确认。"""
    body = request.get_json(silent=True) or {}
    category = str(body.get("category") or "").strip()[:32]
    if not category:
        return jsonify({"error": "请选择或输入产品类目"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pain_point_id,pain_point,description,trigger_words FROM category_pain_points "
                        "WHERE category=%s AND state='usable' ORDER BY priority DESC LIMIT 100", (category,))
            pains = cur.fetchall()
            cur.execute("SELECT specs FROM model_facts WHERE category=%s AND state='usable' LIMIT 200", (category,))
            spec_keys = set()
            for model in cur.fetchall():
                spec_keys.update(_json_object(model.get("specs")).keys())
    if not pains:
        return jsonify({"error": f"类目“{category}”没有可用痛点，请先维护产品类目痛点表"}), 400
    if not spec_keys:
        return jsonify({"error": f"类目“{category}”没有已确认产品参数，暂时无法生成映射"}), 400
    prompt = """你是产品知识库审核助手。根据给定类目痛点和已存在的产品参数键，生成参数证据映射候选。
只能选择提供的 pain_point_id 和 param_key；每条映射必须说明参数如何佐证或限制该痛点，不得编造阈值、实测结果或性能承诺。
一个痛点可给出 0-N 条参数。输出合法 JSON：{\"rows\":[{\"pain_point_id\":...,\"param_key\":...,\"param_name\":...,\"param_aliases\":[...],\"evidence_rule\":...,\"interpretation\":...,\"sort_order\":10}]}。
没有可靠关联时不要输出。
类目：""" + category + "\n痛点：" + json.dumps(pains, ensure_ascii=False) + \
        "\n可用参数键：" + json.dumps(sorted(spec_keys), ensure_ascii=False)
    try:
        response = param_ingest.SESSION.post(
            f"{param_ingest.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {param_ingest.DEEPSEEK_API_KEY}"},
            json={"model": param_ingest.DEEPSEEK_MODEL,
                  "messages": [{"role": "system", "content": "只输出合法 JSON，不输出说明或代码块。"},
                               {"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}, "temperature": 0.1, "max_tokens": 6000},
            timeout=180)
        response.raise_for_status()
        rows = json.loads(response.json()["choices"][0]["message"]["content"]).get("rows", [])
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": f"模型候选结果格式无效: {exc}"}), 502
    except requests.RequestException as exc:
        return jsonify({"error": f"模型请求失败: {exc}"}), 502
    pain_ids = {item["pain_point_id"] for item in pains}
    normalized = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("pain_point_id") not in pain_ids or row.get("param_key") not in spec_keys:
            continue
        try:
            candidate = _norm_category_pain_point_param(row)
            candidate["state"] = "pending"
            normalized.append(candidate)
        except ValueError:
            continue
    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for row in normalized:
                cols = ", ".join(f"`{key}`" for key in row)
                values = ", ".join(["%s"] * len(row))
                cur.execute(f"INSERT IGNORE INTO category_pain_point_params ({cols}) VALUES ({values})", list(row.values()))
                inserted += cur.rowcount
        conn.commit()
    return jsonify({"ok": True, "category": category, "generated": len(normalized), "inserted": inserted,
                    "skipped_existing": len(normalized) - inserted,
                    "note": "候选已保存为 pending，请逐条确认后才会参与自动匹配。"})


def _kb_ingest_fetch(body):
    """按来源类型取正文。返回 (ref, title, content)。"""
    src_type = str(body.get("source_type") or "").strip()
    source = str(body.get("source") or "").strip()
    if src_type == "url":
        if not source:
            raise ValueError("请输入网页链接")
        from urllib.parse import urlsplit
        if (urlsplit(source).path or "/").rstrip("/") == "":
            raise ValueError("这是官网首页, 首页没有产品参数。请点「🔎 搜索官网参数页」选具体产品页, "
                             "或打开官网找到该产品详情页后粘贴其链接, 或用 OCR 识别产品页截图")
        ref, title, content = param_ingest.fetch_url(source)
        if len(content.strip()) < 200:
            # 石头商城是 SPA；直接读取其公开商品接口和详情图，比启动浏览器更稳定。
            mall_data = param_ingest.fetch_roborock_mall_product(source)
            if mall_data:
                ref, title, content = mall_data
                if len(content.strip()) >= 200:
                    return ref, title, content
            # 空壳页 → 无头 Chrome 渲染 + 捕获页面接口 JSON, 免截图自动化
            try:
                ref, title, content = param_ingest.fetch_url_rendered(source)
            except Exception as exc:
                # 动态渲染依赖本机浏览器进程。在受限环境中其启动失败不应冒泡成 HTTP 500。
                logger.warning("动态网页渲染失败，链接=%s，原因=%s", source, exc)
                raise ValueError(
                    "该商品页需要动态加载，但当前服务无法启动网页渲染。"
                    "请上传产品参数页截图进行 OCR，或将参数/说明书内容粘贴到“资料原文”后再提取。"
                ) from exc
            title = f"{title or ''}（动态渲染）".strip()
            if len(content.strip()) < 200:
                raise ValueError("该页面静态与动态渲染后都没有可提取的正文, "
                                 "可能商品已下架或链接无效, 请人工确认链接; 或改用 OCR/粘贴文本")
        return ref, title, content
    if src_type == "feishu":
        if not source:
            raise ValueError("请输入飞书文档链接")
        return param_ingest.fetch_feishu(source)
    if src_type == "text":
        return "", "", source
    raise ValueError("来源类型无效")


@kb_bp.get("/kb/ingest")
def page_kb_ingest():
    return render_template("scrm/kb_ingest.html", active="kb")


# ============================================================
# 官网搜索: 品牌+型号 → 搜索引擎 → 官网候选页 → 用户点选后走正常提取
# ============================================================
# 常见家电品牌 → 官方域名(小写, 匹配 host 或其父域); 覆盖不到的走通用排序
OFFICIAL_SITES = {
    ("石头", "roborock"): ["roborock.com"],
    ("海尔", "haier"): ["haier.com", "haier.com.cn"],
    ("卡萨帝", "casarte"): ["casarte.com"],
    ("统帅", "leader"): ["leader.com.cn"],
    ("美的", "midea"): ["midea.com", "midea.cn"],
    ("小天鹅", "littleswan"): ["littleswan.com"],
    ("colmo",): ["colmo.com.cn"],
    ("格力", "gree"): ["gree.com"],
    ("海信", "hisense"): ["hisense.com"],
    ("容声", "ronshen"): ["ronshen.com"],
    ("追觅", "dreame"): ["dreame.cn", "dreame.com"],
    ("科沃斯", "ecovacs"): ["ecovacs.com", "ecovacs.cn"],
    ("添可", "tineco"): ["tineco.cn", "tineco.com"],
    ("云米", "viomi"): ["viomi.com"],
    ("小米", "米家", "xiaomi", "mijia"): ["mi.com"],
    ("松下", "panasonic"): ["panasonic.cn"],
    ("苏泊尔", "supor"): ["supor.com"],
    ("九阳", "joyoung"): ["joyoung.com"],
    ("戴森", "dyson"): ["dyson.cn", "dyson.com"],
    ("飞利浦", "philips"): ["philips.com.cn"],
    ("老板", "robam"): ["robam.com"],
    ("方太", "fotile"): ["fotile.com"],
    ("华帝", "vatti"): ["vatti.com.cn"],
    ("万和", "vanward"): ["vanward.com"],
    ("万家乐", "macro"): ["macro.com.cn"],
    ("必胜", "bissell"): ["bissell.cn"],
    ("史密斯", "a.o."): ["ao-smith.com.cn"],
}

_SEARCH_JUNK = re.compile(r"baike\.baidu|aiqicha|isite\.baidu|wenku\.baidu|zhidao\.baidu|tieba\.baidu|"
                          r"so\.baidu|bilibili\.com|douyin\.com|xiaohongshu|weibo\.com|zhihu\.com|bbs\.|"
                          r"vmall\.com|zol\.com\.cn|autohome\.com\.cn|pconline|mobile\.163|itechbang|"
                          r"mydrivers|ithome|cnmo\.com", re.I)


def _bing_search(query, limit=15):
    """Bing 中文结果解析: 返回 [{url,title,abstract}], 失败抛 requests 异常。"""
    import html as html_lib
    import urllib.parse
    resp = param_ingest.SESSION.get(
        f"https://cn.bing.com/search?q={urllib.parse.quote(query)}&count={limit}&setlang=zh-cn",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "Chrome/120 Safari/537.36", "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=20)
    resp.raise_for_status()
    out = []
    for m in re.finditer(r'<li class="b_algo".*?<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>(.*?)</li>',
                         resp.text, re.S):
        url_, title, rest = m.group(1), m.group(2), m.group(3)
        abstract_m = re.search(r"<p[^>]*>(.*?)</p>", rest, re.S)
        out.append({"url": url_,
                    "title": html_lib.unescape(re.sub("<[^>]+>", "", title)).strip(),
                    "abstract": html_lib.unescape(re.sub("<[^>]+>", "", abstract_m.group(1))).strip()[:200]
                    if abstract_m else ""})
    return out


@kb_bp.post("/api/kb/ingest/search")
def api_kb_ingest_search():
    """品牌+型号 → 搜索官网参数页候选, 官方域名优先。"""
    body = request.get_json(silent=True) or {}
    brand = str(body.get("brand") or "").strip()
    model = str(body.get("model_name") or "").strip()
    category = str(body.get("category") or "").strip()
    if not brand or not model:
        return jsonify({"error": "请先填写品牌和产品型号"}), 400
    brand_l = brand.lower()
    domains = [d for keys, ds in OFFICIAL_SITES.items()
               if any(key in brand_l for key in keys) for d in ds]
    # 品类参与查询可显著消歧(如"石头 P30 Pro"不搜成华为手机)
    topic = f"{model} {category}".strip()
    queries = [f"{brand} {topic} 参数"]
    if domains:
        queries.insert(0, f"site:{domains[0]} {topic}")
    seen, candidates = set(), []
    from urllib.parse import urlsplit
    for query in queries:
        try:
            results = _bing_search(query)
        except requests.RequestException as exc:
            return jsonify({"error": f"搜索请求失败: {exc}"}), 400
        for item in results:
            url_ = item["url"]
            if not url_.startswith("http") or url_ in seen or _SEARCH_JUNK.search(url_):
                continue
            seen.add(url_)
            host = (urlsplit(url_).hostname or "").lower()
            path = urlsplit(url_).path
            official = bool(domains) and any(host == d or host.endswith("." + d) for d in domains)
            score = (5 if official else 0)
            # 首页/近根路径的页面几乎不会有参数表, 降权让位给具体产品页
            if path in ("", "/") and not re.search(r"(goods|product|item|p)/?\d", url_, re.I):
                score -= 4
            hay = (item["title"] + " " + item["abstract"]).lower()
            if model.lower() in hay:
                score += 2
            if category and category.lower() in hay:
                score += 2
            if brand_l in host:
                score += 1
            candidates.append({**item, "score": score, "official": official})
        if len(candidates) >= 12:
            break
    candidates.sort(key=lambda c: -c["score"])
    return jsonify({"candidates": candidates[:10], "domains": domains,
                    "note": "" if domains else f"品牌 {brand} 不在官网域名库中, 已按相关性排序"})


@kb_bp.post("/api/kb/ingest/ocr")
def api_kb_ingest_ocr():
    """图片截图 OCR → 文本, 供「粘贴文本」流程使用。"""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "请选择图片文件"}), 400
    data = file.read()
    if len(data) > 15 * 1024 * 1024:
        return jsonify({"error": "图片不能超过 15MB"}), 400
    try:
        text = param_ingest.ocr_image_bytes(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ImportError:
        return jsonify({"error": "服务器缺少 OCR 组件, 请在运行环境安装: pip install rapidocr-onnxruntime"}), 500
    return jsonify({"text": text, "chars": len(text)})


@kb_bp.post("/api/kb/ingest/preview")
def api_kb_ingest_preview():
    """读源 + LLM 提取, 只预览不落库。"""
    body = request.get_json(silent=True) or {}
    target = str(body.get("target_table") or "").strip()
    if target not in INGEST_TABLES:
        return jsonify({"error": "目标表无效"}), 400
    hints = body.get("hints") or {}
    try:
        ref, title, content = _kb_ingest_fetch(body)
        if not (content or "").strip():
            raise ValueError("源内容为空, 无法提取")
        today = datetime.now().date().isoformat()
        ctx = {"today": today, "now_ts": time.time()}
        if target == "model_facts":
            fields = param_ingest.normalize(param_ingest.llm_extract(content, hints), hints)
            fields["model_id"] = param_ingest.derive_model_id(
                fields["brand"], fields["model_name"], str(hints.get("model_id") or "").strip())
            fields["verified_at"] = today
            rows = [fields]
        else:
            raw_rows = _kb_ingest_llm_rows(target, content)
            if not raw_rows:
                return jsonify({"rows": [], "count": 0, "source_info": {
                    "source_type": body.get("source_type"), "url": ref, "title": title,
                    "chars": len(content), "extractor": param_ingest.DEEPSEEK_MODEL},
                    "warning": "原文中没有提取到可入库的内容"})
            rows = [_INGEST_NORMALIZERS[target](r, ctx) for r in raw_rows]
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": f"网络请求失败: {exc}"}), 400
    return jsonify({"rows": rows, "count": len(rows), "source_info": {
        "source_type": body.get("source_type"), "url": ref, "title": title,
        "chars": len(content), "extractor": param_ingest.DEEPSEEK_MODEL}})


@kb_bp.post("/api/kb/ingest/apply")
def api_kb_ingest_apply():
    """把预览确认过的行写入目标表; model_facts 一律落 state='pending' 等人工确认。"""
    body = request.get_json(silent=True) or {}
    target = str(body.get("target_table") or "").strip()
    if target not in INGEST_TABLES:
        return jsonify({"error": "目标表无效"}), 400
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"error": "没有要入库的行"}), 400
    if len(raw_rows) > 100:
        return jsonify({"error": "单次最多入库 100 行"}), 400
    source_info = body.get("source_info") or {}
    source_info.setdefault("type", "知识库文档摄取")
    today = datetime.now().date().isoformat()
    ctx = {"today": today, "now_ts": time.time()}
    try:
        if target == "model_facts":
            for fields in raw_rows:
                if not isinstance(fields, dict) or not fields.get("brand") \
                        or not fields.get("model_name") or not fields.get("specs"):
                    raise ValueError("型号事实行必须包含 brand、model_name 和 specs")
                fields.setdefault("model_id", param_ingest.derive_model_id(
                    fields["brand"], fields["model_name"], ""))
                fields.setdefault("verified_at", today)
                fields.setdefault("aliases", [])
                fields.setdefault("known_issues", [])
                param_ingest.upsert_pending(fields, dict(source_info), fields["model_id"])
            return jsonify({"ok": True, "total": len(raw_rows),
                            "note": "已写入型号事实表(state=pending), 请在型号事实表页确认翻 usable"})
        normalized = [_INGEST_NORMALIZERS[target](r, ctx) for r in raw_rows]
        pk = {"category_pain_points": "pain_point_id", "category_pain_point_params": None,
              "glossary": "term", "misconception": "topic_id",
              "banned_phrase": "phrase", "guardrails": "rule_id"}[target]
        inserted = updated = 0
        with get_conn() as conn:
            with conn.cursor() as cur:
                if target == "category_pain_point_params":
                    ids = sorted({row["pain_point_id"] for row in normalized})
                    placeholders = ", ".join(["%s"] * len(ids))
                    cur.execute(f"SELECT pain_point_id FROM category_pain_points "
                                f"WHERE pain_point_id IN ({placeholders})", ids)
                    existing_ids = {row["pain_point_id"] for row in cur.fetchall()}
                    missing = sorted(set(ids) - existing_ids)
                    if missing:
                        raise ValueError("以下 pain_point_id 尚未在产品类目痛点表中建立: " + ", ".join(missing))
                for row in normalized:
                    cols = ", ".join(f"`{k}`" for k in row)
                    ph = ", ".join(["%s"] * len(row))
                    upd = ", ".join(
                        f"`{k}`=VALUES(`{k}`)" for k in row
                        if k != pk and not (target == "category_pain_points" and k == "created_ts"))
                    cur.execute(f"INSERT INTO `{target}` ({cols}) VALUES ({ph}) "
                                f"ON DUPLICATE KEY UPDATE {upd}", list(row.values()))
                    if cur.rowcount == 1:
                        inserted += 1
                    elif cur.rowcount == 2:
                        updated += 1
            conn.commit()
        return jsonify({"ok": True, "inserted": inserted, "updated": updated,
                        "total": len(normalized), "skipped": len(raw_rows) - len(normalized)})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@kb_bp.get("/kb/<table>")
def page_kb_table(table):
    if not _check_table(table):
        return "表不存在", 404
    return render_template("scrm/kb_table.html", active="kb",
                           table=table, meta=KB_META[table])


@kb_bp.get("/api/kb/overview")
def api_kb_overview():
    with get_conn() as conn:
        with conn.cursor() as cur:
            tables = []
            for name, meta in KB_META.items():
                cur.execute(f"SELECT COUNT(*) c FROM `{name}`")
                n = cur.fetchone()["c"]
                cur.execute(f"SELECT COUNT(*) c FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (name,))
                tables.append({**meta, "table": name, "rows": n,
                               "readonly": meta.get("readonly", False)})
    return jsonify({"tables": tables})


@kb_bp.get("/api/kb/<table>/columns")
def api_kb_columns(table):
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
    return jsonify({"cols": cols, "pk": _pk_of(cols), "meta": KB_META[table]})


@kb_bp.get("/api/kb/<table>")
def api_kb_list(table):
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    page = max(1, int(request.args.get("page", 1)))
    size = max(1, min(200, int(request.args.get("size", 20))))
    q = (request.args.get("q") or "").strip()
    state = (request.args.get("state") or "").strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            pk = _pk_of(cols)
            clauses, params = [], []
            if q:
                searchable = [c["name"] for c in cols
                              if c["type"] in ("varchar", "char", "text") and not c["pk"]][:4]
                if searchable:
                    clauses.extend(f"`{c}` LIKE %s" for c in searchable)
                    params.extend(f"%{q}%" for _ in searchable)
            if state and any(c["name"] == "state" for c in cols):
                clauses.append("`state`=%s")
                params.append(state)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            cur.execute(f"SELECT COUNT(*) c FROM `{table}`{where}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"SELECT * FROM `{table}`{where} ORDER BY `{pk}` DESC LIMIT %s OFFSET %s"
                if pk else
                f"SELECT * FROM `{table}`{where} LIMIT %s OFFSET %s",
                params + [size, (page - 1) * size])
            rows = [_row_for_display(r, cols) for r in cur.fetchall()]
    return jsonify({"rows": rows, "total": total, "page": page, "size": size})


@kb_bp.get("/api/kb/<table>/row/<pk_val>")
def api_kb_row(table, pk_val):
    """取单行完整数据(编辑用, 列表接口会截断长文本)"""
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            pk = _pk_of(cols)
            cur.execute(f"SELECT * FROM `{table}` WHERE `{pk}`=%s LIMIT 1", (pk_val,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "记录不存在"}), 404
            # JSON 列还原为对象, date 转 ISO, 长文本不截断
            out = {}
            for c in cols:
                v = row.get(c["name"])
                if c["type"] == "json" and isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                out[c["name"]] = v
    return jsonify({"row": out})


@kb_bp.post("/api/kb/<table>")
def api_kb_insert(table):
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    body = request.get_json(silent=True) or {}
    if table in ("category_pain_points", "model_pain_point_matches"):
        # 新增痛点只需品类和用户痛点；其余可由系统生成，降低人工录入成本。
        category = str(body.get("category") or "").strip()[:32]
        pain_point = str(body.get("pain_point") or "").strip()[:128]
        if not category or not pain_point:
            return jsonify({"error": "新增产品类目痛点只需填写产品品类和用户痛点"}), 400
        body["category"] = category
        body["pain_point"] = pain_point
        if not body.get("pain_point_id"):
            digest = hashlib.md5(f"{category}|{pain_point}".encode("utf-8")).hexdigest()[:12].upper()
            body["pain_point_id"] = f"CPP-{digest}"
        if not body.get("trigger_words"):
            body["trigger_words"] = [pain_point]
        if body.get("priority") in (None, ""):
            body["priority"] = 50
        if not body.get("state"):
            body["state"] = "usable"
    if table == "model_pain_point_matches":
        body["match_source"] = "manual"
        if not body.get("matched_params"):
            body["matched_params"] = []
        if not body.get("state"):
            body["state"] = "usable"
    if table == "model_facts":
        error = _validate_model_price_range(body)
        if error:
            return jsonify({"error": error}), 400
    if table in ("category_pain_points", "model_pain_point_matches"):
        error = _validate_pain_point_star_rating(body)
        if error:
            return jsonify({"error": error}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            names, vals = [], []
            now = int(time.time())
            try:
                for c in cols:
                    if c["auto"]:
                        continue
                    if c["name"] in body:
                        vals.append(_parse_value(c, body[c["name"]]))
                    elif c["name"] in ("created_ts", "updated_ts", "created_at"):
                        vals.append(now)  # 时间戳字段缺省自动填
                    elif not c["nullable"] and c["default"] is None:
                        return jsonify({"error": f"缺少必填字段:{c['name']}"}), 400
                    else:
                        continue
                    names.append(c["name"])
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            sql = f"INSERT INTO `{table}` ({', '.join('`%s`' % n for n in names)}) " \
                  f"VALUES ({', '.join(['%s'] * len(names))})"
            try:
                cur.execute(sql, vals)
                if table == "model_facts":
                    _sync_model_pain_point_matches(cur, str(body.get("model_id") or ""))
            except Exception as e:
                return jsonify({"error": f"写入失败:{str(e)[:200]}"}), 400
            conn.commit()
    return jsonify({"ok": True})


@kb_bp.post("/api/kb/<table>/<pk_val>/update")
def api_kb_update(table, pk_val):
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    if KB_META[table].get("readonly"):
        return jsonify({"error": "该表为只读(系统写入)"}), 403
    body = request.get_json(silent=True) or {}
    if table == "model_pain_point_matches":
        body["match_source"] = "manual"
    if table == "model_facts" and any(k in body for k in ("price_min", "price_max", "price_step")):
        # 编辑时先与数据库原值合并，再校验完整区间。
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT price_min,price_max,price_step FROM model_facts WHERE model_id=%s", (pk_val,))
                existing = cur.fetchone() or {}
        error = _validate_model_price_range({**existing, **body})
        if error:
            return jsonify({"error": error}), 400
    if table == "category_pain_points":
        error = _validate_pain_point_star_rating(body)
        if error:
            return jsonify({"error": error}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            pk = _pk_of(cols)
            sets, vals = [], []
            try:
                for c in cols:
                    if c["pk"] or c["auto"] or c["name"] not in body:
                        continue
                    sets.append(f"`{c['name']}`=%s")
                    raw = body[c["name"]]
                    # 历史硬红线允许空触发模式（例如依赖外部判断的规则），保留为空字符串而不是 NULL。
                    if table == "guardrails" and c["name"] == "trigger_pattern" and raw in (None, ""):
                        vals.append("")
                    else:
                        vals.append(_parse_value(c, raw))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if not sets:
                return jsonify({"error": "没有可更新字段"}), 400
            if any(c["name"] == "updated_ts" for c in cols) and "updated_ts" not in body:
                sets.append("`updated_ts`=%s")
                vals.append(int(time.time()))
            vals.append(pk_val)
            try:
                cur.execute(f"UPDATE `{table}` SET {', '.join(sets)} WHERE `{pk}`=%s", vals)
                if table == "model_facts":
                    _sync_model_pain_point_matches(cur, pk_val)
            except Exception as e:
                return jsonify({"error": f"更新失败:{str(e)[:200]}"}), 400
            conn.commit()
    return jsonify({"ok": True})


@kb_bp.post("/api/kb/<table>/<pk_val>/confirm")
def api_kb_confirm(table, pk_val):
    """摄取管道写入的待确认(pending)记录，人工核实后一键翻 usable 并刷新核验日期。"""
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    if KB_META[table].get("readonly"):
        return jsonify({"error": "该表为只读(系统写入)"}), 403
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            if not any(c["name"] == "state" for c in cols):
                return jsonify({"error": "该表没有 state 字段"}), 400
            pk = _pk_of(cols)
            cur.execute(f"SELECT `state` FROM `{table}` WHERE `{pk}`=%s LIMIT 1", (pk_val,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "记录不存在"}), 404
            if row["state"] != "pending":
                return jsonify({"error": f"仅待确认(pending)记录可确认，当前状态：{row['state']}"}), 400
            sets = ["`state`='usable'"]
            vals = []
            if any(c["name"] == "verified_at" for c in cols):
                sets.append("`verified_at`=%s")
                vals.append(datetime.now().date().isoformat())
            if any(c["name"] == "confidence" for c in cols):
                sets.append("`confidence`='mid'")
            if any(c["name"] == "updated_ts" for c in cols):
                sets.append("`updated_ts`=%s")
                vals.append(int(time.time()))
            vals.append(pk_val)
            cur.execute(f"UPDATE `{table}` SET {', '.join(sets)} WHERE `{pk}`=%s", vals)
            if table == "model_facts":
                _sync_model_pain_point_matches(cur, pk_val)
            conn.commit()
    return jsonify({"ok": True})


@kb_bp.post("/api/kb/<table>/<pk_val>/delete")
def api_kb_delete(table, pk_val):
    if not _check_table(table):
        return jsonify({"error": "表不存在"}), 404
    if KB_META[table].get("readonly"):
        return jsonify({"error": "该表为只读(系统写入)"}), 403
    with get_conn() as conn:
        with conn.cursor() as cur:
            cols = _columns(cur, table)
            pk = _pk_of(cols)
            cur.execute(f"DELETE FROM `{table}` WHERE `{pk}`=%s", (pk_val,))
            conn.commit()
    return jsonify({"ok": True})
