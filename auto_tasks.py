"""Task inspection and explicitly requested single-task execution."""
import time
from flask import Blueprint, jsonify, render_template, request
from db import get_conn
from task_queue import TaskQueue

bp = Blueprint('auto_tasks', __name__)
SOURCES = {'B站': 'bilibili_video_comment', '抖音': 'douyin_aweme_comment',
           '小红书': 'xhs_note_comment', '知乎': 'zhihu_comment'}


def execute_task(task):
    status, error = 'success', None
    try:
        from comment_auto_worker import call_bilibili, call_web_reply, run_douyin_reply
        if task['platform'] == 'B站':
            call_bilibili(task)
        elif task['platform'] == '抖音':
            results = run_douyin_reply([task])
            match = next((r for r in results if str(r.get('replyPlanId')) == str(task['comment_id'])), {})
            if match.get('status') == 'uncertain':
                status = 'uncertain'
                error = match.get('error') or '发表结果待人工核对，请勿直接重试'
            elif match.get('status') != 'replied':
                raise RuntimeError(match.get('error') or match.get('status') or '未确认回复结果')
        else:
            call_web_reply(task)
    except Exception as exc:
        status, error = 'failed', str(exc)[:1000]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if status == 'success':
                cur.execute(f"UPDATE `{task['src_table']}` SET handled=1 WHERE id=%s", (task['src_pk'],))
            verify_after = int(time.time()) + 1800 if status == 'uncertain' else None
            cur.execute('''UPDATE comment_auto_task
                           SET status=%s,error_message=%s,finished_ts=%s,
                               claim_owner=NULL,claimed_ts=NULL,
                               verify_after_ts=COALESCE(%s, verify_after_ts)
                           WHERE id=%s''',
                        (status, error, int(time.time()), verify_after, task['id']))
        conn.commit()

def _load_task(cur, task_id):
    cur.execute('SELECT * FROM comment_auto_task WHERE id=%s FOR UPDATE', (task_id,))
    task = cur.fetchone()
    if not task:
        return None, ('任务不存在', 404)
    if task['status'] not in ('queued', 'pending', 'failed', 'claimed'):
        return None, ('仅待执行、失败或已领取任务可执行', 409)
    if SOURCES.get(task['platform']) != task['src_table'] or (
        task['action_type'] != 'reply' and
        not (task['platform'] in ('B站', '抖音') and task['action_type'] == 'delete')
    ):
        return None, ('暂不支持该平台或操作', 400)
    cur.execute(f"SELECT * FROM `{task['src_table']}` WHERE id=%s", (task['src_pk'],))
    comment = cur.fetchone()
    if not comment or comment.get('handled') or comment.get('no_action'):
        return None, ('评论不存在、已处理或无需操作', 409)
    reply = str(comment.get('reply_suggestion') or '').strip()
    if task['action_type'] == 'reply' and (not comment.get('reviewed') or not reply or reply.startswith('⚠ 分析失败')):
        return None, ('请先审核评论并填写推荐回复', 409)
    merged = {**comment, **task, 'reply_suggestion': reply}
    # Keep the task ID separate from the source comment's ID.
    if task['platform'] == 'B站':
        cur.execute('SELECT bvid,title,nickname AS video_account FROM bilibili_video WHERE video_id=%s LIMIT 1', (task['content_id'],))
        merged.update(cur.fetchone() or {})
    elif task['platform'] == '抖音':
        cur.execute('SELECT title FROM douyin_aweme WHERE aweme_id=%s LIMIT 1', (task['content_id'],))
        merged.update(cur.fetchone() or {})
    elif task['platform'] == '知乎':
        cur.execute('SELECT content_url FROM zhihu_content WHERE content_id=%s LIMIT 1', (task['content_id'],))
        merged['reply_url'] = comment.get('content_url') or (cur.fetchone() or {}).get('content_url')
    return merged, None


def _finish_no_action(cur, task_id, task, reason):
    """A disappeared comment is not a transport failure and is never retried."""
    if task:
        cur.execute(f"UPDATE `{task['src_table']}` SET no_action=1 WHERE id=%s", (task['src_pk'],))
    cur.execute("""UPDATE comment_auto_task
                   SET status='no_action', error_message=%s, finished_ts=%s,
                       claim_owner=NULL, claimed_ts=NULL
                   WHERE id=%s""", (reason[:1000], int(time.time()), task_id))


def _run_queued_task(task_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            task, error = _load_task(cur, task_id)
            if error:
                if error[0].startswith('评论不存在'):
                    _finish_no_action(cur, task_id, None, error[0])
                else:
                    cur.execute("""UPDATE comment_auto_task
                                   SET status='failed',error_message=%s,finished_ts=%s,
                                       claim_owner=NULL,claimed_ts=NULL
                                   WHERE id=%s AND status IN ('queued','pending','failed','claimed')""",
                                (error[0], int(time.time()), task_id))
            else:
                from comment_auto_worker import verify_comment_exists
                if verify_comment_exists(task) is False:
                    _finish_no_action(cur, task_id, task, '发送前确认：目标评论已删除或不可见')
                    task = None
                else:
                    cur.execute("""UPDATE comment_auto_task
                                   SET status='sending',started_ts=%s,finished_ts=NULL,error_message=NULL
                                   WHERE id=%s AND status IN ('queued','pending','failed','claimed')""",
                                (int(time.time()), task_id))
        conn.commit()
    if task:
        execute_task(task)


_TASK_QUEUE = TaskQueue(_run_queued_task, capacity=100)


def launch_task(task_id):
    """Manual execution; one platform write runs at a time."""
    return _TASK_QUEUE.submit(task_id) in ('queued', 'duplicate')


def launch_claimed_task(task_id):
    """Called only by the database dispatcher after atomically claiming work."""
    return _TASK_QUEUE.submit(task_id)


def verify_uncertain_task(task_id, retry_after=1800):
    """Run one reconciliation probe; unknown results are never resent."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM comment_auto_task WHERE id=%s AND status='uncertain' FOR UPDATE", (task_id,))
            task = cur.fetchone()
            if not task:
                return 'skipped'
            cur.execute(f"SELECT * FROM `{task['src_table']}` WHERE id=%s", (task['src_pk'],))
            comment = cur.fetchone()
            if not comment:
                _finish_no_action(cur, task_id, task, '核验时评论已不存在')
                conn.commit()
                return 'no_action'
    from comment_auto_worker import verify_reply
    outcome = verify_reply({**task, **(comment or {})})
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if outcome == 'confirmed':
                cur.execute(f"UPDATE `{task['src_table']}` SET handled=1 WHERE id=%s", (task['src_pk'],))
                cur.execute("""UPDATE comment_auto_task SET status='success', finished_ts=%s,
                               error_message='定时核验确认已发送', verify_after_ts=NULL WHERE id=%s""", (now, task_id))
            elif outcome == 'missing':
                _finish_no_action(cur, task_id, task, '核验时目标评论已删除或不可见')
            else:
                cur.execute("""UPDATE comment_auto_task
                               SET verify_attempts=verify_attempts+1, verify_after_ts=%s,
                                   error_message='发送结果待核验；已安排下一次回扫，不会自动重发'
                               WHERE id=%s AND status='uncertain'""", (now + retry_after, task_id))
        conn.commit()
    return outcome


@bp.get('/scrm/auto-tasks')
def page():
    return render_template('scrm/auto_tasks.html', active='auto_tasks')


@bp.get('/scrm/api/auto-tasks')
def listing():
    try:
        page_num = max(1, int(request.args.get('page', 1)))
    except ValueError:
        return jsonify(error='页码无效'), 400
    clauses, params = [], []
    for field in ('platform', 'status'):
        value = request.args.get(field)
        if value:
            clauses.append(f'{field}=%s'); params.append(value)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM comment_auto_task' + where, params)
            total = cur.fetchone()['n']
            cur.execute('SELECT * FROM comment_auto_task' + where + ' ORDER BY id DESC LIMIT 30 OFFSET %s', params + [(page_num-1)*30])
            rows = cur.fetchall()
    return jsonify(rows=rows, total=total, page=page_num)


@bp.post('/scrm/api/auto-tasks/<int:task_id>/retry')
def retry(task_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _, error = _load_task(cur, task_id)
            if error:
                return jsonify(error=error[0]), error[1]
            cur.execute("""UPDATE comment_auto_task
                           SET status='queued',started_ts=NULL,finished_ts=NULL,
                               claim_owner=NULL,claimed_ts=NULL,error_message=NULL
                           WHERE id=%s""", (task_id,))
        conn.commit()
    return jsonify(ok=True, id=task_id, status='queued', queued=True), 202


@bp.post('/scrm/api/auto-tasks/<int:task_id>/resolve')
def resolve_uncertain(task_id):
    """Operator reconciliation: this never causes another platform write."""
    body = request.get_json(silent=True) or {}
    outcome = body.get('outcome')
    if outcome not in ('sent', 'no_action'):
        return jsonify(error='仅支持 sent 或 no_action'), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM comment_auto_task WHERE id=%s AND status='uncertain' FOR UPDATE", (task_id,))
            task = cur.fetchone()
            if not task:
                return jsonify(error='仅结果待核对任务可核销'), 409
            now = int(time.time())
            if outcome == 'sent':
                cur.execute(f"UPDATE `{task['src_table']}` SET handled=1 WHERE id=%s", (task['src_pk'],))
                cur.execute("""UPDATE comment_auto_task SET status='success', finished_ts=%s,
                               verify_after_ts=NULL,error_message='人工核销：已确认发送' WHERE id=%s""", (now, task_id))
            else:
                _finish_no_action(cur, task_id, task, '人工核销：不再处理')
        conn.commit()
    return jsonify(ok=True, id=task_id, status='success' if outcome == 'sent' else 'no_action')
