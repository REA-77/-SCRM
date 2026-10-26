"""Conservative, persistent send policy for every platform adapter."""
import time

from db import get_conn


def _config():
    try:
        from config import (REPLY_MIN_INTERVAL_SECONDS, REPLY_HOURLY_LIMIT,
                            REPLY_DAILY_LIMIT, REPLY_ACTIVE_HOURS,
                            REPLY_RISK_COOLDOWN_SECONDS)
        return (max(30, int(REPLY_MIN_INTERVAL_SECONDS)), max(1, int(REPLY_HOURLY_LIMIT)),
                max(1, int(REPLY_DAILY_LIMIT)), str(REPLY_ACTIVE_HOURS),
                max(300, int(REPLY_RISK_COOLDOWN_SECONDS)))
    except ImportError:
        return 120, 8, 30, '09:00-22:00', 3600


def _in_active_hours(value, now):
    try:
        start, end = value.split('-', 1)
        sh, sm = (int(v) for v in start.split(':'))
        eh, em = (int(v) for v in end.split(':'))
        minute = time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min
        lower, upper = sh * 60 + sm, eh * 60 + em
        return lower <= minute < upper if lower <= upper else minute >= lower or minute < upper
    except Exception:
        return True


def evaluate_send(task, now=None):
    """Return an allow/block decision without mutating the task itself."""
    now = int(now or time.time())
    minimum, hourly, daily, active_hours, _ = _config()
    from reply_platforms import get_adapter
    profile = get_adapter(task.get('platform')).profile
    # Platform profiles can only tighten the global policy, never relax it.
    minimum = max(minimum, profile.min_interval_seconds)
    hourly = min(hourly, profile.hourly_limit)
    daily = min(daily, profile.daily_limit)
    # human_review is an operator-facing routing label, not a transport veto.
    # High-risk comments may still be reviewed/marked as “转人工” while an
    # already approved reply is generated and sent through the normal task
    # pipeline.  Actual account cooldowns remain enforced below.
    if not _in_active_hours(active_hours, now):
        return {'allowed': False, 'reason': '不在回复活跃时段', 'until': now + 300}
    account = str(task.get('account_nickname') or '').strip()
    platform = str(task.get('platform') or '').strip()
    if not account or not platform:
        return {'allowed': False, 'reason': '缺少发送账号或平台归属', 'until': None}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT cooldown_until FROM reply_account_risk
                           WHERE platform=%s AND account_nickname=%s""", (platform, account))
            state = cur.fetchone()
            if state and state.get('cooldown_until') and int(state['cooldown_until']) > now:
                return {'allowed': False, 'reason': '账号处于风险冷却', 'until': int(state['cooldown_until'])}
            cur.execute("""SELECT MAX(sent_ts) AS latest,
                                  SUM(sent_ts >= %s) AS hourly_count,
                                  SUM(sent_ts >= %s) AS daily_count
                           FROM reply_send_ledger WHERE platform=%s AND account_nickname=%s""",
                        (now - 3600, now - 86400, platform, account))
            usage = cur.fetchone() or {}
    latest = int(usage.get('latest') or 0)
    if latest and latest + minimum > now:
        return {'allowed': False, 'reason': '未达到账号最小发送间隔', 'until': latest + minimum}
    if int(usage.get('hourly_count') or 0) >= hourly:
        return {'allowed': False, 'reason': '已达到账号小时发送上限', 'until': now + 300}
    if int(usage.get('daily_count') or 0) >= daily:
        return {'allowed': False, 'reason': '已达到账号每日发送上限', 'until': now + 3600}
    return {'allowed': True}


def record_success(task, evidence=None, now=None):
    now = int(now or time.time())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reply_send_ledger
                           (task_id,platform,account_nickname,sent_ts,evidence)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (task['id'], task['platform'], task.get('account_nickname') or '', now,
                         str(evidence or '')[:2000]))
        conn.commit()


def record_failure(task, failure_class, now=None):
    """Only platform-risk/auth failures cool an account; network failures do not."""
    if failure_class not in ('risk_control', 'auth'):
        return
    now = int(now or time.time())
    _, _, _, _, cooldown = _config()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reply_account_risk
                           (platform,account_nickname,cooldown_until,reason,updated_ts)
                           VALUES (%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE cooldown_until=VALUES(cooldown_until),
                               reason=VALUES(reason),updated_ts=VALUES(updated_ts)""",
                        (task['platform'], task.get('account_nickname') or '', now + cooldown,
                         failure_class, now))
        conn.commit()
