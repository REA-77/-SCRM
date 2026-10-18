import asyncio
import json
import unittest
from unittest.mock import patch

import account_cookie_sync as sync


class CookieSyncTests(unittest.TestCase):
    def test_only_platform_domains_and_unexpired_cookies(self):
        result = sync.select_cookie("抖音", [
            {"domain": ".douyin.com", "name": "sessionid", "value": "valid"},
            {"domain": "www.douyin.com", "name": "other", "value": "ok"},
            {"domain": "evildouyin.com", "name": "sessionid", "value": "wrong"},
            {"domain": "zhihu.com", "name": "z_c0", "value": "wrong"},
            {"domain": "douyin.com", "name": "expired", "value": "old", "expires": 1},
        ])
        self.assertEqual(result, "sessionid=valid; other=ok")

    def test_anonymous_cookies_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.select_cookie("小红书", [{"domain": "xiaohongshu.com", "name": "a1", "value": "anonymous"}])

    @patch.object(sync, "bit_call")
    def test_window_on_later_page(self, call):
        call.side_effect = [{"list": [{"id": "first", "seq": 1}]},
                            {"list": [{"id": "target", "seq": 174}]}]
        self.assertEqual(sync.resolve_window("174"), "target")
        self.assertEqual(call.call_args.args[1]["page"], 1)

    @patch.object(sync, "bit_call", return_value={"list": []})
    def test_unknown_window_not_guessed(self, call):
        with self.assertRaises(RuntimeError):
            sync.resolve_window("unknown")

    def test_cdp_events_do_not_hide_command_response(self):
        class Socket:
            def __init__(self):
                self.messages = iter([
                    {"method": "event"},
                    {"id": 1, "result": {"cookies": [{"domain": "zhihu.com", "name": "z_c0", "value": "test"}]}},
                    {"id": 2, "result": {"userAgent": "test-agent"}},
                ])
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, message): pass
            async def recv(self): return json.dumps(next(self.messages))
        with patch.object(sync.websockets, "connect", return_value=Socket()):
            self.assertEqual(asyncio.run(sync.read_browser("ws://test", "知乎")),
                             ("z_c0=test", "test-agent"))


if __name__ == "__main__":
    unittest.main()
