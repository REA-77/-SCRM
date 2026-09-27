# -*- coding: utf-8 -*-
"""数据库驱动的抖音评论自动回复

流程:
1. 从 crawler_cookies_account 表取指定账号 cookie (不再扫码登录)
2. 从 douyin_aweme_comment 表取 id 区间内 handled=0 且有 reply_suggestion 的评论,
   关联 douyin_aweme 得到作品标题
3. 生成计划 JSON → 调用 douyin-creator-tools/db-auto-reply.mjs 完成页面回复
4. 回复成功(replied)或页面已显示有回复(skipped_already_replied) → 回写 handled=1

用法:
  py douyin_db_reply.py --account-id 2 --start-id 1 --end-id 10           # 正式回复
  py douyin_db_reply.py --account-id 2 --start-id 1 --end-id 10 --dry-run # 只输入不发送
"""
import argparse
import json
import os
import subprocess
import sys

from db import get_conn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(BASE_DIR, "douyin-creator-tools")
INPUT_PATH = os.path.join(REPO_DIR, "comments-output", "db-reply-input.json")
OUTPUT_PATH = os.path.join(REPO_DIR, "comments-output", "db-reply-result.json")


def load_cookie(account_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_name, platform_name, cookies, status "
                "FROM crawler_cookies_account WHERE id=%s",
                (account_id,),
            )
            row = cur.fetchone()
    if not row:
        raise SystemExit(f"crawler_cookies_account 中不存在 id={account_id}")
    if row["platform_name"] != "dy":
        print(f"警告: 账号 {row['account_name']} 的 platform_name={row['platform_name']}, 非 dy")
    if row["status"] != 0:
        print(
            f"警告: 账号 {row['account_name']} status={row['status']} (标记失效), "
            "仍按指定账号尝试, cookie 失效时会在登录校验阶段报错"
        )
    if not row["cookies"]:
        raise SystemExit(f"账号 {row['account_name']} 的 cookies 为空")
    return row


def load_comments(start_id, end_id, limit=0):
    if limit > 0:
        # 不限 id 区间, 取全表前 N 条待处理评论
        sql = (
            "SELECT c.id AS row_id, c.comment_id, c.aweme_id, c.nickname, c.content, "
            "c.reply_suggestion, a.title "
            "FROM douyin_aweme_comment c "
            "LEFT JOIN douyin_aweme a ON a.aweme_id = c.aweme_id "
            "WHERE c.handled=0 AND c.reply_suggestion IS NOT NULL AND c.reply_suggestion <> '' "
            "ORDER BY c.id LIMIT %s"
        )
        params = (limit,)
    else:
        sql = (
            "SELECT c.id AS row_id, c.comment_id, c.aweme_id, c.nickname, c.content, "
            "c.reply_suggestion, a.title "
            "FROM douyin_aweme_comment c "
            "LEFT JOIN douyin_aweme a ON a.aweme_id = c.aweme_id "
            "WHERE c.id BETWEEN %s AND %s AND c.handled=0 "
            "AND c.reply_suggestion IS NOT NULL AND c.reply_suggestion <> '' "
            "ORDER BY c.id"
        )
        params = (start_id, end_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    plans, skipped = [], []
    for row in rows:
        if not row["title"]:
            skipped.append((row["row_id"], "douyin_aweme 表中无该作品标题, 无法选择作品"))
            continue
        plans.append(
            {
                "id": str(row["comment_id"]),
                "awemeId": str(row["aweme_id"]),
                "workTitle": row["title"],
                "commentText": row["content"] or "",
                "replyMessage": row["reply_suggestion"],
            }
        )
    return plans, skipped


def mark_handled(comment_ids):
    updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for cid in comment_ids:
                cur.execute(
                    "UPDATE douyin_aweme_comment SET handled=1 WHERE comment_id=%s AND handled=0",
                    (cid,),
                )
                updated += cur.rowcount
        conn.commit()
    return updated


def main():
    parser = argparse.ArgumentParser(description="douyin_aweme_comment 自动回复(handled 标记)")
    parser.add_argument("--account-id", type=int, default=2, help="crawler_cookies_account.id")
    parser.add_argument("--start-id", type=int, default=1, help="评论表起始 id(自增主键)")
    parser.add_argument("--end-id", type=int, default=10, help="评论表结束 id(含)")
    parser.add_argument("--limit", type=int, default=0,
                        help="取全表前 N 条待处理评论(优先级高于 id 区间)")
    parser.add_argument("--dry-run", action="store_true", help="只输入回复文本, 不点击发送")
    args = parser.parse_args()

    account = load_cookie(args.account_id)
    plans, skipped = load_comments(args.start_id, args.end_id, args.limit)
    print(f"账号: id={account['id']} {account['account_name']}")
    if args.limit > 0:
        print(f"待回复评论: {len(plans)} 条 (全表前 {args.limit} 条待处理)")
    else:
        print(f"待回复评论: {len(plans)} 条 (id {args.start_id}-{args.end_id}, handled=0 且有回复推荐)")
    for row_id, reason in skipped:
        print(f"  跳过评论#{row_id}: {reason}")
    if not plans:
        print("没有可执行的回复计划")
        return

    os.makedirs(os.path.dirname(INPUT_PATH), exist_ok=True)
    with open(INPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "account": {"id": account["id"], "account_name": account["account_name"]},
                "cookieHeader": account["cookies"],
                "plans": plans,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    cmd = ["node", "db-auto-reply.mjs", INPUT_PATH]
    if args.dry_run:
        cmd.append("--dry-run")
    cmd += ["--out", OUTPUT_PATH]
    print(f"执行: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=REPO_DIR)
    if result.returncode != 0:
        raise SystemExit(f"回复脚本退出码 {result.returncode}, 未回写 handled")

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        output = json.load(f)

    handled_ids = []
    print("\n===== 回复结果 =====")
    for r in output.get("results", []):
        status = r.get("status")
        cid = r.get("replyPlanId")
        mark = ""
        if status == "replied":
            handled_ids.append(cid)
            mark = "→ handled=1"
        elif status == "skipped_already_replied":
            handled_ids.append(cid)
            mark = "→ handled=1 (页面已有回复)"
        content = (r.get("commentText") or "")[:20]
        print(f"[{status}] {cid} {content} {mark}")

    if args.dry_run:
        print("\ndry-run 模式: 不回写 handled")
        return
    if handled_ids:
        updated = mark_handled(handled_ids)
        print(f"\n已回写 handled=1: {updated} 条")
    else:
        print("\n本次无成功回复, 不回写 handled")


if __name__ == "__main__":
    main()
