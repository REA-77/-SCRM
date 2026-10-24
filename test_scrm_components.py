import unittest
from unittest.mock import MagicMock, patch

import reply_service_client
from scrm_components.feishu import FeishuBitableClient, cell_money, cell_text, cell_timestamp
from scrm_components.comment_queries import comment_filters, comment_fragment, comments_union, contents_union
from scrm_components.comment_service import CommentReadService, with_platform
from scrm_components.platforms import COMMENT_TABLES_ENABLED, PLATFORM_MAP, PLATFORM_OF


class PlatformConfigurationTests(unittest.TestCase):
    def test_enabled_tables_have_complete_platform_mapping(self):
        self.assertTrue(COMMENT_TABLES_ENABLED)
        self.assertEqual(set(COMMENT_TABLES_ENABLED), set(PLATFORM_OF))
        for table in COMMENT_TABLES_ENABLED:
            self.assertIn(table, PLATFORM_MAP)
            self.assertTrue(PLATFORM_MAP[table]["platform"])
            self.assertTrue(PLATFORM_MAP[table]["ctable"])


class CommentQueryTests(unittest.TestCase):
    def test_unions_include_every_enabled_platform(self):
        comments = comments_union()
        contents = contents_union()
        for table in COMMENT_TABLES_ENABLED:
            self.assertIn(f"'{table}' AS src_table", comments)
            self.assertIn(f"'{table}' AS src_table", contents)
        self.assertIn("src_pk", comment_fragment(COMMENT_TABLES_ENABLED[0]))

    def test_filters_are_parameterized_and_time_is_injectable(self):
        where, params = comment_filters(
            {"platform": "抖音", "q": "空调", "status": "reply_ready", "days": "2"},
            now=1_700_000_000,
        )
        self.assertIn("U.src_table IN (%s)", where)
        self.assertIn("U.content LIKE %s", where)
        self.assertIn("U.reply IS NOT NULL", where)
        self.assertEqual(params, ["douyin_aweme_comment", "%空调%", 1_699_827_200])

    def test_is_self_filter_is_parameterized_per_table(self):
        tables = COMMENT_TABLES_ENABLED
        where, params = comment_filters({"is_self": "1"})
        self.assertEqual(where.count("<=>"), len(tables))
        self.assertEqual(params, list(tables))
        where0, params0 = comment_filters({"is_self": "0"})
        self.assertEqual(where0.count("NOT "), len(tables))
        self.assertEqual(params0, list(tables))
        # 未传该参数时不得影响既有查询
        where_none, params_none = comment_filters({})
        self.assertNotIn("<=>", where_none)
        self.assertEqual(params_none, [])

    def test_enrich_contents_flags_self_replies(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"cid": "v1", "title": "视频", "url": "u", "author_nickname": "作者"}]
        rows = CommentReadService(cursor).enrich_contents([
            {"src_table": "douyin_aweme_comment", "content_id": "v1", "nickname": " 作者 "},
            {"src_table": "douyin_aweme_comment", "content_id": "v1", "nickname": "路人"},
            {"src_table": "douyin_aweme_comment", "content_id": "v1", "nickname": None},
            {"src_table": "douyin_aweme_comment", "content_id": "缺失", "nickname": "作者"},
        ])
        self.assertTrue(rows[0]["is_self"])
        self.assertFalse(rows[1]["is_self"])
        self.assertFalse(rows[2]["is_self"])
        self.assertFalse(rows[3]["is_self"])

    def test_detail_service_owns_context_assembly(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"src_pk": 9, "src_table": "douyin_aweme_comment", "content_id": "v1"},
            {"title": "视频", "url": "https://example.test", "upname": "作者"},
        ]
        cursor.fetchall.return_value = [{"src_pk": 8, "content": "同视频评论"}]
        row = CommentReadService(cursor).load_detail("douyin_aweme_comment", 9)
        self.assertEqual(row["platform"], "抖音")
        self.assertEqual(row["content_info"]["title"], "视频")
        self.assertEqual(row["thread"][0]["src_pk"], 8)
        self.assertEqual(cursor.execute.call_count, 3)
        self.assertEqual(with_platform([{"src_table": "douyin_aweme_comment"}])[0]["platform"], "抖音")


class FeishuClientTests(unittest.TestCase):
    def test_value_normalizers(self):
        self.assertEqual(cell_text([{"text": "A"}, {"name": "B"}]), "A,B")
        self.assertEqual(cell_timestamp(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(str(cell_money("￥1,234.50")), "1234.50")

    def test_list_records_follows_page_token(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"code": 0, "tenant_access_token": "token"}
        first = MagicMock(json=lambda: {"code": 0, "data": {"items": [{"record_id": "1"}], "has_more": True, "page_token": "next"}})
        second = MagicMock(json=lambda: {"code": 0, "data": {"items": [{"record_id": "2"}], "has_more": False}})
        for response in (first, second):
            response.raise_for_status = MagicMock()
        session.get.side_effect = [first, second]
        session.post.return_value.raise_for_status = MagicMock()
        rows = FeishuBitableClient("id", "secret", session=session).list_records("app", "table")
        self.assertEqual([row["record_id"] for row in rows], ["1", "2"])
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["page_token"], "next")


class ReplyDecisionContractTests(unittest.TestCase):
    def test_analysis_and_reply_requires_structured_analysis(self):
        valid = {
            "available": True, "action": "reply", "reply": "可以看看", "analysis": {
                "is_negative": False, "has_purchase_intent": True,
                "comment_type": "求推荐", "confidence": 0.9,
            },
        }
        with patch.object(reply_service_client, "generate_reply", return_value=valid):
            result = reply_service_client.evaluate_comment({"comment": "想买"}, "batch")
        self.assertTrue(result["available"])
        self.assertTrue(result["analysis"]["has_purchase_intent"])

    def test_missing_analysis_is_not_silently_written_as_normal(self):
        with patch.object(reply_service_client, "generate_reply", return_value={"available": True, "action": "no_reply"}):
            result = reply_service_client.evaluate_comment({"comment": "测试"}, "batch")
        self.assertFalse(result["available"])
        self.assertIn("分析结果", result["error"])

    def test_water_tank_purchase_question_gets_safe_reply_when_service_returns_no_reply(self):
        decision = {
            "available": True,
            "action": "no_reply",
            "analysis": {
                "is_negative": False,
                "has_purchase_intent": True,
                "comment_type": "产品咨询",
            },
        }
        with patch.object(reply_service_client, "generate_reply", return_value=decision):
            result = reply_service_client.evaluate_comment(
                {"comment": "石头P30 Pro的污水箱容量大吗？会不会拖一半就要倒污水了"}, "batch")
        self.assertEqual(result["action"], "reply")
        self.assertIn("用水档位", result["reply"])
        self.assertEqual(result["fallback_reason"], "water_tank_pre_purchase_question")

    def test_unrelated_no_reply_remains_unchanged(self):
        decision = {
            "available": True,
            "action": "no_reply",
            "analysis": {
                "is_negative": False,
                "has_purchase_intent": True,
                "comment_type": "产品咨询",
            },
        }
        with patch.object(reply_service_client, "generate_reply", return_value=decision):
            result = reply_service_client.evaluate_comment({"comment": "这机器是什么颜色？"}, "batch")
        self.assertEqual(result["action"], "no_reply")


if __name__ == "__main__":
    unittest.main()
