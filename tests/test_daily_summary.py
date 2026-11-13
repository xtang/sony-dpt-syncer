from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.daily_summary import summary_zone
from app.llm import ModelClient, ModelError
from app.service import Service
from app.store import Store


class DailySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(self.root/'data', self.root/'auth')
        self.service.save_settings({'sync_folders': ['Document'], 'model_name': 'test-model'})
        self.store = self.service.store
        self.daily = self.service.daily
        self.doc = self.service.upsert_document(dict(entry_path='Document/study.pdf', entry_id='demo', total_page=1))
        self.snapshot = self.store.execute('INSERT INTO snapshots(document_id,sha256,pdf_path,captured_at) VALUES(?,?,?,?)',
                                          (self.doc, 'hash', 'unused.pdf', '2020-04-01T00:00:00Z'))
        self.service.model.summarize_day = Mock(return_value='## 关键知识\n\n状态变化与习惯的区别。来源 #1。')

    def tearDown(self):
        self.service.stop_event.set()
        self.temp.cleanup()

    def note(self, modified='2020-04-01T02:00:00Z', trusted=True, ignored=False):
        n = self.store.one('SELECT COUNT(*) AS n FROM notes')['n']+1
        return self.store.execute('''INSERT INTO notes(document_id,page,group_key,content_hash,annotation_ids,
            snapshot_id,title,transcript,question,device_modified,time_trusted,first_seen,last_seen,ignored)
            VALUES(?,1,?,?,'[]',?,'变化与习惯','ようになる','什么意思？',?,?,?,?,?)''',
            (self.doc, str(n), str(n), self.snapshot, modified, int(trusted), '2020-04-01T00:00:00Z', '2020-04-01T00:00:00Z', int(ignored)))

    def test_settings_default_off_and_validation(self):
        settings = self.service.settings()
        self.assertFalse(settings['daily_summary_enabled'])
        self.assertEqual(settings['daily_summary_time'], '05:00')
        self.assertEqual(settings['daily_summary_timezone'], 'Asia/Shanghai')
        for values in [{'daily_summary_enabled': 1}, {'daily_summary_time': '25:00'},
                       {'daily_summary_time': '5:00'}, {'daily_summary_timezone': '../etc/passwd'},
                       {'daily_summary_timezone': 'Not/AZone'}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.service.save_settings(values)
        self.service.save_settings({'daily_summary_enabled': True, 'daily_summary_time': '06:30', 'daily_summary_timezone': 'UTC'})
        restarted = Service(self.service.data, self.root/'auth')
        self.assertEqual(restarted.settings()['daily_summary_time'], '06:30')

    def test_calendar_boundaries_views_and_exclusions(self):
        before = self.note('2020-03-31T15:59:59Z')
        start = self.note('2020-03-31T16:00:00Z')
        end = self.note('2020-04-01T16:00:00Z')
        viewed = self.note('1999-01-01T00:00:00Z', trusted=False)
        self.note(trusted=False)
        self.note(ignored=True)
        archived = self.note()
        self.store.execute('UPDATE notes SET archived=1 WHERE id=?', (archived,))
        self.store.execute('INSERT INTO note_views VALUES(?,?)', (viewed, '2020-04-01T15:59:59Z'))
        sources = self.daily.sources('2020-04-01', 'Asia/Shanghai')
        self.assertEqual([s['id'] for s in sources], [start, viewed])
        self.assertNotIn(before, [s['id'] for s in sources])
        self.assertNotIn(end, [s['id'] for s in sources])
        self.assertEqual(sources[1]['evidence'], ['网页查看'])
        self.service.save_settings({'sync_folders': ['Document/elsewhere']})
        self.assertEqual(self.daily.sources('2020-04-01', 'Asia/Shanghai'), [])

    def test_dst_day_uses_local_midnights(self):
        before = self.note('2020-03-08T04:59:59Z')
        start = self.note('2020-03-08T05:00:00Z')
        last = self.note('2020-03-09T03:59:59Z')
        end = self.note('2020-03-09T04:00:00Z')
        self.assertEqual([s['id'] for s in self.daily.sources('2020-03-08', 'America/New_York')], [start,last])

    def test_saved_summary_is_durable_and_idempotent(self):
        identifier = self.note()
        result = self.daily.generate('2020-04-01', 'Asia/Shanghai')
        self.daily.generate('2020-04-01', 'Asia/Shanghai')
        self.service.model.summarize_day.assert_called_once()
        row = self.daily.record('2020-04-01', 'Asia/Shanghai')
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['note_count'], 1)
        self.assertIn('#1', row['markdown'])
        self.assertEqual(json.loads(row['sources'])[0]['id'], identifier)
        self.store.execute("UPDATE notes SET transcript='changed' WHERE id=?", (identifier,))
        restored = Store(self.store.path).one('SELECT * FROM daily_summaries WHERE id=?', (result['summary_id'],))
        self.assertEqual(restored['markdown'], row['markdown'])
        self.assertEqual(json.loads(restored['sources'])[0]['transcript'], 'ようになる')
        self.assertEqual(len(self.service.export()['daily_summaries']), 1)

    def test_empty_day_never_calls_model(self):
        self.daily.generate('2020-04-01', 'UTC')
        self.service.model.summarize_day.assert_not_called()
        self.assertEqual(self.daily.record('2020-04-01', 'UTC')['status'], 'empty')

    def test_scheduler_time_off_switch_and_queue_dedup(self):
        after = datetime(2020,4,1,21,0,tzinfo=timezone.utc)  # April 2 at 05:00 in Shanghai.
        self.daily.tick(after)
        self.assertTrue(self.service.model_tasks.empty())
        self.service.save_settings({'daily_summary_enabled': True})
        self.daily.tick(datetime(2020,4,1,20,59,tzinfo=timezone.utc))
        self.assertTrue(self.service.model_tasks.empty())
        self.daily.tick(after)
        self.daily.tick(after)
        duplicate = self.service.enqueue('daily_summary', dict(day='2020-04-01', zone='Asia/Shanghai'))
        self.assertFalse(duplicate['queued'])
        self.assertEqual(self.service.model_tasks.qsize(), 1)
        _, kind, args, _ = self.service.model_tasks.get_nowait()
        self.assertEqual(kind, 'daily_summary')
        self.assertEqual(args['day'], '2020-04-01')
        self.service.save_settings({'daily_summary_enabled': False})
        self.daily.generate(**args)
        self.assertIsNone(self.daily.record('2020-04-01', 'Asia/Shanghai'))

    def test_failures_are_visible_retry_is_bounded_and_manual_retry_works(self):
        self.note()
        self.service.save_settings({'daily_summary_enabled': True, 'model_api_key': 'test-secret'})
        self.service.model.summarize_day.side_effect = RuntimeError('failure test-secret')
        with patch('app.daily_summary.now', return_value='2020-04-01T21:00:00+00:00'):
            with self.assertRaises(RuntimeError):
                self.daily.generate('2020-04-01', 'Asia/Shanghai')
        row = self.daily.record('2020-04-01', 'Asia/Shanghai')
        self.assertEqual(row['status'], 'error')
        self.assertNotIn('test-secret', row['error'])
        self.service.enqueue = Mock()
        self.daily.tick(datetime(2020,4,1,21,30,tzinfo=timezone.utc))
        self.service.enqueue.assert_not_called()
        self.daily.tick(datetime(2020,4,1,22,0,tzinfo=timezone.utc))
        self.service.enqueue.assert_called_once()
        self.service.enqueue.reset_mock()
        self.store.execute('UPDATE daily_summaries SET attempts=3')
        self.daily.tick(datetime(2020,4,1,23,0,tzinfo=timezone.utc))
        self.service.enqueue.assert_not_called()
        self.service.model.summarize_day.side_effect = None
        self.daily.generate('2020-04-01', 'Asia/Shanghai')
        self.assertEqual(self.daily.record('2020-04-01', 'Asia/Shanghai')['status'], 'ready')

    def test_restart_recovers_interrupted_summary_without_duplicate_ready_record(self):
        self.note()
        self.daily.generate('2020-04-01', 'UTC')
        self.store.execute("UPDATE daily_summaries SET status='running'")
        Store(self.store.path)
        self.assertEqual(self.daily.record('2020-04-01', 'UTC')['status'], 'error')
        self.daily.generate('2020-04-01', 'UTC')
        self.assertEqual(len(self.store.query('SELECT id FROM daily_summaries')), 1)

    def test_concurrent_generation_claims_one_record(self):
        self.note()
        started, release = threading.Event(), threading.Event()
        def model(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            return '总结'
        self.service.model.summarize_day.side_effect = model
        errors = []
        def run():
            try:
                self.daily.generate('2020-04-01', 'UTC')
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.daily.generate('2020-04-01', 'UTC')
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(errors, [])
        self.service.model.summarize_day.assert_called_once()

    def test_read_and_revisit_evidence_without_marking_ignored_notes(self):
        note = self.note()
        self.store.save_answer(note, {'summary': '解释'}, 'test')
        answer = self.store.note(note)['answer_id']
        self.assertEqual(self.store.mark_answers_read(self.doc, [answer]), [answer])
        self.assertEqual(len(self.store.query('SELECT * FROM note_views')), 1)
        self.store.record_note_view(note)
        self.store.record_note_view(note)
        self.assertLessEqual(len(self.store.query('SELECT * FROM note_views')), 2)
        ignored = self.note(ignored=True)
        self.store.record_note_view(ignored)
        self.assertIsNone(self.store.one('SELECT * FROM note_views WHERE note_id=?', (ignored,)))

    def test_invalid_dates_are_rejected(self):
        for day in [None, '../settings', '2020-02-31', '20200401', '2999-01-01']:
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.daily.validate(day, 'UTC')


class SummaryModelTests(unittest.TestCase):
    def test_batches_include_every_note_and_merge(self):
        client = ModelClient(lambda: {})
        client.completion = Mock(return_value={'markdown': '## 摘要\n\n保留来源。'})
        sources = [dict(id=i, document='文档', page=1, title='笔记', evidence=['网页查看'], status='needs_review',
                        confirmation_stale=True, transcript='字'*1600, question='问'*800, anchor='文'*1600, answer={'explanation':'答'*6000}) for i in range(8)]
        result = client.summarize_day('2020-04-01', 'UTC', sources)
        self.assertTrue(result)
        seen = []
        for call in client.completion.call_args_list:
            data = json.loads(call.args[0][1]['content'])['material']
            if data and isinstance(data[0], dict):
                seen += [note['id'] for note in data]
                self.assertTrue(all(note['confirmation_stale'] for note in data))
        self.assertEqual(seen, list(range(8)))
        self.assertGreater(client.completion.call_count, 1)

    def test_rejects_empty_or_malformed_model_summary(self):
        client = ModelClient(lambda: {})
        source = dict(id=1, document='x', page=1, title='t', evidence=[], status='pending', confirmation_stale=False,
                      transcript='', question='', anchor='', answer=None)
        for result in [{}, {'markdown': ''}, {'markdown': []}]:
            client.completion = Mock(return_value=result)
            with self.assertRaises(ModelError):
                client.summarize_day('2020-04-01', 'UTC', [source])


if __name__ == '__main__':
    unittest.main()
