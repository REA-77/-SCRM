"""Platform adapter boundary for comment replies.

Adapters deliberately wrap the proven existing send functions instead of
reimplementing platform protocols.  The dispatcher and risk guard only depend
on this small contract, so future platforms can be added without branching
the task state machine.
"""
from dataclasses import dataclass

from comment_auto_worker import (call_bilibili, call_web_reply, run_douyin_reply,
                                 verify_comment_exists, verify_reply)


@dataclass(frozen=True)
class RateLimitProfile:
    min_interval_seconds: int
    hourly_limit: int
    daily_limit: int


class ReplyAdapter:
    platform = ''
    profile = RateLimitProfile(120, 8, 30)

    def validate_account(self, task):
        account = str(task.get('account_nickname') or '').strip()
        return (bool(account), '缺少发送账号归属' if not account else '')

    def preflight(self, task):
        return verify_comment_exists(task)

    def verify_reply(self, task):
        return verify_reply(task)

    def classify_error(self, error):
        text = str(error or '').lower()
        if 'http_403' in text:
            return 'request_rejected'
        if any(item in text for item in ('cookie', '登录', 'session')):
            return 'auth'
        if any(item in text for item in ('403', '429', '验证码', '风控')):
            return 'risk_control'
        if any(item in text for item in ('timeout', '连接', 'network')):
            return 'network'
        return 'platform'


class BilibiliAdapter(ReplyAdapter):
    platform = 'B站'
    profile = RateLimitProfile(120, 8, 30)

    def send_reply(self, task):
        return {'status': 'replied', 'evidence': call_bilibili(task)}


class DouyinAdapter(ReplyAdapter):
    platform = '抖音'
    profile = RateLimitProfile(180, 6, 24)

    def send_reply(self, task):
        results = run_douyin_reply([task])
        result = next((row for row in results if str(row.get('replyPlanId')) == str(task.get('comment_id'))), {})
        status = result.get('status')
        if status == 'replied':
            return {'status': 'replied', 'evidence': result}
        if status == 'uncertain':
            return {'status': 'uncertain', 'error': result.get('error') or '发表结果待核验'}
        return {'status': 'failed', 'error': result.get('error') or status or '未确认回复结果'}


class BrowserReplyAdapter(ReplyAdapter):
    """Existing browser/API implementations for 小红书 and 知乎."""
    def send_reply(self, task):
        return {'status': 'replied', 'evidence': call_web_reply(task)}


class XhsAdapter(BrowserReplyAdapter):
    platform = '小红书'
    profile = RateLimitProfile(180, 6, 24)


class ZhihuAdapter(BrowserReplyAdapter):
    platform = '知乎'
    profile = RateLimitProfile(240, 4, 16)


_ADAPTERS = {item.platform: item for item in (BilibiliAdapter(), DouyinAdapter(), XhsAdapter(), ZhihuAdapter())}


def get_adapter(platform):
    adapter = _ADAPTERS.get(platform)
    if not adapter:
        raise ValueError(f'未接入回复适配器的平台：{platform}')
    return adapter
