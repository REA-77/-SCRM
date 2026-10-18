import unittest
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from contextlib import asynccontextmanager
from xhs_reply import reply_api, cookie_from_state, classify, _matches_payload


STATE = {"cookies": [{"domain": ".xiaohongshu.com", "name": "a1", "value": "test"}]}


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_client_signed_request_and_unwrapped_success(self):
        from xhs_direct import XhsApiClient
        response = SimpleNamespace(status_code=200, json=lambda: {
            "success": True, "code": 0, "data": {"comment": {"id": "new-comment"}}})
        with patch.object(XhsApiClient, "_request", new_callable=AsyncMock, return_value=response) as transport:
            result = await reply_api("note123", "测试回复", storage_state=STATE,
                user_agent="Mozilla/5.0 Chrome/130.0.0.0", target_comment_id="comment123")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["comment"]["id"], "new-comment")
        transport.assert_awaited_once()
        args, kwargs = transport.call_args
        self.assertEqual(args, ("post", "https://edith.xiaohongshu.com/api/sns/web/v1/comment/post"))
        self.assertEqual(json.loads(kwargs["data"])["target_comment_id"], "comment123")
        self.assertTrue(any(k.lower() == "x-s" for k in kwargs["headers"]))

    async def test_bundled_response_failures(self):
        from xhs_direct import XhsApiClient
        for payload, http_status, expected in [
            ({"success": False, "code": -100}, 200, "failed"),
            ({"success": False, "code": 123}, 200, "failed"),
            ({"data": {}}, 200, "uncertain"),
            ({"success": True, "data": {}}, 503, "uncertain"),
        ]:
            with self.subTest(payload=payload, http_status=http_status):
                response = SimpleNamespace(status_code=http_status, json=lambda: payload)
                with patch.object(XhsApiClient, "_request", new_callable=AsyncMock, return_value=response) as transport:
                    result = await reply_api("n", "hello", storage_state=STATE,
                        user_agent="Mozilla/5.0 Chrome/130.0.0.0")
                self.assertEqual(result["status"], expected)
                transport.assert_awaited_once()

    async def test_api_forwards_target(self):
        captured = {}

        class Client:
            def __init__(self, cookie, **kwargs):
                captured["cookie"] = cookie

            @asynccontextmanager
            async def session_scope(self):
                yield

            async def post_comment(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, "code": 0}

        result = await reply_api("note123", "hello", storage_state=STATE,
                                 user_agent="test", target_comment_id="comment123", client_factory=Client)
        self.assertEqual(result["status"], "success")
        self.assertEqual(captured["target_comment_id"], "comment123")
        self.assertEqual(captured["cookie"], "a1=test")

    async def test_send_timeout_uncertain(self):
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            @asynccontextmanager
            async def session_scope(self):
                yield

            async def post_comment(self, **kwargs):
                raise TimeoutError()

        result = await reply_api("n", "hello", storage_state=STATE,
                                 user_agent="test", client_factory=Client)
        self.assertEqual(result["status"], "uncertain")

    def test_cookie_domain_isolation(self):
        state = {"cookies": STATE["cookies"] + [
            {"domain": "evilxiaohongshu.com", "name": "secret", "value": "hidden"}]}
        self.assertEqual(cookie_from_state(state), "a1=test")
        with self.assertRaises(ValueError):
            cookie_from_state({"cookies": []})

    def test_business_status(self):
        self.assertEqual(classify({"success": False, "code": 0})["status"], "failed")
        self.assertEqual(classify({"success": True, "code": 123})["status"], "failed")
        self.assertEqual(classify({"data": {}})["status"], "uncertain")

    def test_wrong_target_blocked(self):
        payload = {"note_id": "n", "content": "hello", "target_comment_id": "c"}
        self.assertTrue(_matches_payload(payload, "n", "hello", "c"))
        self.assertFalse(_matches_payload(payload, "n", "hello", "other"))
        self.assertFalse(_matches_payload(payload, "other", "hello", "c"))
        self.assertFalse(_matches_payload(payload, "n", "changed", "c"))
        self.assertFalse(_matches_payload(payload, "n", "hello", ""))


if __name__ == "__main__":
    unittest.main()
