import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

import account_cookie_health as health
import scrm


class CookieHealthTests(unittest.TestCase):
    def response(self, status=200, payload=None):
        result = MagicMock(status_code=status)
        result.json.return_value = payload
        return result

    def test_missing_and_missing_login_marker(self):
        self.assertEqual(health.check_cookie("B站", "")[0], "missing")
        self.assertEqual(health.check_cookie("B站", "bili_jct=test")[0], "expired")

    @patch.object(health.requests, "get")
    def test_bilibili_valid_and_expired(self, get):
        for payload, expected in [({"code": 0, "data": {"isLogin": True}}, "valid"),
                                  ({"code": -101, "data": {"isLogin": False}}, "expired"),
                                  ({"code": -352}, "unknown"),
                                  ({"code": 0, "data": {}}, "unknown")]:
            get.return_value = self.response(payload=payload)
            self.assertEqual(health.check_cookie("B站", "SESSDATA=test")[0], expected)

    @patch.object(health.requests, "get")
    def test_http_and_network_errors_are_not_expired(self, get):
        for status in [403, 429, 500, 302]:
            get.return_value = self.response(status)
            self.assertEqual(health.check_cookie("知乎", "z_c0=test")[0], "unknown")
        get.side_effect = health.requests.Timeout()
        self.assertEqual(health.check_cookie("知乎", "z_c0=test")[0], "unknown")

    @patch.object(health.requests, "get")
    def test_auth_error_is_expired(self, get):
        get.return_value = self.response(401)
        self.assertEqual(health.check_cookie("知乎", "z_c0=test")[0], "expired")

    @patch("xhs_direct.client.XhsApiClient")
    def test_xhs_uses_unwrapped_profile(self, client):
        from unittest.mock import AsyncMock
        client.return_value.self_info = AsyncMock(return_value={"user_id": "test", "guest": False})
        self.assertEqual(health.check_cookie("小红书", "web_session=test")[0], "valid")
        client.return_value.self_info = AsyncMock(return_value={"user_id": "test", "guest": True})
        self.assertEqual(health.check_cookie("小红书", "web_session=test")[0], "expired")

    def test_unsupported_platform_not_assumed_valid(self):
        self.assertEqual(health.check_cookie("视频号", "some=test")[0], "unknown")

    @patch.object(health.requests, "get")
    @patch.object(health.requests, "post")
    def test_douyin_history_endpoint_status(self, post, get):
        post.return_value = self.response(payload={"isok": True, "data": {"a_bogus": "signed"}})
        for payload, expected in [({"status_code": 0}, "valid"), ({"status_code": 8}, "expired")]:
            get.return_value = self.response(payload=payload)
            self.assertEqual(health.check_cookie("抖音", "sessionid=test")[0], expected)
        self.assertIn("max_cursor=0", post.call_args.kwargs["json"]["query_params"])

    @patch.object(health.requests, "post")
    def test_kuaishou_graphql_result(self, post):
        post.return_value = self.response(payload={"data": {"visionProfileUserList": {"result": 1}}})
        self.assertEqual(health.check_cookie("快手", "kuaishou.server.web_st=test")[0], "valid")
        post.return_value = self.response(payload={"data": {"visionProfileUserList": {"result": 0}}})
        self.assertEqual(health.check_cookie("快手", "kuaishou.server.web_st=test")[0], "expired")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["operationName"], "visionProfileUserList")
        self.assertEqual(body["variables"], {"ftype": 1})

    @patch.object(health.requests, "get")
    def test_weibo_and_reddit_login_shapes(self, get):
        get.return_value = self.response(payload={"data": {"login": True}})
        self.assertEqual(health.check_cookie("微博", "SUB=test")[0], "valid")
        get.return_value = self.response(payload={"data": {"login": False}})
        self.assertEqual(health.check_cookie("微博", "SUB=test")[0], "expired")
        get.return_value = self.response(payload={"kind": "t2", "data": {"id": "abc"}})
        self.assertEqual(health.check_cookie("Reddit", "reddit_session=test")[0], "valid")
        get.return_value = self.response(payload={"kind": "Listing", "data": {}})
        self.assertEqual(health.check_cookie("Reddit", "reddit_session=test")[0], "expired")

    @patch.object(health, "get_conn")
    @patch.object(health, "check_cookie", return_value=("expired", "login expired"))
    def test_old_result_cannot_overwrite_refreshed_cookie(self, check, connect):
        cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{"cookie": "old", "credentials": None},
                                      {"fingerprint": health.fingerprint("new")}]
        cursor.rowcount = 0
        result = health.check_account("B站", "test")
        self.assertEqual(result["cookie_status"], "unknown")
        update = cursor.execute.call_args_list[1]
        self.assertIn("SHA2", update.args[0])
        self.assertEqual(update.args[1][-1], health.fingerprint("old"))

    def test_api_validation(self):
        app = Flask(__name__)
        app.register_blueprint(scrm.bp)
        client = app.test_client()
        self.assertEqual(client.post("/scrm/api/accounts/check-cookie", json=[]).status_code, 400)
        self.assertEqual(client.get("/scrm/api/accounts?cookie_status=invalid").status_code, 400)
        with patch.object(health, "check_account", return_value={"cookie_status": "expired", "message": "未登录"}):
            result = client.post("/scrm/api/accounts/check-cookie", json={"platform": "B站", "nickname": "test"})
            self.assertEqual(result.json["cookie_status"], "expired")

    @patch.object(scrm, "get_conn")
    def test_filter_applies_to_count_and_page(self, connect):
        cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"c": 0}
        cursor.fetchall.return_value = []
        app = Flask(__name__)
        app.register_blueprint(scrm.bp)
        response = app.test_client().get("/scrm/api/accounts?cookie_status=expired")
        self.assertEqual(response.status_code, 200)
        count, page = cursor.execute.call_args_list[:2]
        self.assertIn("A.cookie_status = %s", count.args[0])
        self.assertEqual(count.args[1], ["expired"])
        self.assertIn("A2.cookie_status = %s", page.args[0])
        self.assertEqual(page.args[1][0], "expired")


if __name__ == "__main__":
    unittest.main()
