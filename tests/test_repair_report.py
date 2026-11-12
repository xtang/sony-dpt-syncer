import sqlite3
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import ArrayObject,DictionaryObject,FloatObject,NameObject,TextStringObject
from app.pdf_notes import ingest_pdf
from app.service import Service
from scripts.repair_report import build_report


class RepairReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.service=Service(self.root/'data',self.root/'auth')
        self.service.save_settings({'sync_folders':['Document']})
        self.doc=self.service.upsert_document(dict(entry_path='Document/test.pdf',entry_type='document',entry_id='test'),True)
        writer=PdfWriter();page=writer.add_blank_page(width=595,height=842)
        references=ArrayObject()
        for identifier,y in [('a',100),('b',170)]:
            annotation=DictionaryObject({NameObject('/Subtype'):NameObject('/Ink'),NameObject('/NM'):TextStringObject(identifier),
                NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (100,y,112,y+12)]),
                NameObject('/InkList'):ArrayObject([ArrayObject([FloatObject(v) for v in (100,y,112,y+12)])])})
            references.append(writer._add_object(annotation))
        page[NameObject('/Annots')]=references
        pdf=self.root/'fixture.pdf';writer.write(pdf)
        self.ids=ingest_pdf(self.service.store,self.service.data,self.doc,pdf)['notes']
        self.assertEqual(len(self.ids),2)
        for identifier,ref in zip(self.ids,['8','問題2 第8题']):
            self.service.store.execute("UPDATE notes SET transcript=? WHERE id=?",('とたん是什么意思？faint?',identifier))
            self.service.store.save_answer(identifier,dict(summary='legacy',explanation='legacy',exercise_reviews=[dict(question_ref=ref)]),'old')

    def tearDown(self):
        self.temporary.cleanup()

    def report(self):
        return build_report(self.service.store.path)

    def test_report_finds_all_three_signals_without_writing(self):
        job=self.service.store.job('analyze')
        self.service.store.update_job(job,status='running')
        def snapshot():
            with sqlite3.connect(self.service.store.path) as db: return db.serialize()
        before=snapshot()
        report=self.report()
        self.assertEqual(snapshot(),before)
        self.assertEqual(report['eligible_note_ids'],self.ids)
        self.assertEqual(report['summary']['candidate_notes'],2)
        self.assertEqual(report['model_call_estimate']['total_at_most_without_transport_retries'],4)
        self.assertEqual(report['summary']['duplicate_review_pairs'],1)
        self.assertEqual(report['summary']['similar_transcript_pairs'],1)
        for note in report['notes']:
            self.assertEqual(set(note['reasons']),{'legacy_crop_contains_other_strokes','duplicate_question_review','similar_neighbour_transcript'})
            self.assertEqual(len(note['evidence']['foreign_annotation_ids']),1)
        self.assertEqual(report['state_hash'],self.report()['state_hash'])
        self.assertEqual(self.service.store.one('SELECT status FROM jobs WHERE id=?',(job,))['status'],'running')

    def test_confirmed_ignored_new_and_out_of_scope_notes_are_separate(self):
        first,second=self.ids
        self.service.store.execute('UPDATE notes SET manual_transcript=1 WHERE id=?',(first,))
        self.service.store.execute('UPDATE notes SET ignored=1,scope_version=1 WHERE id=?',(second,))
        report=self.report()
        self.assertEqual(report['eligible_note_ids'],[])
        self.assertEqual(report['notes'][0]['skip_reasons'],['protected_confirmation'])
        self.assertEqual(set(report['notes'][1]['skip_reasons']),{'ignored','already_scoped'})
        self.service.store.execute('UPDATE documents SET tracked=0 WHERE id=?',(self.doc,))
        self.assertTrue(all('out_of_scope' in n['skip_reasons'] for n in self.report()['notes']))

    def test_imported_answers_and_stale_confirmations_are_protected(self):
        self.service.store.execute("UPDATE answers SET provenance='verified_import' WHERE note_id=?",(self.ids[0],))
        self.service.store.execute('UPDATE notes SET confirmation_stale=1 WHERE id=?',(self.ids[1],))
        report=self.report()
        self.assertEqual(report['eligible_note_ids'],[])
        self.assertEqual(report['summary']['candidate_skip_counts']['protected_confirmation'],2)

    def test_missing_or_changed_snapshot_blocks_repair(self):
        path=Path(self.service.store.one('SELECT pdf_path FROM snapshots')['pdf_path'])
        path.write_bytes(b'changed')
        report=self.report()
        self.assertEqual(report['summary']['inspection_failures'],2)
        self.assertEqual(report['eligible_note_ids'],[])
        self.assertTrue(all('inspection_failed' in n['skip_reasons'] for n in report['notes']))
        path.unlink()
        self.assertEqual(self.report()['summary']['inspection_failures'],2)

    def test_archives_and_historical_answers_are_not_current_duplicate_claims(self):
        old_hash=self.report()['state_hash']
        self.service.store.execute("UPDATE notes SET status='needs_review',transcript='' WHERE id=?",(self.ids[0],))
        report=self.report()
        self.assertNotEqual(report['state_hash'],old_hash)
        self.assertEqual(report['summary']['duplicate_review_pairs'],0)
        self.assertEqual(report['summary']['similar_transcript_pairs'],0)
        self.service.store.execute('UPDATE notes SET archived=1 WHERE id=?',(self.ids[0],))
        report=self.report()
        self.assertEqual(report['summary']['active_notes'],1)
        self.assertEqual(report['summary']['archived_notes_excluded'],1)

    def test_already_scoped_note_is_not_flagged_for_unfiltered_ui_crop_alone(self):
        self.service.store.execute("UPDATE notes SET scope_version=1,transcript='',status='needs_review'")
        report=self.report()
        self.assertEqual(report['summary']['candidate_notes'],0)
        self.assertEqual(report['eligible_note_ids'],[])


if __name__=='__main__':
    unittest.main()
