# -*- coding: utf-8 -*-
"""Read the operations workbook and enrich UP/account management tables."""
import argparse
import html
import re
import zipfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _repair_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _col_to_num(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch.upper()) - 64
    return n


def _cell_text(cell_xml):
    texts = re.findall(r"<t[^>]*>(.*?)</t>", cell_xml, flags=re.S)
    if texts:
        return _repair_text(html.unescape("".join(texts)))
    value = re.search(r"<v[^>]*>(.*?)</v>", cell_xml, flags=re.S)
    return html.unescape(value.group(1)).strip() if value else ""


def read_rows(path):
    with zipfile.ZipFile(path) as zf:
        sheet_name = next(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
        raw = zf.read(sheet_name)
    xml = raw.decode("utf-8", "replace")
    rows = []
    for row_xml in re.findall(r"<row\b[^>]*>.*?</row>", xml, flags=re.S):
        cells = {}
        for match in re.finditer(r"<c\b([^>]*)>(.*?)</c>", row_xml, flags=re.S):
            attrs, body = match.groups()
            ref = re.search(r'\br="([A-Z]+)(\d+)"', attrs)
            if not ref:
                continue
            col = _col_to_num(ref.group(1))
            cells[col] = _cell_text(body)
        if cells:
            max_col = max(cells)
            rows.append([cells.get(i, "") for i in range(1, max_col + 1)])
    return rows


def parse_fans(value):
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([wW万亿]?)", text)
    if not m:
        return None
    mult = {"": 1, "w": 10000, "W": 10000, "万": 10000, "亿": 100000000}[m.group(2)]
    try:
        return int(Decimal(m.group(1)) * mult)
    except (InvalidOperation, ValueError):
        return None


def header_map(headers):
    return {h: i for i, h in enumerate(headers) if h}


def val(row, hmap, name):
    i = hmap.get(name)
    return row[i].strip() if i is not None and i < len(row) and row[i] is not None else ""


PLATFORM_COLUMNS = {
    "B站": {"id": "B站ID", "fans": "B站粉丝量", "bit": "BIT窗口"},
    "抖音": {"id": "抖音ID-IT用", "fans": "抖音粉丝量", "bit": "BIT窗口"},
    "小红书": {"id": "小红书ID", "fans": "小红书粉丝量", "bit": "BIT窗口"},
    "微博": {"id": "微博ID", "fans": "微博粉丝量", "bit": "BIT窗口"},
    "知乎": {"id": "知乎ID", "fans": "知乎粉丝量", "bit": "BIT窗口"},
}


def split_platforms(text):
    return [p.strip() for p in re.split(r"[,，、/]+", text or "") if p.strip()]


def build_records(rows):
    headers = rows[0]
    hmap = header_map(headers)
    up_records = {}
    account_records = []
    for row in rows[1:]:
        nickname = val(row, hmap, "账号名称")
        if not nickname:
            continue
        up = up_records.setdefault(nickname, {})
        for src, dst in (
            ("BIT窗口", "bit_window"),
            ("账号等级", "account_level"),
            ("运营(新)", "operator"),
            ("运营", "operator"),
            ("出镜人", "presenter"),
            ("组别", "team_group"),
            ("B站绑定公司", "bound_company"),
            ("账号绑定公司", "bound_company"),
            ("商务微信", "business_wechat"),
            ("邮箱", "business_email"),
            ("商务邮箱", "business_email"),
        ):
            cell = val(row, hmap, src)
            if cell and not up.get(dst):
                up[dst] = cell
        platforms = split_platforms(val(row, hmap, "平台"))
        for platform in platforms:
            cols = PLATFORM_COLUMNS.get(platform, {})
            account_records.append({
                "platform": platform,
                "nickname": nickname,
                "account_id": val(row, hmap, cols.get("id", "")),
                "fans": parse_fans(val(row, hmap, cols.get("fans", ""))),
                "bit_window": val(row, hmap, cols.get("bit", "")),
            })
    return headers, up_records, account_records


def preview(path):
    rows = read_rows(path)
    headers, up_records, account_records = build_records(rows)
    print("rows", len(rows) - 1)
    print("columns", len(headers))
    print("headers", headers)
    print("up_records", len(up_records))
    print("account_records", len(account_records))
    print("platform_counts", dict(Counter(r["platform"] for r in account_records)))
    filled = defaultdict(int)
    for rec in up_records.values():
        for k, v in rec.items():
            if v:
                filled[k] += 1
    print("up_field_filled", dict(filled))


def apply_updates(path):
    from db import get_conn

    rows = read_rows(path)
    _, up_records, account_records = build_records(rows)
    with get_conn() as conn:
        with conn.cursor() as cur:
            up_updated = 0
            for nickname, rec in up_records.items():
                fields = []
                params = []
                mapping = {
                    "bit_window": ("`BIT窗口`", 255),
                    "account_level": ("`账号等级`", 64),
                    "operator": ("`运营人员`", 64),
                    "presenter": ("`出镜人`", 64),
                    "team_group": ("`组别`", 64),
                    "bound_company": ("`账号绑定公司`", 128),
                    "business_wechat": ("`商务微信`", 128),
                    "business_email": ("`商务邮箱`", 128),
                }
                for key, (col, limit) in mapping.items():
                    value = rec.get(key)
                    if value:
                        fields.append(f"{col}=%s")
                        params.append(value[:limit])
                if fields:
                    fields.append("`updated_ts`=UNIX_TIMESTAMP()")
                    params.append(nickname)
                    up_updated += cur.execute(
                        f"UPDATE `UP主管理` SET {', '.join(fields)} WHERE `昵称`=%s",
                        params,
                    )
            account_updated = 0
            for rec in account_records:
                fields = []
                params = []
                if rec.get("account_id"):
                    fields.append("`ID`=%s")
                    params.append(rec["account_id"][:255])
                if rec.get("fans") is not None:
                    fields.append("`粉丝`=%s")
                    params.append(rec["fans"])
                if rec.get("bit_window"):
                    fields.append("`Bit窗口`=%s")
                    params.append(rec["bit_window"][:255])
                if not fields:
                    continue
                params.extend([rec["platform"], rec["nickname"]])
                account_updated += cur.execute(
                    f"UPDATE `平台运营账号` SET {', '.join(fields)} "
                    "WHERE `平台`=%s AND `昵称`=%s",
                    params,
                )
        conn.commit()
    print("up_updated", up_updated)
    print("account_updated", account_updated)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply_updates(args.workbook)
    else:
        preview(args.workbook)


if __name__ == "__main__":
    main()
