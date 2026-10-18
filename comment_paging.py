"""MySQL 8 comment thread pagination without loading full tables into Python."""


def thread_roots_sql(edges_sql, matches_sql):
    # Follow only matching comments to their roots. JSON paths stop cycles;
    # the smallest ID in a cycle is its stable representative.
    return f"""WITH RECURSIVE
    edges AS ({edges_sql}),
    matched AS ({matches_sql}),
    ancestry AS (
        SELECT n.src_table, n.src_pk, n.parent_pk,
               m.ctime AS match_time, m.src_pk AS match_pk,
               JSON_ARRAY(CAST(n.src_pk AS CHAR)) AS visited
        FROM edges n JOIN matched m
          ON m.src_table=n.src_table AND m.src_pk=n.src_pk
        UNION ALL
        SELECT p.src_table, p.src_pk, p.parent_pk, a.match_time, a.match_pk,
               JSON_ARRAY_APPEND(a.visited, '$', CAST(p.src_pk AS CHAR))
        FROM ancestry a JOIN edges p
          ON p.src_table=a.src_table AND p.src_pk=a.parent_pk
        WHERE NOT JSON_CONTAINS(a.visited, JSON_QUOTE(CAST(a.parent_pk AS CHAR)))
    ),
    resolved AS (
        SELECT a.src_table,
          CASE WHEN a.parent_pk IS NULL THEN a.src_pk ELSE (
            SELECT MIN(j.pk) FROM JSON_TABLE(a.visited, '$[*]'
                COLUMNS (ord FOR ORDINALITY, pk BIGINT PATH '$')) j
            WHERE j.ord >= 1 + CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(
                JSON_UNQUOTE(JSON_SEARCH(a.visited, 'one', CAST(a.parent_pk AS CHAR))),
                '[', -1), ']', 1) AS UNSIGNED)
          ) END AS root_pk,
          CONCAT(LPAD(COALESCE(a.match_time,0),20,'0'),LPAD(a.match_pk,20,'0')) AS sort_key
        FROM ancestry a
        WHERE a.parent_pk IS NULL
           OR JSON_CONTAINS(a.visited, JSON_QUOTE(CAST(a.parent_pk AS CHAR)))
    ),
    roots AS (
        SELECT src_table, root_pk, MAX(sort_key) AS sort_key
        FROM resolved GROUP BY src_table, root_pk
    ) """


def thread_page_sql(table, content_column, time_column, fragment, filter_expression):
    # Restrict traversal to one content item and return just one bounded page.
    return f"""WITH RECURSIVE nodes AS (
        SELECT id, CAST(comment_id AS CHAR) AS comment_id,
               CAST(parent_comment_id AS CHAR) AS parent_id,
               CAST({time_column} AS UNSIGNED) AS ctime
        FROM `{table}` WHERE `{content_column}`=%s
    ), branch AS (
        SELECT id, comment_id, parent_id, 0 AS depth,
            JSON_ARRAY(CAST(id AS CHAR)) AS visited,
            CAST('' AS CHAR(42000)) AS sort_path
        FROM nodes WHERE id=%s
        UNION ALL
        SELECT n.id, n.comment_id, n.parent_id, b.depth+1,
            JSON_ARRAY_APPEND(b.visited, '$', CAST(n.id AS CHAR)),
            CONCAT(b.sort_path, LPAD(COALESCE(n.ctime,0),20,'0'), LPAD(n.id,20,'0'), '/')
        FROM branch b JOIN nodes n ON n.parent_id=b.comment_id
        WHERE n.parent_id NOT IN ('','0')
          AND NOT JSON_CONTAINS(b.visited, JSON_QUOTE(CAST(n.id AS CHAR)))
    )
    SELECT /*+ MAX_EXECUTION_TIME(15000) */ U.*, b.depth,
           CASE WHEN {filter_expression} THEN 1 ELSE 0 END AS matches_filter,
           CASE WHEN b.depth=0 AND b.parent_id NOT IN ('','0')
                AND NOT EXISTS (SELECT 1 FROM nodes p WHERE p.comment_id=b.parent_id)
                THEN 1 ELSE 0 END AS parent_missing
    FROM branch b JOIN ({fragment}) U ON U.src_pk=b.id
    ORDER BY b.sort_path LIMIT %s OFFSET %s"""
