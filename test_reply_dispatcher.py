import unittest
from unittest.mock import MagicMock, patch

import reply_dispatcher


class DispatcherSafetyTests(unittest.TestCase):
    def test_claim_query_only_selects_new_queue_state(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        with patch.object(reply_dispatcher, 'get_conn', return_value=conn):
            self.assertEqual(reply_dispatcher.claim_queued(), [])
        sql = cur.execute.call_args.args[0]
        self.assertIn("status='queued'", sql)
        self.assertNotIn("'pending'", sql)

    def test_full_local_queue_releases_database_claim(self):
        with patch.object(reply_dispatcher, 'recover_expired_claims'), \
             patch.object(reply_dispatcher, 'claim_queued', return_value=[7]), \
             patch('auto_tasks.launch_claimed_task', return_value='full'), \
             patch.object(reply_dispatcher, 'release_claim') as release:
            self.assertEqual(reply_dispatcher.dispatch_once(), 0)
        release.assert_called_once_with(7, '本地执行队列暂不可用，等待下次调度')


if __name__ == '__main__':
    unittest.main()
