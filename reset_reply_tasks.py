from db import get_conn

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("UPDATE comment_auto_task SET status='pending', error_message=NULL WHERE id IN (11, 12)")
    conn.commit()
print('已重置任务 11、12')
