import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from comment_auto_worker import _post_douyin_reply_direct, run_douyin_reply


IDENTITY = {"ua": "Mozilla/5.0 Chrome/130.0.0.0", "locale": "zh-CN", "width": 1440, "height": 900}
TASK = {"aweme_id": "7123456789012345678", "comment_id": "7123456789012345679", "reply_suggestion": "感谢关注"}


class DouyinReplySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_post_exception_is_not_failed_or_retried(self):
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def post_comment(self, *args, **kwargs):
                raise TimeoutError("read timed out")

        with patch("comment_auto_worker._load_douyin_commenter", return_value=Client):
            result = await _post_douyin_reply_direct(TASK, "sessionid=test", IDENTITY)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["failure_category"], "network")
        self.assertIn("勿直接重发", result["error"])

    async def test_explicit_risk_rejection_is_failed_with_category(self):
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def post_comment(self, *args, **kwargs):
                return False, "", "api_rejected:status_code=219 风控"

        with patch("comment_auto_worker._load_douyin_commenter", return_value=Client):
            result = await _post_douyin_reply_direct(TASK, "sessionid=test", IDENTITY)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "risk_control")

    async def test_invalid_input_stops_before_loading_client(self):
        with patch("comment_auto_worker._load_douyin_commenter") as factory:
            result = await _post_douyin_reply_direct({"aweme_id": "a", "comment_id": "c"}, "sessionid=test", IDENTITY)
        self.assertEqual(result["status"], "failed")
        factory.assert_not_called()


class DouyinReplyRoutingTests(unittest.TestCase):
    def test_api_rejection_does_not_fall_back_to_browser_send(self):
        direct = AsyncMock(return_value={
            "replyPlanId": TASK["comment_id"], "status": "failed",
            "error": "api_rejected:status_code=219",
        })
        with patch("comment_auto_worker.get_douyin_cookie", return_value={"cookies": "sessionid=test"}), \
             patch("comment_auto_worker.get_douyin_browser_identity", return_value=IDENTITY), \
             patch("comment_auto_worker._post_douyin_reply_direct", direct), \
             patch("comment_auto_worker._run_douyin_browser_plans") as browser:
            result = run_douyin_reply([TASK])
        self.assertEqual(result[0]["status"], "failed")
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
