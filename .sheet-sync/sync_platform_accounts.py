# -*- coding: utf-8 -*-
"""Preview or synchronize platform-account fields from the operations workbook."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_conn
from update_up_data_from_workbook import (
    PLATFORM_COLUMNS,
    header_map,
    parse_fans,
    read_rows,
    split_platforms,
    val,
)


def source_records(workbook_path: Path):
    rows = read_rows(workbook_path)
    hmap = header_map(rows[0])
    grouped = defaultdict(lambda: defaultdict(set))
    for row in rows[1:]:
        nickname = val(row, hmap, "账号名称")
        bit_window = val(row, hmap, "BIT窗口号")
        if not nickname:
            continue
        for platform in split_platforms(val(row, hmap, "平台")):
            fields = grouped[(platform, nickname)]
            columns = PLATFORM_COLUMNS.get(platform, {})
            account_id = val(row, hmap, columns.get("id", ""))
            fans = parse_fans(val(row, hmap, columns.get("fans", "")))
            if account_id:
                fields["ID"].add(account_id)
            if fans is not None:
                fields["粉丝"].add(fans)
            if bit_window:
                fields["Bit窗口"].add(bit_window)

    conflicts = {
        key: {field: sorted(values) for field, values in fields.items() if len(values) > 1}
        for key, fields in grouped.items()
        if any(len(values) > 1 for values in fields.values())
    }
    records = {
        key: {field: next(iter(values)) for field, values in fields.items() if len(values) == 1}
        for key, fields in grouped.items()
        if key not in conflicts and fields
    }
    return records, conflicts


def compare(records):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `平台`, `昵称`, `ID`, `粉丝`, `Bit窗口` FROM `平台运营账号`")
            current = {
                (row["平台"], row["昵称"]): {
                    "ID": (row["ID"] or "").strip(),
                    "粉丝": row["粉丝"],
                    "Bit窗口": (row["Bit窗口"] or "").strip(),
                }
                for row in cur.fetchall()
            }
    missing = sorted(key for key in records if key not in current)
    changes = {}
    unchanged = []
    for key, source in records.items():
        if key not in current:
            continue
        delta = {field: value for field, value in source.items() if current[key][field] != value}
        if delta:
            changes[key] = delta
        else:
            unchanged.append(key)
    return changes, unchanged, missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    records, conflicts = source_records(args.workbook)
    changes, unchanged, missing = compare(records)
    field_counts = defaultdict(int)
    for delta in changes.values():
        for field in delta:
            field_counts[field] += 1
    print(f"source_records={len(records)}")
    print(f"changes={len(changes)}")
    print(f"unchanged={len(unchanged)}")
    print(f"missing_accounts={len(missing)}")
    print(f"conflicting_source={len(conflicts)}")
    print("changed_fields=" + ",".join(f"{field}:{count}" for field, count in sorted(field_counts.items())))
    if missing:
        print("missing_keys=" + "; ".join(f"{platform}/{nickname}" for platform, nickname in missing))
    if conflicts:
        print("conflicts=" + "; ".join(
            f"{platform}/{nickname}:" + ",".join(f"{field}={'、'.join(map(str, values))}" for field, values in fields.items())
            for (platform, nickname), fields in conflicts.items()
        ))

    if not args.apply:
        return
    if conflicts:
        raise SystemExit("存在同一平台账号的冲突源数据，未执行写入。")

    with get_conn() as conn:
        with conn.cursor() as cur:
            updated = 0
            for (platform, nickname), delta in changes.items():
                assignments = ", ".join(f"`{field}`=%s" for field in delta)
                params = list(delta.values()) + [platform, nickname]
                updated += cur.execute(
                    f"UPDATE `平台运营账号` SET {assignments} WHERE `平台`=%s AND `昵称`=%s",
                    params,
                )
        conn.commit()
    print(f"updated={updated}")


if __name__ == "__main__":
    main()
