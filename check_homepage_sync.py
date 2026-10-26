from db import get_conn
from update_up_data_from_workbook import build_records, read_rows


rows = build_records(read_rows(r"D:\\下载\\运营部_所有账号.xlsx"))[2]
candidates = [row for row in rows if row["homepage_url"]]
pairs = {(row["platform"], row["nickname"]) for row in candidates}

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='平台运营账号' AND COLUMN_NAME='主页链接'"
        )
        column_exists = cur.fetchone()["n"]
        if not column_exists:
            print({"workbook_link_candidates": len(candidates), "homepage_column_exists": False})
        else:
            cur.execute(
                "SELECT `平台` AS platform, `昵称` AS nickname, "
                "COALESCE(`主页链接`, '') AS homepage_url FROM `平台运营账号`"
            )
            db_rows = cur.fetchall()
            matched = [row for row in db_rows if (row["platform"], row["nickname"]) in pairs]
            missing = [row for row in matched if not row["homepage_url"].strip()]
            print({
                "workbook_link_candidates": len(candidates),
                "homepage_column_exists": True,
                "db_rows": len(db_rows),
                "matched": len(matched),
                "missing_links": len(missing),
                "missing_examples": [(row["platform"], row["nickname"]) for row in missing[:10]],
            })
