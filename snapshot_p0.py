# -*- coding: utf-8 -*-
"""P0 基线快照：导出 Skill、知识路由、历史建议回复和主体映射，不改写数据库。"""
import csv
import json
import sys
import time
from pathlib import Path

from config import COMMENT_TABLES
from db import get_columns, get_conn


def _dump_json(path, rows):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2, default=str)


def _table_exists(cur, name):
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (name,),
    )
    return bool(cur.fetchone()["n"])


def _read_table(cur, name):
    if not _table_exists(cur, name):
        return []
    cur.execute(f"SELECT * FROM `{name}`")
    return cur.fetchall()


def _reply_snapshot(cur):
    rows = []
    for table in COMMENT_TABLES:
        columns = {item["name"] for item in get_columns(table)}
        if not {"id", "reply_suggestion"}.issubset(columns):
            continue
        comment_id = "comment_id" if "comment_id" in columns else "id"
        cur.execute(
            f"SELECT `id` AS src_pk, CAST(`{comment_id}` AS CHAR) AS comment_id, "
            "CAST(`content` AS CHAR) AS content, CAST(`reply_suggestion` AS CHAR) AS reply "
            f"FROM `{table}` WHERE `reply_suggestion` IS NOT NULL AND `reply_suggestion`<>''"
        )
        rows.extend(dict(row, src_table=table) for row in cur.fetchall())
    return rows


def _mapping_report(cur):
    creators = _read_table(cur, "UP主管理")
    accounts = _read_table(cur, "平台运营账号")
    skills = _read_table(cur, "scrm_skill")
    creator_ids = {row["id"] for row in creators}
    orphan_accounts = [row for row in accounts if not row.get("UP主ID") or row["UP主ID"] not in creator_ids]
    creators_by_name = {str(row.get("昵称") or ""): row["id"] for row in creators}
    skill_mappings = []
    for skill in skills:
        bound_name = str(skill.get("bind_up") or "")
        skill_mappings.append({
            "skill_id": skill.get("id"),
            "skill_name": skill.get("name"),
            "bind_up": bound_name,
            "resolved_up_id": creators_by_name.get(bound_name),
            "status": "mapped" if creators_by_name.get(bound_name) else "unmapped",
        })
    return {
        "creator_count": len(creators),
        "platform_account_count": len(accounts),
        "skill_count": len(skills),
        "orphan_platform_accounts": orphan_accounts,
        "skill_mappings": skill_mappings,
    }


def main():
    root = Path("backups") / ("p0-" + time.strftime("%Y%m%d-%H%M%S"))
    root.mkdir(parents=True, exist_ok=False)
    with get_conn() as conn:
        with conn.cursor() as cur:
            data = {
                "scrm_skill": _read_table(cur, "scrm_skill"),
                "persona_pack": _read_table(cur, "persona_pack"),
                "routing_matrix": _read_table(cur, "routing_matrix"),
                "reply_log": _read_table(cur, "reply_log"),
                "UP主管理": _read_table(cur, "UP主管理"),
                "平台运营账号": _read_table(cur, "平台运营账号"),
            }
            replies = _reply_snapshot(cur)
            mapping = _mapping_report(cur)
    for name, rows in data.items():
        _dump_json(root / f"{name}.json", rows)
    _dump_json(root / "historical_reply_suggestions.json", replies)
    _dump_json(root / "mapping_report.json", mapping)
    with (root / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["artifact", "rows"])
        writer.writeheader()
        for name, rows in data.items():
            writer.writerow({"artifact": name, "rows": len(rows)})
        writer.writerow({"artifact": "historical_reply_suggestions", "rows": len(replies)})
    print(root.resolve())
    print(json.dumps({"orphan_platform_accounts": len(mapping["orphan_platform_accounts"]),
                      "unmapped_skills": sum(x["status"] != "mapped" for x in mapping["skill_mappings"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"P0 快照失败: {exc}", file=sys.stderr)
        raise
