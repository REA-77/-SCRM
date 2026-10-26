"""Persistent, database-backed dispatcher for approved comment replies.

The dispatcher owns *scheduling*, while ``auto_tasks`` owns the actual write.
Keeping the claim in MySQL means a restart never loses queued work and two
processes cannot send the same reply at the same time.
"""
import logging
import os
import socket
import threading
import time

from db import get_conn

logger = logging.getLogger("reply_dispatcher")
_STOP = threading.Event()
_THREAD = None
WORKER_ID = "%s:%s" % (socket.gethostname(), os.getpid())


def _settings():
    # Conservative defaults: a task must have been explicitly approved before
    # it reaches ``queued``.  The dispatcher never creates tasks itself.
    try:
        from config import REPLY_DISPATCH_INTERVAL_SECONDS, REPLY_VERIFY_DELAY_SECONDS
        return max(2, int(REPLY_DISPATCH_INTERVAL_SECONDS)), max(300, int(REPLY_VERIFY_DELAY_SECONDS))
    except ImportError:
        return 10, 1800


def recover_expired_claims(now=None):
    """Make work claimed by a dead process runnable again, without retrying writes."""
    now = int(now or time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE comment_auto_task
                           SET status='queued', claim_owner=NULL, claimed_ts=NULL,
                               error_message='执行进程中断，已重新排队'
                           WHERE status='claimed' AND claimed_ts < %s""", (now - 300,))
            count = cur.rowcount
            cur.execute("""UPDATE comment_auto_task SET status='queued', error_message=NULL
                           WHERE status='blocked' AND next_attempt_ts IS NOT NULL
                             AND next_attempt_ts <= %s""", (now,))
            count += cur.rowcount
        conn.commit()
    return count


def claim_queued(limit=20, now=None):
    """Atomically claim a small batch.  Legacy ``pending`` rows stay untouched.

    This avoids turning a deployment/restart into permission to send old work;
    an operator must explicitly requeue any historical task first.
    """
    now = int(now or time.time())
    claimed = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            # ``FOR UPDATE`` makes this work on MySQL versions without SKIP LOCKED.
            cur.execute("""SELECT id FROM comment_auto_task
                           WHERE status='queued'
                             AND (next_attempt_ts IS NULL OR next_attempt_ts <= %s)
                           ORDER BY created_ts, id LIMIT %s FOR UPDATE""", (now, limit))
            for row in cur.fetchall():
                task_id = row['id']
                cur.execute("""UPDATE comment_auto_task
                               SET status='claimed', claim_owner=%s, claimed_ts=%s,
                                   attempt_count=attempt_count+1, error_message=NULL
                               WHERE id=%s AND status='queued'""",
                            (WORKER_ID, now, task_id))
                if cur.rowcount:
                    claimed.append(task_id)
        conn.commit()
    return claimed


def release_claim(task_id, reason):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE comment_auto_task
                           SET status='queued', claim_owner=NULL, claimed_ts=NULL,
                               error_message=%s
                           WHERE id=%s AND status='claimed' AND claim_owner=%s""",
                        (str(reason)[:1000], task_id, WORKER_ID))
        conn.commit()


def dispatch_once():
    recover_expired_claims()
    from auto_tasks import launch_claimed_task
    dispatched = 0
    for task_id in claim_queued():
        result = launch_claimed_task(task_id)
        if result == 'queued':
            dispatched += 1
        elif result != 'duplicate':
            release_claim(task_id, '本地执行队列暂不可用，等待下次调度')
    return dispatched


def verify_uncertain_once():
    """Reconcile ambiguous writes; never turns an unknown result into a retry."""
    _, delay = _settings()
    now = int(time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM comment_auto_task
                           WHERE status='uncertain'
                             AND (verify_after_ts IS NULL OR verify_after_ts <= %s)
                           ORDER BY finished_ts, id LIMIT 20""", (now,))
            ids = [row['id'] for row in cur.fetchall()]
    if not ids:
        return 0
    from auto_tasks import verify_uncertain_task
    for task_id in ids:
        verify_uncertain_task(task_id, retry_after=delay)
    return len(ids)


def _loop():
    while not _STOP.is_set():
        try:
            dispatch_once()
            verify_uncertain_once()
        except Exception:
            logger.exception("reply dispatcher cycle failed")
        interval, _ = _settings()
        _STOP.wait(interval)


def start_dispatcher():
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name='reply-dispatcher', daemon=True)
    _THREAD.start()
    logger.info("评论回复常驻调度器已启动: %s", WORKER_ID)
    return _THREAD


def status():
    return {'worker_id': WORKER_ID, 'running': bool(_THREAD and _THREAD.is_alive())}
