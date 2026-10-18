"""Regression tests; RUN_MEMORY_DB_TESTS=1 enables read-only MySQL checks."""
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import analyzer
import app as web
import db
import auto_tasks
from comment_paging import thread_roots_sql, thread_page_sql
from task_queue import TaskQueue


class BoundedQueueTests(unittest.TestCase):
    def test_capacity_serial_execution_and_duplicate(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def execute(task_id):
            calls.append(task_id)
            if task_id == 1:
                entered.set()
                release.wait(5)
            if task_id == 3:
                finished.set()

        runner = TaskQueue(execute, capacity=2)
        self.assertEqual(runner.submit(1), 'queued')
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(runner.submit(1), 'duplicate')
            self.assertEqual(runner.submit(2), 'queued')
            self.assertEqual(runner.submit(3), 'queued')
            self.assertEqual(runner.submit(4), 'full')
            self.assertEqual(calls, [1])
        finally:
            release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(calls, [1, 2, 3])

    def test_worker_continues_after_failure(self):
        finished = threading.Event()

        def execute(task_id):
            if task_id == 1:
                raise RuntimeError('simulated failure')
            finished.set()

        with patch('task_queue.logging.getLogger'):
            runner = TaskQueue(execute)
            runner.submit(1)
            runner.submit(2)
            self.assertTrue(finished.wait(2))


class AnalyzerMemoryTests(unittest.TestCase):
    def test_full_sweep_advances_without_rereading_failed_records(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {'last_id': 250}
        calls = []

        def page(table, limit, after_id, through_id):
            calls.append((limit, after_id, through_id))
            return [{'id': n} for n in range(after_id+1, min(after_id+limit, through_id)+1)]

        with patch.object(analyzer, 'get_conn', return_value=conn), patch.object(analyzer, 'get_unanalyzed', side_effect=page):
            ids = [r['id'] for r in analyzer.iter_unanalyzed('test', 0)]
        self.assertEqual(ids, list(range(1, 251)))
        self.assertEqual(calls, [(100, 0, 250), (100, 100, 250), (100, 200, 250), (100, 250, 250)])

    def test_fast_sweep_is_bounded(self):
        with patch.object(analyzer, 'get_unanalyzed', return_value=[{'id': 1}]) as fetch:
            self.assertEqual(list(analyzer.iter_unanalyzed('test', 20)), [{'id': 1}])
        fetch.assert_called_once_with('test', 20, 0, None)

    def test_age_filter_precedes_order_and_limit_is_capped(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        with patch.object(analyzer, 'get_conn', return_value=conn), patch.object(analyzer, 'ANALYZE_MAX_AGE_DAYS', 90):
            analyzer.get_unanalyzed('bilibili_video_comment', 100000, 10, 100)
        sql, params = cur.execute.call_args.args
        self.assertLess(sql.index('CASE WHEN'), sql.index('ORDER BY'))
        self.assertEqual(params[:2], (10, 100))
        self.assertEqual(params[-1], 500)

    def test_alert_buffer_is_flushed_in_small_batches(self):
        records = [{'id': i, 'content': 'test'} for i in range(65)]
        batches = []
        with patch.object(analyzer, 'COMMENT_TABLES', ['test']), \
             patch.object(analyzer, 'iter_unanalyzed', return_value=iter(records)), \
             patch.object(analyzer, 'call_deepseek', return_value=(True, False, 'reply')), \
             patch.object(analyzer, 'mark_analyzed'), \
             patch.object(analyzer, 'send_feishu_alert', side_effect=lambda a: batches.append(len(a))), \
             patch.object(analyzer.logger, 'info'):
            self.assertEqual(analyzer.scan_all(0), 65)
        self.assertEqual(batches, [30, 30, 5])


class TaskValidationTests(unittest.TestCase):
    def test_task_id_and_fresh_reply_survive_comment_merge(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {'id': 90, 'src_pk': 10, 'src_table': 'bilibili_video_comment',
             'platform': 'B站', 'action_type': 'reply', 'status': 'pending', 'content_id': 'v'},
            {'id': 10, 'reviewed': 1, 'reply_suggestion': 'fresh reply'},
            {'bvid': 'BV1', 'video_account': 'author'}]
        task, error = auto_tasks._load_task(cur, 90)
        self.assertIsNone(error)
        self.assertEqual(task['id'], 90)
        self.assertEqual(task['src_pk'], 10)
        self.assertEqual(task['reply_suggestion'], 'fresh reply')
        self.assertEqual(task['video_account'], 'author')

    def test_worker_revalidates_and_skips_handled_comment(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {'id': 90, 'src_pk': 10, 'src_table': 'bilibili_video_comment',
             'platform': 'B站', 'action_type': 'reply', 'status': 'pending'},
            {'id': 10, 'handled': 1}]
        with patch.object(auto_tasks, 'get_conn', return_value=conn), patch.object(auto_tasks, 'execute_task') as execute:
            auto_tasks._run_queued_task(90)
        execute.assert_not_called()
        self.assertIn("status='failed'", cur.execute.call_args.args[0])

    def test_retry_full_queue_does_not_mark_running(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        with patch.object(auto_tasks, 'get_conn', return_value=conn), \
             patch.object(auto_tasks, '_load_task', return_value=({'id': 90}, None)), \
             patch.object(auto_tasks._TASK_QUEUE, 'submit', return_value='full'):
            response = web.app.test_client().post('/scrm/api/auto-tasks/90/retry')
        self.assertEqual(response.status_code, 429)
        cur.execute.assert_not_called()


class ExportMemoryTests(unittest.TestCase):
    def setUp(self):
        self.slots = threading.BoundedSemaphore(2)
        self.patches = [patch.object(web, '_EXPORT_SLOTS', self.slots),
                        patch.object(web, 'valid_table', return_value=True),
                        patch.object(web, 'get_columns', return_value=[{'name': 'content'}])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_database_streaming_is_opt_in(self):
        with patch.object(db.pymysql, 'connect') as connect:
            db.get_conn(stream=True)
            self.assertIs(connect.call_args.kwargs['cursorclass'], db.pymysql.cursors.SSDictCursor)
            db.get_conn()
            self.assertIs(connect.call_args.kwargs['cursorclass'], db.DB_CONFIG['cursorclass'])

    def test_csv_stream_and_connection_cleanup(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchmany.side_effect = [[{'content': 'a,"b"\n中文'}, {'content': None}], []]
        with patch.object(web, 'get_conn', return_value=conn) as connect:
            response = web.app.test_client().get('/api/export/test')
            data = response.get_data(as_text=True)
            response.close()
        self.assertTrue(data.startswith('\ufeffcontent\r\n'))
        self.assertIn('"a,""b""\n中文"', data)
        connect.assert_called_once_with(stream=True)
        conn.__exit__.assert_called_once()
        self.assertTrue(self.slots.acquire(False))
        self.assertTrue(self.slots.acquire(False))
        self.assertFalse(self.slots.acquire(False))

    def test_cancel_before_database_read_releases_slot(self):
        with patch.object(web, 'get_conn') as connect:
            response = web.app.test_client().get('/api/export/test', buffered=False)
            response.close()
            connect.assert_not_called()
        self.assertTrue(self.slots.acquire(False))
        self.assertTrue(self.slots.acquire(False))

    def test_busy_export_returns_429(self):
        self.slots.acquire(False)
        self.slots.acquire(False)
        self.assertEqual(web.app.test_client().get('/api/export/test').status_code, 429)


@unittest.skipUnless(os.environ.get('RUN_MEMORY_DB_TESTS') == '1', 'read-only MySQL tests opt in')
class CommentSqlTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        self.cur = self.conn.cursor()

    def test_filtered_child_orphan_self_parent_and_cycle(self):
        edges = [(1, None), (2, 1), (3, 2), (4, None), (5, 6), (6, 5), (7, 6), (8, None)]
        sql = ' UNION ALL '.join(f"SELECT 't' AS src_table,{pk} AS src_pk,{parent if parent else 'NULL'} AS parent_pk" for pk, parent in edges)
        matches = ' UNION ALL '.join(f"SELECT 't' AS src_table,{pk} AS src_pk,{pk} AS ctime" for pk in (3, 4, 5, 6, 7, 8))
        self.cur.execute(thread_roots_sql(sql, matches) + 'SELECT root_pk FROM roots ORDER BY root_pk')
        self.assertEqual([r['root_pk'] for r in self.cur.fetchall()], [1, 4, 5, 8])

    def test_depth_first_reply_pagination_and_filter_flags(self):
        # IDs 2 and 4 are siblings, ID 3 is a nested reply. Include an orphan.
        data = [(1, '0', 1), (2, '1', 2), (3, '2', 4), (4, '1', 3), (5, '99', 5)]
        fixture = ' UNION ALL '.join(f"SELECT {pk} AS id,'{pk}' AS comment_id,'{parent}' AS parent_comment_id,'v' AS content_id,{ts} AS ctime" for pk, parent, ts in data)
        fragment = f"SELECT id AS src_pk, comment_id FROM ({fixture}) f"
        sql = thread_page_sql('fixture', 'content_id', 'ctime', fragment, 'U.src_pk=3')
        sql = sql.replace('FROM `fixture`', f'FROM ({fixture}) f')
        self.cur.execute(sql, ['v', 1, 3, 0])
        first = self.cur.fetchall()
        self.assertEqual([r['src_pk'] for r in first], [1, 2, 3])
        self.assertEqual([r['depth'] for r in first], [0, 1, 2])
        self.assertEqual([r['matches_filter'] for r in first], [0, 0, 1])
        self.cur.execute(sql, ['v', 1, 3, 3])
        self.assertEqual([r['src_pk'] for r in self.cur.fetchall()], [4])
        self.cur.execute(sql, ['v', 5, 3, 0])
        self.assertEqual(self.cur.fetchone()['parent_missing'], 1)

    def test_actual_readonly_endpoint(self):
        response = web.app.test_client().get('/scrm/api/comments?size=2')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertLessEqual(len(data['threads']), 2)
        self.assertLessEqual(len(data['rows']), 100)
        response = web.app.test_client().get('/scrm/api/comments?page=invalid')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
