import unittest

from reply_platforms import get_adapter


class PlatformAdapterTests(unittest.TestCase):
    def test_supported_platforms_have_complete_contract(self):
        for platform in ('B站', '抖音', '小红书', '知乎'):
            adapter = get_adapter(platform)
            self.assertTrue(callable(adapter.validate_account))
            self.assertTrue(callable(adapter.preflight))
            self.assertTrue(callable(adapter.send_reply))
            self.assertTrue(callable(adapter.verify_reply))
            self.assertTrue(callable(adapter.classify_error))
            self.assertGreater(adapter.profile.min_interval_seconds, 0)

    def test_douyin_profile_is_stricter_than_bilibili(self):
        self.assertGreaterEqual(get_adapter('抖音').profile.min_interval_seconds,
                                get_adapter('B站').profile.min_interval_seconds)


if __name__ == '__main__':
    unittest.main()
