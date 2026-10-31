"""Task inspection and explicitly requested single-task execution."""
import json
from pathlib import Path
import time
from flask import Blueprint, jsonify, render_template, request
from db import get_conn
from task_queue import TaskQueue

bp = Blueprint('auto_tasks', __name__)
SOURCES = {'B站': 'bilibili_video_comment', '抖音': 'douyin_aweme_comment',
           '小红书': 'xhs_note_comment', '知乎': 'zhihu_comment'}


def _audit(cur, task_id, event_type, detail='', actor='system'):
    """Append-only task timeline.  Details must never include cookies or keys."""
    cur.execute("""INSERT INTO comment_auto_task_audit
                   (task_id,event_type,actor,detail,created_ts)
                   VALUES (%s,%s,%s,%s,%s)""",
                (task_id, event_type, actor, str(detail or '')[:1000], int(time.time())))
    cur.execute("UPDATE comment_auto_task SET last_event=%s WHERE id=%s", (event_type, task_id))


def _failure_class(error):
    text = str(error or '').lower()
    # Local browser/process failures are not account authentication failures.
    # In particular, Playwright spawn/permission errors may contain the word
    # "cookie" in the profile path and must never create an account cooldown.
    if any(token in text for token in ('spawn eperm', 'browsertype.launch', 'permission denied', '拒绝访问', 'assertion failed', 'uv_handle_closing')):
        return 'platform'
    # An HTTP 403 without a Douyin business response is an edge rejection.  It
    # is commonly caused by a bad request identity/signature and is not enough
    # evidence to cool down an account.  Only explicit platform risk signals
    # below may create a persistent account cooldown.
    if 'http_403' in text:
        return 'request_rejected'
    if any(token in text for token in ('cookie', '登录', 'session')):
        return 'auth'
    if any(token in text for token in ('403', '429', '验证码', '风控')):
        return 'risk_control'
    if any(token in text for token in ('timeout', 'timed out', '超时', '连接', 'network')):
        return 'network'
    return 'platform'


def execute_task(task):
    status, error = 'success', None
    evidence = None
    try:
        from reply_platforms import get_adapter
        result = get_adapter(task['platform']).send_reply(task)
        if result.get('status') == 'uncertain':
            status = 'uncertain'
            error = result.get('error') or '发表结果待人工核对，请勿直接重试'
        elif result.get('status') != 'replied':
            raise RuntimeError(result.get('error') or result.get('status') or '未确认回复结果')
        else:
            evidence = result.get('evidence')
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
                               verify_after_ts=COALESCE(%s, verify_after_ts),
                               failure_class=%s,
                               sent_evidence=COALESCE(%s, sent_evidence)
                           WHERE id=%s''',
                        (status, error, int(time.time()), verify_after,
                         _failure_class(error) if status == 'failed' else None,
                         json.dumps(evidence, ensure_ascii=False)[:2000] if evidence else None,
                         task['id']))
            _audit(cur, task['id'], 'send_' + status, error or '平台已确认接收')
        conn.commit()
    if status == 'success':
        from reply_risk_guard import record_success
        record_success(task, evidence)
    elif status == 'failed':
        from reply_risk_guard import record_failure
        record_failure(task, _failure_class(error))

def _load_task(cur, task_id):
    cur.execute('SELECT * FROM comment_auto_task WHERE id=%s FOR UPDATE', (task_id,))
    task = cur.fetchone()
    if not task:
        return None, ('任务不存在', 404)
    if task['status'] not in ('queued', 'pending', 'failed', 'claimed', 'blocked'):
        return None, ('仅待执行、失败或已领取任务可执行', 409)
    if SOURCES.get(task['platform']) != task['src_table'] or (
        task['action_type'] != 'reply' and
        not (task['platform'] in ('B站', '抖音', '小红书') and task['action_type'] == 'delete')
    ):
        return None, ('暂不支持该平台或操作', 400)
    cur.execute(f"SELECT * FROM `{task['src_table']}` WHERE id=%s", (task['src_pk'],))
    comment = cur.fetchone()
    if not comment or comment.get('handled') or comment.get('no_action'):
        return None, ('评论不存在、已处理或无需操作', 409)
    reply = str(comment.get('reply_suggestion') or '').strip()
    if task['action_type'] == 'reply' and (not (comment.get('reviewed') or comment.get('reply_manual_override')) or not reply or reply.startswith('⚠ 分析失败')):
        return None, ('请先审核评论并填写推荐回复', 409)
    # A task always sends the reviewed snapshot, not a draft subsequently
    # edited in the comments screen.
    merged = {**comment, **task, 'reply_suggestion': task.get('reply_snapshot') or reply}
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
    _audit(cur, task_id, 'no_action', reason)


def _release_reply_ledger(task):
    """Append an operator release event so a failed send can be retried.

    The browser sender uses an append-only JSONL ledger.  Removing its old
    send_attempted/sent_unconfirmed line would be unsafe while another worker
    is reading it, so append the supported ``released`` tombstone instead.
    """
    if not task or task.get('platform') != '抖音':
        return False
    work_title = str(task.get('title') or '').strip()
    username = str(task.get('nickname') or '').strip()
    comment_text = str(task.get('content') or '').strip()
    if not (work_title and username and comment_text):
        return False
    ledger = Path(__file__).resolve().parent / 'douyin-creator-tools' / 'comments-output' / 'reply-ledger.jsonl'
    ledger.parent.mkdir(parents=True, exist_ok=True)
    event = {
        'version': 1,
        'recordedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'workTitle': work_title,
        'username': username,
        'commentText': comment_text,
        'status': 'released',
        'source': 'operator_manual_not_sent'
    }
    with ledger.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        handle.flush()
    return True


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
                    _audit(cur, task_id, 'blocked', error[0])
            else:
                from reply_platforms import get_adapter
                adapter = get_adapter(task['platform'])
                valid, reason = adapter.validate_account(task)
                if not valid:
                    cur.execute("""UPDATE comment_auto_task SET status='blocked',error_message=%s,
                                   claim_owner=NULL,claimed_ts=NULL WHERE id=%s""", (reason, task_id))
                    _audit(cur, task_id, 'account_blocked', reason)
                    task = None
                elif adapter.preflight(task) is False:
                    _finish_no_action(cur, task_id, task, '发送前确认：目标评论已删除或不可见')
                    task = None
                else:
                    from reply_risk_guard import evaluate_send
                    decision = evaluate_send(task)
                    if not decision.get('allowed'):
                        cur.execute("""UPDATE comment_auto_task
                                       SET status='blocked',next_attempt_ts=%s,error_message=%s,
                                           claim_owner=NULL,claimed_ts=NULL
                                       WHERE id=%s""",
                                    (decision.get('until'), decision.get('reason'), task_id))
                        _audit(cur, task_id, 'risk_blocked', decision.get('reason'))
                        task = None
                    else:
                        cur.execute("""UPDATE comment_auto_task
                                       SET status='sending',started_ts=%s,finished_ts=NULL,error_message=NULL
                                       WHERE id=%s AND status IN ('queued','pending','failed','claimed')""",
                                    (int(time.time()), task_id))
                        _audit(cur, task_id, 'sending', '已通过发送前检查与统一风控闸门')
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
    from reply_platforms import get_adapter
    outcome = get_adapter(task['platform']).verify_reply({**task, **(comment or {})})
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            if outcome == 'confirmed':
                cur.execute(f"UPDATE `{task['src_table']}` SET handled=1 WHERE id=%s", (task['src_pk'],))
                cur.execute("""UPDATE comment_auto_task SET status='success', finished_ts=%s,
                               error_message='定时核验确认已发送', verify_after_ts=NULL WHERE id=%s""", (now, task_id))
                _audit(cur, task_id, 'verified_success', '定时核验确认已发送')
            elif outcome == 'missing':
                _finish_no_action(cur, task_id, task, '核验时目标评论已删除或不可见')
            else:
                cur.execute("""UPDATE comment_auto_task
                               SET verify_attempts=verify_attempts+1, verify_after_ts=%s,
                                   error_message='发送结果待核验；已安排下一次回扫，不会自动重发'
                               WHERE id=%s AND status='uncertain'""", (now + retry_after, task_id))
                _audit(cur, task_id, 'verify_pending', '核验未得到可靠结论')
        conn.commit()
    return outcome


@bp.get('/scrm/auto-tasks')
def page():
    return render_template('scrm/auto_tasks.html', active='auto_tasks')


@bp.get('/scrm/reply-inbox')
def inbox_page():
    return render_template('scrm/reply_inbox.html', active='reply_inbox')


@bp.get('/scrm/risk-center')
def risk_page():
    return render_template('scrm/reply_risk.html', active='reply_risk')


@bp.get('/scrm/reply-report')
def report_page():
    return render_template('scrm/reply_report.html', active='reply_report')


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


@bp.get('/scrm/api/reply-inbox')
def inbox_listing():
    platform, status = request.args.get('platform', ''), request.args.get('status', '')
    clauses, params = [], []
    if platform:
        clauses.append('platform=%s'); params.append(platform)
    if status:
        clauses.append('status=%s'); params.append(status)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM comment_auto_task' + where, params)
            total = cur.fetchone()['n']
            cur.execute('''SELECT id,platform,account_nickname,comment_id,content_id,action_type,status,
                                  reply_snapshot,error_message,failure_class,last_event,created_ts,finished_ts
                           FROM comment_auto_task''' + where + ' ORDER BY id DESC LIMIT 100', params)
            rows = cur.fetchall()
    return jsonify(rows=rows, total=total)


@bp.get('/scrm/api/reply-risk')
def risk_listing():
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT platform,account_nickname,cooldown_until,reason,updated_ts
                           FROM reply_account_risk ORDER BY cooldown_until DESC LIMIT 100""")
            accounts = cur.fetchall()
            cur.execute("""SELECT id,platform,account_nickname,status,error_message,next_attempt_ts,created_ts
                           FROM comment_auto_task WHERE status IN ('blocked','uncertain')
                           ORDER BY created_ts DESC LIMIT 100""")
            tasks = cur.fetchall()
    return jsonify(now=now, accounts=accounts, tasks=tasks)


@bp.get('/scrm/api/reply-report')
def report_data():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT platform,status,COUNT(*) AS n FROM comment_auto_task GROUP BY platform,status")
            task_counts = cur.fetchall()
            cur.execute("""SELECT platform,COUNT(*) AS sent,COUNT(DISTINCT account_nickname) AS accounts
                           FROM reply_send_ledger WHERE sent_ts >= UNIX_TIMESTAMP() - 86400 GROUP BY platform""")
            sent_counts = cur.fetchall()
            cur.execute("""SELECT event_type,COUNT(*) AS n FROM comment_auto_task_audit
                           WHERE created_ts >= UNIX_TIMESTAMP() - 86400 GROUP BY event_type""")
            events = cur.fetchall()
    return jsonify(tasks=task_counts, sent_24h=sent_counts, events_24h=events)


@bp.get('/scrm/api/auto-tasks/dispatcher')
def dispatcher_status():
    from reply_dispatcher import status
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM comment_auto_task GROUP BY status")
            counts = {row['status']: row['n'] for row in cur.fetchall()}
    return jsonify(**status(), counts=counts)


@bp.post('/scrm/api/auto-tasks/<int:task_id>/retry')
def retry(task_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _, error = _load_task(cur, task_id)
            if error:
                return jsonify(error=error[0]), error[1]
            cur.execute("""UPDATE comment_auto_task
                           SET status='queued',started_ts=NULL,finished_ts=NULL,
                               claim_owner=NULL,claimed_ts=NULL,next_attempt_ts=NULL,error_message=NULL
                           WHERE id=%s""", (task_id,))
            _audit(cur, task_id, 'requeued', '人工重新排队')
        conn.commit()
    return jsonify(ok=True, id=task_id, status='queued', queued=True), 202


@bp.post('/scrm/api/auto-tasks/<int:task_id>/resolve')
def resolve_uncertain(task_id):
    """Operator reconciliation: this never causes another platform write."""
    body = request.get_json(silent=True) or {}
    outcome = body.get('outcome')
    if outcome not in ('sent', 'no_action', 'not_sent'):
        return jsonify(error='仅支持 sent、not_sent 或 no_action'), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM comment_auto_task WHERE id=%s AND status IN ('uncertain','failed') FOR UPDATE", (task_id,))
            task = cur.fetchone()
            if not task:
                return jsonify(error='仅失败或结果待核对任务可人工核销'), 409
            # The task table stores only IDs.  Resolve the original comment
            # snapshot before releasing the browser ledger, otherwise the
            # release event has no work title/user/text and is silently
            # skipped, leaving the old ``skipped_reply_ledger`` lock active.
            if task.get('src_table') in SOURCES.values():
                cur.execute(f"SELECT * FROM `{task['src_table']}` WHERE id=%s", (task['src_pk'],))
                task.update(cur.fetchone() or {})
            if task.get('platform') == '抖音':
                cur.execute("SELECT title FROM douyin_aweme WHERE aweme_id=%s LIMIT 1", (task.get('content_id'),))
                task.update(cur.fetchone() or {})
            now = int(time.time())
            if outcome == 'sent':
                cur.execute(f"UPDATE `{task['src_table']}` SET handled=1 WHERE id=%s", (task['src_pk'],))
                cur.execute("""UPDATE comment_auto_task SET status='success', finished_ts=%s,
                               verify_after_ts=NULL,error_message='人工核销：已确认发送' WHERE id=%s""", (now, task_id))
                _audit(cur, task_id, 'manual_confirmed', '人工核销：已确认发送', actor='operator')
            elif outcome == 'no_action':
                _finish_no_action(cur, task_id, task, '人工核销：不再处理')
            else:
                released = _release_reply_ledger(task)
                cur.execute("""UPDATE comment_auto_task
                               SET status='failed', finished_ts=%s,
                                   verify_after_ts=NULL, claim_owner=NULL,
                                   claimed_ts=NULL,
                                   error_message='人工标记：未发送成功，可重新排队'
                               WHERE id=%s""", (now, task_id))
                _audit(cur, task_id, 'manual_not_sent',
                       '人工标记未发送成功，已释放回复台账' if released else
                       '人工标记未发送成功，但未找到可释放的回复台账', actor='operator')
        conn.commit()
    return jsonify(ok=True, id=task_id,
                   status='success' if outcome == 'sent' else 'no_action' if outcome == 'no_action' else 'failed')
