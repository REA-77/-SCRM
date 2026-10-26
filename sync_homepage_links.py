"""将运营工作簿中的主页链接按“平台 + 昵称”回填到平台运营账号。"""
import argparse
from collections import defaultdict
import re

from db import get_conn
from update_up_data_from_workbook import build_records, read_rows


def main(source):
    records = build_records(read_rows(source))[2]
    links_by_account = defaultdict(set)
    for record in records:
        link = (record.get("homepage_url") or "").strip()
        if link:
            links_by_account[(record["platform"], record["nickname"])].add(link)

    # 工作簿中个别重复行只含 B 站根地址；同账号存在具体主页时，根地址不视为冲突来源。
    for key, values in list(links_by_account.items()):
        specific = [value for value in values
                    if not re.fullmatch(r"https?://space\.bilibili\.com/?", value, re.I)]
        if len(specific) == 1 and len(values) > 1:
            links_by_account[key] = {specific[0]}

    conflicts = {key: values for key, values in links_by_account.items() if len(values) > 1}
    if conflicts:
        examples = list(conflicts.items())[:5]
        raise ValueError(f"发现 {len(conflicts)} 个账号有冲突的主页链接，未执行回填：{examples}")

    rows = [(next(iter(values)), platform, nickname)
            for (platform, nickname), values in links_by_account.items()]
    with get_conn() as conn:
        with conn.cursor() as cur:
            updated = 0
            unmatched = []
            preserved = 0
            for homepage_url, platform, nickname in rows:
                cur.execute(
                    "SELECT COALESCE(`主页链接`, '') AS homepage_url FROM `平台运营账号` "
                    "WHERE `平台`=%s AND `昵称`=%s FOR UPDATE",
                    (platform, nickname),
                )
                current = cur.fetchone()
                if not current:
                    unmatched.append((platform, nickname))
                    continue
                if current["homepage_url"].strip():
                    preserved += 1
                    continue
                cur.execute(
                    "UPDATE `平台运营账号` SET `主页链接`=%s WHERE `平台`=%s AND `昵称`=%s",
                    (homepage_url[:1000], platform, nickname),
                )
                updated += cur.rowcount
        conn.commit()
    print({
        "source_links": len(rows),
        "updated": updated,
        "preserved_existing": preserved,
        "unmatched": len(unmatched),
        "unmatched_examples": unmatched[:10],
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", default=r"D:\下载\运营部_所有账号.xlsx")
    main(parser.parse_args().source)
