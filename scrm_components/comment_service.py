"""评论读取用例：详情装配、跨表内容补全与线程分页。"""
from comment_paging import thread_page_sql
from scrm_components.comment_queries import comment_filters, comment_fragment, comments_union
from scrm_components.platforms import PLATFORM_MAP, PLATFORM_OF


THREAD_PAGE_SIZE = 50


def with_platform(rows):
    for row in rows or []:
        row["platform"] = PLATFORM_OF.get(row.get("src_table"), "?")
    return rows


class CommentReadService:
    """Uses a caller-owned database cursor so routes retain transaction ownership."""
    def __init__(self, cursor):
        self.cursor = cursor

    def enrich_contents(self, rows):
        grouped = {}
        for row in rows or []:
            content_id = str(row.get("content_id") or "")
            if content_id:
                grouped.setdefault(row["src_table"], set()).add(content_id)
        info = {}
        for table, ids in grouped.items():
            mapping = PLATFORM_MAP[table]
            try:
                placeholders = ",".join(["%s"] * len(ids))
                self.cursor.execute(
                    f"SELECT `{mapping['cid']}` cid, CAST(`{mapping['ctitle']}` AS CHAR) title, "
                    f"CAST({mapping['curl']} AS CHAR) url, {mapping['cnick']} AS author_nickname, "
                    f"COALESCE(NULLIF(`category`, ''), '原创') AS video_category "
                    f"FROM `{mapping['ctable']}` WHERE `{mapping['cid']}` IN ({placeholders})", list(ids))
                for item in self.cursor.fetchall():
                    info[(table, str(item["cid"]))] = item
            except Exception:
                # A missing optional content column must not block comment reads.
                continue
        for row in rows or []:
            item = info.get((row.get("src_table"), str(row.get("content_id") or "")))
            row["video_title"] = item["title"] if item else None
            row["video_url"] = item["url"] if item else None
            row["author_nickname"] = item.get("author_nickname") if item else None
            row["video_category"] = item.get("video_category") if item else None
            nickname = str(row.get("nickname") or "").strip()
            author = str(row.get("author_nickname") or "").strip()
            row["is_self"] = bool(nickname and author and nickname == author)
        return rows

    def load_thread_page(self, table, root_pk, args, offset=0):
        mapping = PLATFORM_MAP[table]
        self.cursor.execute(f"SELECT `{mapping['cid']}` AS content_id FROM `{table}` WHERE id=%s", (root_pk,))
        root = self.cursor.fetchone()
        if not root:
            return [], False
        where, params = comment_filters(args)
        sql = thread_page_sql(table, mapping["cid"], mapping["time"], comment_fragment(table), where[7:] if where else "1=1")
        self.cursor.execute(sql, [root["content_id"], root_pk] + params + [THREAD_PAGE_SIZE + 1, offset])
        records = self.cursor.fetchall()
        more = len(records) > THREAD_PAGE_SIZE
        rows = with_platform(records[:THREAD_PAGE_SIZE])
        for row in rows:
            row["matches_filter"] = bool(row["matches_filter"])
            row["parent_missing"] = bool(row["parent_missing"])
            row["thread_root"] = root_pk
        return self.enrich_contents(rows), more

    def load_content(self, table, content_id):
        mapping = PLATFORM_MAP[table]
        try:
            self.cursor.execute(
                f"SELECT CAST(`{mapping['ctitle']}` AS CHAR) title, CAST({mapping['curl']} AS CHAR) url, "
                f"{mapping['cnick']} upname FROM `{mapping['ctable']}` WHERE `{mapping['cid']}`=%s LIMIT 1", (content_id,))
            return self.cursor.fetchone()
        except Exception:
            return None

    def load_detail(self, table, rid):
        self.cursor.execute(f"SELECT * FROM ({comment_fragment(table)}) Z WHERE Z.src_pk=%s", (rid,))
        row = self.cursor.fetchone()
        if not row:
            return None
        row["platform"] = PLATFORM_MAP[table]["platform"]
        row["content_info"] = self.load_content(table, row["content_id"])
        self.cursor.execute(
            f"SELECT src_pk, nickname, content, ctime, neg, intent, handled FROM {comments_union()} "
            "WHERE src_table=%s AND content_id=%s ORDER BY ctime DESC LIMIT 11", (table, row["content_id"]))
        row["thread"] = self.cursor.fetchall()
        return row
