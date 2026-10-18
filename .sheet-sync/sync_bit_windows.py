# -*- coding: utf-8 -*-
"""Preview or synchronize BIT window numbers from the operations workbook."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_conn
from update_up_data_from_workbook import header_map, read_rows, val


def source_mapping(workbook_path: Path):
    rows = read_rows(workbook_path)
    headers = rows[0]
    hmap = header_map(headers)
    grouped = defaultdict(set)
    for row in rows[1:]:
        nickname = val(row, hmap, "账号名称")
        bit_window = val(row, hmap, "BIT窗口号")
        if nickname and bit_window:
            grouped[nickname].add(bit_window)
    conflicts = {name: sorted(values) for name, values in grouped.items() if len(values) > 1}
    mapping = {name: next(iter(values)) for name, values in grouped.items() if len(values) == 1}
    return mapping, conflicts


def compare(mapping):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `昵称`, `BIT窗口` FROM `UP主管理`")
            current = {row["昵称"]: (row["BIT窗口"] or "").strip() for row in cur.fetchall()}
    missing = sorted(name for name in mapping if name not in current)
    unchanged = sorted(name for name, bit in mapping.items() if name in current and current[name] == bit)
    changes = {name: bit for name, bit in mapping.items() if name in current and current[name] != bit}
    return changes, unchanged, missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    mapping, conflicts = source_mapping(args.workbook)
    changes, unchanged, missing = compare(mapping)
    print(f"source_unique={len(mapping)}")
    print(f"changes={len(changes)}")
    print(f"unchanged={len(unchanged)}")
    print(f"missing_up={len(missing)}")
    print(f"conflicting_source={len(conflicts)}")
    if missing:
        print("missing_names=" + "、".join(missing))
    if conflicts:
        print("conflicts=" + "; ".join(f"{name}:{'、'.join(values)}" for name, values in conflicts.items()))

    if not args.apply:
        return
    if conflicts:
        raise SystemExit("存在同名账号的冲突窗口号，未执行写入。")

    with get_conn() as conn:
        with conn.cursor() as cur:
            updated = 0
            for nickname, bit_window in changes.items():
                updated += cur.execute(
                    "UPDATE `UP主管理` SET `BIT窗口`=%s, `updated_ts`=UNIX_TIMESTAMP() WHERE `昵称`=%s",
                    (bit_window, nickname),
                )
        conn.commit()
    print(f"updated={updated}")


if __name__ == "__main__":
    main()
