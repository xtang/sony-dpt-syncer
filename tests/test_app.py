from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import contextlib
import tarfile
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

from app.bootstrap import relocate
from app.llm import ModelClient, ModelError, parse_json, validate_model_url
from app.pdf_notes import extract_annotations, ingest_pdf, pdf_date, trusted_date
from app.server import Handler
from app.service import Service, document_id, fingerprint
from app.web_auth import COOKIE, SESSION_SECONDS, WebAuth, create_access_file
from app.scope import in_folders, normalize_folders
from app.store import SCHEMA, Store
from app.env import read_env, load_env
from scripts.deploy import deployment_location, deploy, validate_target, validate_web_url, write_options
from scripts.package import application_sources, copy_pairing_credentials
from app.paths import DATABASE_NAME, LEGACY_DATABASE_NAME, database_path, default_data_dir
from dptrp1.dptrp1 import ResolveObjectFailed


def exercise_review(**changes):
    result=dict(question_ref='第1题',question='2 + 3 = ? A. 4 B. 5 C. 6',user_answer='A. 4',
                suggested_answer='B. 5',verdict='incorrect',basis='reasoning',confidence=.98,reference_quote='',
                explanation='2 加 3 等于 5；选项 A 的结果少算了 1。应选 B。',knowledge_points=['加法表示合并两个数量，2 + 3 = 5。'])
    result.update(changes)
    return result


def fixture_pdf(path, position=120, include=True, date=None, rotation=0, extra_note=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    if rotation:
        page.rotate(rotation)
    if include:
        ann = DictionaryObject({NameObject('/Type'):NameObject('/Annot'), NameObject('/Subtype'):NameObject('/Ink'),
            NameObject('/NM'):TextStringObject('stable-uuid'),
            NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (100,100,200,150)]),
            NameObject('/InkList'):ArrayObject([ArrayObject([FloatObject(v) for v in (100,100,position,140)])]),
            NameObject('/M'):TextStringObject(date or datetime.now(timezone.utc).strftime('D:%Y%m%d%H%M%SZ'))})
        page[NameObject('/Annots')] = ArrayObject([writer._add_object(ann)])
        if extra_note:
            second=DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Ink'),
                NameObject('/NM'):TextStringObject('second-uuid'),NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (300,500,400,550)]),
                NameObject('/InkList'):ArrayObject([ArrayObject([FloatObject(v) for v in (300,500,350,530)])])})
            page[NameObject('/Annots')].append(writer._add_object(second))
    with Path(path).open('wb') as file:
        writer.write(file)


def grouped_fixture(path, positions):
    writer=PdfWriter()
    page=writer.add_blank_page(width=595,height=842)
    refs=ArrayObject()
    for identifier,y in positions:
        annotation=DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Ink'),
            NameObject('/NM'):TextStringObject(identifier),
            NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (100,y,112,y+12)]),
            NameObject('/InkList'):ArrayObject([ArrayObject([FloatObject(v) for v in (100,y,112,y+12)])])})
        refs.append(writer._add_object(annotation))
    page[NameObject('/Annots')]=refs
    writer.write(path)


class FakePaper:
    """A device with stable document IDs and metadata-only native moves."""
    def __init__(self, entries, payload):
        self.entries={entry['entry_id']:dict(entry) for entry in entries}
        self.payloads={entry['entry_id']:payload for entry in entries}
        self.folders={'Document':dict(entry_path='Document',entry_type='folder',entry_id='root')}
        self.moves=[]
        self.created=[]

    def inventory(self):
        return [dict(value) for value in [*self.entries.values(),*self.folders.values()]]

    def list_document_info(self,path):
        for item in [*self.entries.values(),*self.folders.values()]:
            if item['entry_path']==path:return dict(item)
        raise ResolveObjectFailed(path)

    def new_folder(self,path):
        self.created.append(path)
        parts=path.split('/')
        for index in range(1,len(parts)+1):
            name='/'.join(parts[:index])
            self.folders.setdefault(name,dict(entry_path=name,entry_type='folder',entry_id='folder-'+name))

    def move_file_to_folder_by_id(self,file_id,folder_id,new_filename=None):
        folder=next(value for value in self.folders.values() if value['entry_id']==folder_id)
        entry=self.entries[file_id]
        entry['entry_path']=folder['entry_path']+'/'+(new_filename or Path(entry['entry_path']).name)
        self.moves.append((file_id,folder_id))
        response=requests.Response();response.status_code=200;response._content=b'{}'
        return response


class SyncerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = Service(self.root/'data', self.root/'auth')
        self.service.save_settings({'sync_folders':['Document']})
        self.entry = dict(entry_path='Document/test.pdf', entry_type='document', entry_id='tablet-id',
                          total_page=1, file_revision='r1', file_size='800', modified_date='2026-11-12T01:00:00Z')
        self.docid = self.service.upsert_document(self.entry, True)
        self.pdf = self.root/'note.pdf'
        fixture_pdf(self.pdf)
        self.web_auth = None
        self.allowed_hosts = set()

    def tearDown(self):
        self.service.stop_event.set()
        self.temporary.cleanup()

    def ingest(self):
        return ingest_pdf(self.service.store, self.service.data, self.docid, self.pdf)

    def request(self, path, method='GET', body=None, origin=None, host='localhost:4387', raw=None, filename=None, cookie=None):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(service=self.service, allowed_hosts=self.allowed_hosts, server_port=4387, web_auth=self.web_auth)
        handler.headers = Message()
        handler.headers['Host'] = host
        if origin:
            handler.headers['Origin'] = origin
        if cookie:
            handler.headers['Cookie'] = cookie
        payload = raw if raw is not None else json.dumps(body or {}).encode()
        handler.headers['Content-Type'] = 'application/pdf' if raw is not None else 'application/json'
        handler.headers['Content-Length'] = str(len(payload))
        if filename:
            handler.headers['X-Filename'] = filename
        handler.path = path
        handler.command = method
        handler.request_version = 'HTTP/1.1'
        handler.requestline = method+' '+path+' HTTP/1.1'
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        handler.connection = Mock()
        handler.dispatch(method=='POST')
        header, data = handler.wfile.getvalue().split(b'\r\n\r\n',1)
        self.last_headers = header.decode()
        return int(header.split()[1]), data

    def enable_web_auth(self):
        self.access_file=self.service.data/'web-access.json'
        create_access_file(self.access_file,'test-browser-password')
        self.web_auth=WebAuth(self.access_file)
        self.allowed_hosts.add('192.0.2.10')

    def login_cookie(self):
        status,_=self.request('/api/login','POST',{'password':'test-browser-password'})
        self.assertEqual(status,200)
        return next(line.split(': ',1)[1].split(';',1)[0] for line in self.last_headers.splitlines() if line.startswith('Set-Cookie:'))

    def test_lan_requires_login_for_notes_files_and_mutations(self):
        self.enable_web_auth()
        self.assertEqual(self.request('/api/health',host='192.0.2.10:4387')[0],200)
        status,body=self.request('/',host='192.0.2.10:4387')
        self.assertEqual(status,200)
        self.assertIn('访问口令'.encode(),body)
        for path in ['/api/state','/api/notes','/api/channels','/api/folders','/api/export.json','/api/backup.sqlite3','/app.js','/api/daily-summaries','/api/daily-summaries/1/download']:
            self.assertEqual(self.request(path,host='192.0.2.10:4387')[0],401)
        self.assertEqual(self.request('/api/sync','POST',host='192.0.2.10:4387')[0],401)
        self.assertEqual(self.request('/api/daily-summaries/generate','POST',{'date':'2020-04-01'},host='192.0.2.10:4387')[0],401)
        self.assertEqual(self.service.store.query('SELECT * FROM jobs'),[])

    def test_web_login_session_cookie_and_logout(self):
        self.enable_web_auth()
        self.assertEqual(self.request('/api/login','POST',{'password':'wrong'})[0],401)
        cookie=self.login_cookie()
        self.assertIn('HttpOnly',self.last_headers)
        self.assertIn('SameSite=Strict',self.last_headers)
        self.assertEqual(self.request('/api/state',cookie=cookie,host='192.0.2.10:4387')[0],200)
        self.assertNotIn('session_secret',self.request('/api/state',cookie=cookie)[1].decode())
        self.assertEqual(self.request('/api/settings','POST',{'auto_sync':False},cookie=cookie,origin='https://evil.example')[0],403)
        self.assertEqual(self.request('/api/logout','POST',cookie=cookie)[0],200)
        self.assertIn('Max-Age=0',self.last_headers)

    def test_web_session_tampering_expiration_and_password_reset(self):
        self.enable_web_auth()
        cookie=self.login_cookie()
        self.assertFalse(self.web_auth.valid(cookie+'0'))
        self.assertFalse(self.web_auth.valid(COOKIE+'=bad.☃'))
        with patch('app.web_auth.time.time',return_value=datetime.now(timezone.utc).timestamp()+SESSION_SECONDS+60):
            self.assertFalse(self.web_auth.valid(cookie))
        self.assertEqual(self.access_file.stat().st_mode&0o777,0o600)
        self.assertNotIn('test-browser-password',self.access_file.read_text())
        self.assertIsNone(create_access_file(self.access_file,'replacement-without-reset'))
        create_access_file(self.access_file,'new-password',reset=True)
        self.assertFalse(WebAuth(self.access_file).valid(cookie))

    def test_login_limits_repeated_incorrect_attempts(self):
        self.enable_web_auth()
        for _ in range(10):
            with self.assertRaises(PermissionError): self.web_auth.login('wrong','test-client')
        with self.assertRaisesRegex(PermissionError,'次数过多'):
            self.web_auth.login('test-browser-password','test-client')
        token=self.web_auth.login('test-browser-password','another-client')
        self.assertTrue(self.web_auth.valid(COOKIE+'='+token))

    def test_real_pdf_ingestion_is_idempotent_and_keeps_answer(self):
        first = self.ingest()
        self.assertEqual(len(first['notes']),1)
        noteid = first['notes'][0]
        self.service.store.save_answer(noteid, {'summary':'saved','explanation':'saved'}, 'test')
        second = self.ingest()
        self.assertEqual(second['notes'],[])
        self.assertEqual(self.service.store.stats()['notes'],1)
        self.assertEqual(self.service.store.note(noteid)['answer']['summary'],'saved')
        self.assertEqual(self.service.store.one('SELECT count(*) AS n FROM snapshots')['n'],1)

    def test_changed_handwriting_requires_reconfirmation_and_retains_history(self):
        identifier = self.ingest()['notes'][0]
        self.service.edit_note(identifier, {'transcript':'previous writing','question':'previous question'})
        self.service.store.save_answer(identifier, {'summary':'old','explanation':'old'}, 'test')
        fixture_pdf(self.pdf, position=145)
        self.assertEqual(self.ingest()['notes'],[identifier])
        note = self.service.store.note(identifier)
        self.assertTrue(note['manual_transcript'])
        self.assertTrue(note['confirmation_stale'])
        self.assertEqual(note['transcript'],'previous writing')
        self.assertEqual(note['status'],'needs_review')
        self.assertEqual(len(note['history']),1)
        self.assertEqual(note['answer']['summary'],'old')

    def test_added_stroke_keeps_identity_and_ignore_state(self):
        grouped_fixture(self.pdf, [('a',100)])
        identifier=self.ingest()['notes'][0]
        before=self.service.store.note(identifier)
        self.service.ignore_note(identifier,True)
        grouped_fixture(self.pdf, [('b',120),('a',100)])
        self.assertEqual(self.ingest()['notes'],[identifier])
        after=self.service.store.note(identifier)
        self.assertEqual(after['group_key'],before['group_key'])
        self.assertEqual(after['annotation_ids'],['a','b'])
        self.assertTrue(after['ignored'])
        self.assertEqual(after['revisions'][0]['note']['annotation_ids'],['a'])
        self.assertEqual(self.ingest()['notes'],[])
        self.assertEqual(len(self.service.store.note(identifier)['revisions']),1)

    def test_split_largest_fragment_keeps_identity_and_confirmed_text(self):
        grouped_fixture(self.pdf,[('a',100),('b',120),('c',140),('d',160)])
        identifier=self.ingest()['notes'][0]
        self.service.edit_note(identifier,dict(transcript='confirmed text',question='original question'))
        self.service.store.save_answer(identifier,dict(summary='old reply',explanation='details'),'test')
        grouped_fixture(self.pdf,[('a',100),('d',340),('b',300),('c',320)])
        self.ingest()
        retained=self.service.store.note(identifier)
        self.assertEqual(retained['annotation_ids'],['b','c','d'])
        self.assertEqual(retained['transcript'],'confirmed text')
        self.assertTrue(retained['confirmation_stale'])
        self.assertEqual(retained['answer']['summary'],'old reply')
        fragment=next(n for n in self.service.store.notes() if n['id']!=identifier)
        self.assertEqual(fragment['annotation_ids'],['a'])
        self.assertTrue(fragment['confirmation_stale'])
        self.assertEqual(fragment['transcript'],'')
        links=self.service.store.note(fragment['id'])['lineage']
        self.assertEqual((links[0]['parent_id'],links[0]['child_id'],links[0]['relation']),(identifier,fragment['id'],'split'))
        self.assertEqual(self.ingest()['notes'],[])
        self.service.model=Mock()
        self.assertTrue(self.service.analyze(identifier)['needs_review'])
        self.service.model.recognize.assert_not_called()
        self.service.model.answer.assert_not_called()
        self.service.edit_note(identifier,dict(transcript='confirmed new text'))
        self.service.model.answer.return_value=dict(summary='new reply',explanation='new details')
        self.service.analyze(identifier)
        current=self.service.store.note(identifier)
        self.assertFalse(current['confirmation_stale'])
        self.assertEqual(len(current['history']),2)
        self.assertEqual(current['revisions'][0]['note']['transcript'],'confirmed text')
        self.assertEqual(current['revisions'][0]['reason'],'manual_edit')

    def test_merge_keeps_largest_parent_and_archives_other_history(self):
        grouped_fixture(self.pdf,[('a',100),('b',120),('c',300)])
        self.ingest()
        first=next(n for n in self.service.store.notes() if 'a' in n['annotation_ids'])['id']
        second=next(n for n in self.service.store.notes() if 'c' in n['annotation_ids'])['id']
        self.service.edit_note(second,dict(transcript='other confirmed text'))
        self.service.store.save_answer(second,dict(summary='other old answer',explanation='details'),'test')
        grouped_fixture(self.pdf,[('c',300),('a',100),('b',120),('d',150),('e',180),('f',210),('g',240),('h',270)])
        self.assertEqual(self.ingest()['notes'],[first])
        winner=self.service.store.note(first);archived=self.service.store.note(second)
        self.assertFalse(winner['archived'])
        self.assertTrue(winner['confirmation_stale'])
        self.assertTrue(archived['archived'])
        self.assertEqual(archived['transcript'],'other confirmed text')
        self.assertEqual(archived['answer']['summary'],'other old answer')
        self.assertEqual(winner['lineage'][0]['relation'],'merge')
        self.assertEqual(winner['lineage'][0]['parent_id'],second)
        self.assertEqual(archived['revisions'][0]['reason'],'archived')
        self.assertEqual(self.ingest()['notes'],[])
        self.assertEqual(len(self.service.store.note(first)['lineage']),1)

    def test_failed_split_rolls_back_identity_revisions_and_snapshot(self):
        from app.pdf_notes import crop_group
        grouped_fixture(self.pdf,[('a',100),('b',120)])
        identifier=self.ingest()['notes'][0]
        before=self.service.store.note(identifier)
        grouped_fixture(self.pdf,[('a',100),('b',300)])
        calls=0
        def fail_second(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2: raise RuntimeError('renderer failed')
            return crop_group(*args,**kwargs)
        with patch('app.pdf_notes.crop_group',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'renderer failed'): self.ingest()
        self.assertEqual(self.service.store.note(identifier),before)
        self.assertEqual(self.service.store.document(self.docid)['snapshot_id'],before['snapshot_id'])
        self.assertEqual(len(self.service.store.notes()),1)
        self.assertEqual(self.service.store.query('SELECT * FROM note_links'),[])
        self.ingest()
        self.assertEqual(len(self.service.store.notes()),2)

    def test_restore_exact_archived_group_keeps_identity(self):
        grouped_fixture(self.pdf,[('a',100)])
        identifier=self.ingest()['notes'][0]
        grouped_fixture(self.pdf,[])
        self.ingest()
        self.assertTrue(self.service.store.note(identifier)['archived'])
        grouped_fixture(self.pdf,[('a',100)])
        self.ingest()
        self.assertFalse(self.service.store.note(identifier)['archived'])
        self.assertEqual(len(self.service.store.notes()),1)

    def test_ignored_split_fragments_stay_ignored(self):
        grouped_fixture(self.pdf,[('a',100),('b',120)])
        identifier=self.ingest()['notes'][0]
        self.service.ignore_note(identifier,True)
        grouped_fixture(self.pdf,[('a',100),('b',300)])
        self.ingest()
        notes=self.service.store.notes(include_ignored=True)
        self.assertEqual(len(notes),2)
        self.assertTrue(all(n['ignored'] for n in notes))

    def test_deleted_annotation_archives_instead_of_deleting(self):
        identifier = self.ingest()['notes'][0]
        fixture_pdf(self.pdf, include=False)
        self.ingest()
        self.assertEqual(self.service.store.stats()['notes'],0)
        self.assertTrue(self.service.store.note(identifier)['archived'])
        self.assertEqual(self.service.store.one('SELECT removed FROM annotations')['removed'],1)

    def test_rotated_pdf_crop_is_valid(self):
        from PIL import Image
        fixture_pdf(self.pdf, rotation=90)
        identifier = self.ingest()['notes'][0]
        with Image.open(self.service.store.note(identifier)['preview_path']) as image:
            self.assertGreater(image.width,20)
            self.assertGreater(image.height,20)

    def test_low_confidence_does_not_call_answer_or_grade(self):
        identifier = self.ingest()['notes'][0]
        self.service.model = Mock()
        self.service.model.recognize.return_value = dict(transcript='?',question='?',anchor='',title='unclear',kind='question',confidence=.5,tags=[])
        result = self.service.analyze(identifier)
        self.assertTrue(result['needs_review'])
        self.service.model.answer.assert_not_called()
        self.assertEqual(self.service.store.note(identifier)['status'],'needs_review')

    def test_manual_confirmation_skips_recognition_and_saves_answer(self):
        identifier = self.ingest()['notes'][0]
        self.service.edit_note(identifier, {'transcript':'区别是什么','question':'具体问题'})
        self.service.model = Mock()
        self.service.model.answer.return_value = {'summary':'answer','explanation':'details'}
        self.service.analyze(identifier)
        self.service.model.recognize.assert_not_called()
        self.assertEqual(self.service.store.note(identifier)['status'],'answered')
        self.assertEqual(self.service.store.stats()['unread'],1)

    def test_ignore_is_reversible_and_preserves_notes_answers_and_exports(self):
        identifier=self.ingest()['notes'][0]
        self.service.store.save_answer(identifier,dict(summary='Saved reply',explanation='Saved details'),'model')
        self.service.store.execute("UPDATE notes SET status='needs_review' WHERE id=?",(identifier,))
        preview=Path(self.service.store.note(identifier)['preview_path'])
        status,body=self.request(f'/api/notes/{identifier}/ignore','POST',{'ignored':True})
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['ignored'])
        self.assertTrue(preview.exists())
        self.assertEqual(self.service.store.notes(),[])
        self.assertEqual(self.service.store.stats()['needs_review'],0)
        self.assertEqual(self.service.store.stats()['unread'],0)
        self.assertEqual(self.service.store.stats()['ignored'],1)
        self.assertEqual(self.service.report()['next_steps'],[])
        self.assertEqual(json.loads(self.request('/api/notes?status=ignored')[1])[0]['id'],identifier)
        self.assertTrue(json.loads(self.request('/api/export.json')[1])['notes'][0]['ignored'])
        self.assertIn('已忽略'.encode(),self.request('/api/export.md')[1])
        self.assertEqual(self.request(f'/api/notes/{identifier}/analyze','POST')[0],400)
        self.assertEqual(self.request(f'/api/notes/{identifier}/edit','POST',{'transcript':'new'})[0],400)
        self.assertEqual(self.request(f'/api/notes/{identifier}/ignore','POST',{'ignored':False})[0],200)
        note=self.service.store.note(identifier)
        self.assertFalse(note['ignored'])
        self.assertEqual(note['answer']['summary'],'Saved reply')
        self.assertEqual(note['status'],'needs_review')
        self.assertEqual(note['unread'],0)
        self.assertEqual(len(note['history']),1)

    def test_ignored_handwriting_stays_ignored_after_changes_and_sync(self):
        identifier=self.ingest()['notes'][0]
        self.service.ignore_note(identifier,True)
        self.assertEqual(self.ingest()['notes'],[])
        fixture_pdf(self.pdf,position=150)
        self.assertEqual(self.ingest()['notes'],[identifier])
        self.mock_inventory([self.entry])
        self.service.model=Mock();self.service.model.ready.return_value=True
        self.service.enqueue=Mock()
        self.service.sync()
        self.assertTrue(self.service.store.note(identifier)['ignored'])
        self.assertEqual(self.service.store.notes(status='pending'),[])
        self.service.enqueue.assert_not_called()
        self.service.analyze(identifier)
        self.service.model.recognize.assert_not_called()
        self.service.model.answer.assert_not_called()

    def test_ignore_during_recognition_discards_late_transcription(self):
        identifier=self.ingest()['notes'][0]
        self.service.model=Mock()
        def recognize(*args):
            self.service.ignore_note(identifier,True)
            return dict(transcript='late writing',question='late question',anchor='',title='late',kind='question',confidence=1,tags=[])
        self.service.model.recognize.side_effect=recognize
        self.service.analyze(identifier)
        self.assertEqual(self.service.store.note(identifier)['transcript'],'')
        self.service.model.answer.assert_not_called()

    def test_ignore_while_preparing_images_stops_the_model_request(self):
        identifier=self.ingest()['notes'][0]
        from app.pdf_notes import render_note_images
        def prepare(*args):
            result=render_note_images(*args)
            self.service.ignore_note(identifier,True)
            return result
        self.service.model=Mock()
        with patch('app.service.render_note_images',side_effect=prepare):
            self.service.analyze(identifier)
        self.service.model.recognize.assert_not_called()
        self.service.model.answer.assert_not_called()

    def test_ignore_then_restore_during_answer_discards_the_old_result(self):
        identifier=self.ingest()['notes'][0]
        self.service.edit_note(identifier,{'question':'question'})
        self.service.model=Mock()
        def answer(*args):
            self.service.ignore_note(identifier,True)
            self.service.ignore_note(identifier,False)
            return dict(summary='late',explanation='late')
        self.service.model.answer.side_effect=answer
        self.service.analyze(identifier)
        note=self.service.store.note(identifier)
        self.assertFalse(note['ignored'])
        self.assertIsNone(note['answer'])
        self.assertEqual(note['status'],'recognized')

    def test_ignore_during_failed_answer_does_not_restore_an_error(self):
        identifier=self.ingest()['notes'][0]
        self.service.edit_note(identifier,{'question':'question'})
        self.service.model=Mock()
        def answer(*args):
            self.service.ignore_note(identifier,True)
            raise ModelError('late failure')
        self.service.model.answer.side_effect=answer
        with self.assertRaises(ModelError):self.service.analyze(identifier)
        note=self.service.store.note(identifier)
        self.assertTrue(note['ignored'])
        self.assertEqual(note['error'],'')
        self.assertEqual(note['status'],'recognized')

    def test_ignore_requires_boolean_and_does_not_change_device_files(self):
        identifier=self.ingest()['notes'][0]
        self.service.device=Mock()
        for value in [None,1,'true',[]]:
            self.assertEqual(self.request(f'/api/notes/{identifier}/ignore','POST',{'ignored':value})[0],400)
        self.service.ignore_note(identifier,True)
        self.service.ignore_note(identifier,True)
        self.assertEqual(self.service.device.method_calls,[])
        self.assertEqual(self.service.store.note(identifier)['ignore_version'],1)

    def test_channels_group_documents_and_keep_full_counts_when_searching(self):
        fixture_pdf(self.pdf,extra_note=True)
        first,second=self.ingest()['notes']
        self.service.store.save_answer(first,dict(summary='unique result',explanation='details'),'model')
        self.service.store.execute("UPDATE notes SET status='needs_review' WHERE id=?",(second,))
        other_entry=dict(self.entry,entry_path='Document/other.pdf',entry_id='other-device-file')
        other=self.service.upsert_document(other_entry)
        fixture_pdf(self.pdf)
        other_note=ingest_pdf(self.service.store,self.service.data,other,self.pdf)['notes'][0]
        self.service.store.save_answer(other_note,dict(summary='Other result',explanation='details'),'model')
        self.service.store.execute("UPDATE notes SET activity_at='2026-11-12T01:00:00Z' WHERE document_id=?",(self.docid,))
        self.service.store.execute("UPDATE notes SET activity_at='2026-11-12T10:00:00+08:00' WHERE document_id=?",(other,))
        status,body=self.request('/api/channels')
        self.assertEqual(status,200)
        channels=json.loads(body)
        self.assertEqual([row['id'] for row in channels],[other,self.docid])
        target=channels[1]
        self.assertEqual((target['notes'],target['unread'],target['needs_review']),(2,1,1))
        search=json.loads(self.request('/api/channels?q=unique')[1])
        self.assertEqual(len(search),1)
        self.assertEqual(search[0]['notes'],2)
        self.assertEqual(json.loads(self.request('/api/channels?status=needs_review')[1])[0]['id'],self.docid)
        thread=json.loads(self.request(f'/api/notes?document_id={self.docid}&order=thread')[1])
        self.assertEqual([note['id'] for note in thread],[first,second])
        answer=self.service.store.note(other_note)['answer_id']
        self.request(f'/api/documents/{other}/read','POST',{'answer_ids':[answer]})
        self.assertEqual(json.loads(self.request('/api/channels')[1])[0]['id'],self.docid)
        self.service.ignore_note(first,True)
        self.assertEqual(self.service.store.channels(search='unique'),[])
        self.assertEqual(self.service.store.channels(status='ignored')[0]['notes'],1)

    def test_read_receipts_do_not_clear_newer_answers_or_other_documents(self):
        identifier=self.ingest()['notes'][0]
        self.service.store.save_answer(identifier,dict(summary='first',explanation='details'),'model')
        old=self.service.store.note(identifier)['answer_id']
        self.service.store.save_answer(identifier,dict(summary='new',explanation='details'),'model')
        latest=self.service.store.note(identifier)['answer_id']
        other=self.service.upsert_document(dict(self.entry,entry_path='Document/other.pdf',entry_id='other-file'))
        other_note=ingest_pdf(self.service.store,self.service.data,other,self.pdf)['notes'][0]
        self.service.store.save_answer(other_note,dict(summary='other',explanation='details'),'model')
        other_answer=self.service.store.note(other_note)['answer_id']
        path=f'/api/documents/{self.docid}/read'
        for answer in [old,other_answer]:
            self.assertEqual(json.loads(self.request(path,'POST',{'answer_ids':[answer]})[1])['marked'],[])
            self.assertTrue(self.service.store.note(identifier)['unread'])
        self.request(f'/api/notes/{identifier}/read','POST',{'answer_id':old})
        self.assertTrue(self.service.store.note(identifier)['unread'])
        self.assertEqual(self.request(f'/api/notes/{identifier}/read','POST',{})[0],400)
        for values in [[True],[-1],'all',None,list(range(1,502))]:
            self.assertEqual(self.request(path,'POST',{'answer_ids':values})[0],400)
        self.assertEqual(json.loads(self.request(path,'POST',{'answer_ids':[latest]})[1])['marked'],[latest])
        self.assertFalse(self.service.store.note(identifier)['unread'])
        self.assertTrue(self.service.store.note(other_note)['unread'])

    def test_ignored_answers_are_not_related_context(self):
        ignored=self.ingest()['notes'][0]
        self.service.store.save_answer(ignored,dict(summary='ignored context',explanation='details',tags=['topic']),'model')
        self.service.ignore_note(ignored,True)
        other=self.service.upsert_document(dict(self.entry,entry_path='Document/other.pdf',entry_id='other-file'))
        identifier=ingest_pdf(self.service.store,self.service.data,other,self.pdf)['notes'][0]
        self.service.edit_note(identifier,{'question':'question'})
        self.service.store.execute('UPDATE notes SET tags=? WHERE id=?',(json.dumps(['topic']),identifier))
        self.service.model=Mock();self.service.model.answer.return_value=dict(summary='answer',explanation='details')
        self.service.analyze(identifier)
        self.assertEqual(self.service.model.answer.call_args.args[1],[])

    def test_target_images_hide_neighbours_and_preserve_original(self):
        from app.pdf_notes import render_note_images, write_target_page
        from pypdf import PdfReader
        from PIL import Image, ImageChops
        fixture_pdf(self.pdf, extra_note=True)
        writer=PdfWriter(clone_from=self.pdf)
        second=writer.pages[0]['/Annots'][1].get_object()
        second[NameObject('/Rect')]=ArrayObject([FloatObject(x) for x in (300,170,320,190)])
        second[NameObject('/InkList')]=ArrayObject([ArrayObject([FloatObject(x) for x in (300,170,320,190)])])
        writer.write(self.pdf)
        ids=self.ingest()['notes']
        self.assertEqual(len(ids),2)
        original=self.pdf.read_bytes()
        note=self.service.store.note(ids[0])
        images=render_note_images(self.pdf,note,self.service.data,'snapshot-a')
        reader=PdfReader(self.pdf)
        isolated=self.pdf.parent/'isolated.pdf'
        write_target_page(reader,1,set(note['annotation_ids']),isolated)
        self.assertEqual(len(extract_annotations(isolated)[1]),1)
        self.assertEqual(len(extract_annotations(self.pdf)[1]),2)
        clean=self.pdf.parent/'clean.pdf'
        write_target_page(reader,1,set(),clean)
        self.assertEqual(extract_annotations(clean)[1],[])
        # A neighbouring stroke inside the padded crop disappears, not just its metadata.
        with Image.open(note['preview_path']) as before, Image.open(images['model_preview_path']) as after:
            self.assertEqual(before.size,after.size)
            self.assertIsNotNone(ImageChops.difference(before,after).getbbox())
        cached=Path(images['model_preview_path']).stat().st_mtime_ns
        self.assertEqual(render_note_images(self.pdf,note,self.service.data,'snapshot-a'),images)
        self.assertEqual(Path(images['model_preview_path']).stat().st_mtime_ns,cached)
        changed=render_note_images(self.pdf,note,self.service.data,'snapshot-b')
        self.assertNotEqual(changed['model_preview_path'],images['model_preview_path'])
        self.assertEqual(self.pdf.read_bytes(),original)
        self.assertEqual(self.ingest()['notes'],[])

    def test_target_images_handle_all_rotations(self):
        from app.pdf_notes import render_note_images
        from PIL import Image
        for rotation in (0,90,180,270):
            fixture_pdf(self.pdf,rotation=rotation)
            self.ingest()
            note=self.service.store.notes()[0]
            images=render_note_images(self.pdf,note,self.service.data,str(rotation))
            with Image.open(images['model_preview_path']) as crop, Image.open(images['page_image_path']) as page:
                self.assertGreater(crop.width,20)
                self.assertGreater(crop.height,20)
                self.assertEqual(page.width>page.height,rotation in (90,270))

    def scoped_note(self, identifier, refs):
        self.service.store.execute("UPDATE notes SET kind='answer',transcript='作答',question_refs=?,scope_status='known',scope_version=1,status='recognized' WHERE id=?",(json.dumps(refs),identifier))

    def test_verified_conflict_withdraws_both_claims_but_keeps_history(self):
        fixture_pdf(self.pdf,extra_note=True)
        first,second=self.ingest()['notes']
        for identifier in (first,second): self.scoped_note(identifier,['1'])
        self.service.store.save_answer(first,dict(summary='prior',explanation='prior'),'test')
        self.service.model=Mock()
        result=self.service.analyze(second)
        self.assertTrue(result['needs_review'])
        self.service.model.answer.assert_not_called()
        a,b=self.service.store.note(first),self.service.store.note(second)
        self.assertEqual((a['status'],b['status']),('needs_review','needs_review'))
        self.assertEqual(a['scope_conflicts'],[second])
        self.assertEqual(b['scope_conflicts'],[first])
        self.assertEqual(len(a['history']),1)
        self.assertEqual(b['history'],[])
        # Human scope correction resolves the conflict without overwriting history.
        self.service.edit_note(second,dict(transcript='第2题选 A',question_refs='2'))
        self.service.model.answer.return_value=dict(summary='new',explanation='new',exercise_reviews=[exercise_review(question_ref='2')])
        self.service.analyze(second)
        self.assertEqual(self.service.store.note(second)['status'],'answered')
        self.service.model.recognize.assert_not_called()

    def test_legacy_claim_does_not_block_verified_scope(self):
        fixture_pdf(self.pdf,extra_note=True)
        first,second=self.ingest()['notes']
        self.service.store.save_answer(first,dict(summary='legacy',explanation='legacy',exercise_reviews=[exercise_review()]),'old')
        self.scoped_note(second,['1'])
        self.service.model=Mock()
        self.service.model.answer.return_value=dict(summary='new',explanation='new',exercise_reviews=[exercise_review()])
        self.service.analyze(second)
        self.assertEqual(self.service.store.note(second)['status'],'answered')

    def test_conflict_appearing_during_answer_prevents_publication(self):
        fixture_pdf(self.pdf,extra_note=True)
        first,second=self.ingest()['notes']
        self.scoped_note(first,['1'])
        self.service.model=Mock()
        def answer(*args):
            self.scoped_note(second,['1'])
            return dict(summary='new',explanation='new',exercise_reviews=[exercise_review()])
        self.service.model.answer.side_effect=answer
        self.assertTrue(self.service.analyze(first)['needs_review'])
        self.assertEqual(self.service.store.note(first)['history'],[])

    def test_unknown_exercise_scope_stops_before_answer(self):
        identifier=self.ingest()['notes'][0]
        self.service.model=Mock()
        self.service.model.recognize.return_value=dict(transcript='圈选',question='核对',anchor='',title='作答',kind='answer',confidence=.99,tags=[],question_refs=[],scope_status='unknown')
        self.assertTrue(self.service.analyze(identifier)['needs_review'])
        self.service.model.answer.assert_not_called()

    def test_content_edit_invalidates_scope_and_missing_review_is_not_saved(self):
        identifier=self.ingest()['notes'][0]
        self.scoped_note(identifier,['1','2'])
        self.service.model=Mock()
        self.service.model.answer.return_value=dict(summary='both',explanation='both',exercise_reviews=[exercise_review()])
        self.assertTrue(self.service.analyze(identifier)['needs_review'])
        self.assertEqual(self.service.store.note(identifier)['history'],[])
        fixture_pdf(self.pdf,position=130)
        self.ingest()
        note=self.service.store.note(identifier)
        self.assertEqual((note['scope_version'],note['scope_status'],note['question_refs']),(0,'unknown',[]))

    def test_scanned_exercise_uses_page_images_and_archives_explanation(self):
        identifier=self.ingest()['notes'][0]
        self.assertEqual(self.service.store.note(identifier)['source_text'],'')
        client=ModelClient(self.service.settings)
        client.completion=Mock(side_effect=[
            dict(transcript='第1题选 A',question='核对第1题的作答',anchor='2 + 3 = ?',title='第1题作答',kind='answer',confidence=.97,tags=['加法'],question_refs=['1'],scope_status='known'),
            dict(summary='第1题需要订正',explanation='详见逐题解析。',exercise_reviews=[exercise_review()])
        ])
        self.service.model=client
        self.service.analyze(identifier)
        note=self.service.store.note(identifier)
        self.assertEqual(note['kind'],'answer')
        self.assertEqual(note['answer']['exercise_reviews'][0]['verdict'],'incorrect')
        self.assertEqual(note['answer']['exercise_reviews'][0]['knowledge_points'],exercise_review()['knowledge_points'])
        self.assertTrue(note['unread'])
        for call in client.completion.call_args_list:
            content=call.args[0][1]['content']
            images=[part['image_url']['url'] for part in content if part['type']=='image_url']
            self.assertEqual(len(images),2)
            self.assertTrue(images[0].startswith('data:image/png;base64,'))
            self.assertTrue(images[1].startswith('data:image/jpeg;base64,'))
        payload=json.loads(client.completion.call_args_list[1].args[0][1]['content'][0]['text'])
        self.assertEqual(payload['transcript'],'第1题选 A')
        self.assertNotIn(str(self.service.data),json.dumps(payload))
        self.assertEqual(len(list((self.service.data/'page-context').glob('*.jpg'))),1)
        status,body=self.request('/api/export.md')
        self.assertEqual(status,200)
        for text in ['第1题','A. 4','B. 5','少算了 1','加法表示合并两个数量','模型推导的参考解答']:
            self.assertIn(text.encode(),body)
        api=json.loads(self.request(f'/api/notes/{identifier}')[1])
        self.assertEqual(api['answer']['exercise_reviews'],note['answer']['exercise_reviews'])
        self.assertNotIn('page_image_path',api)

    def test_inflight_review_is_discarded_when_pdf_version_changes(self):
        identifier=self.ingest()['notes'][0]
        self.service.edit_note(identifier,{'transcript':'第1题选 A'})
        self.service.model=Mock()
        def changed_page(*args):
            writer=PdfWriter(clone_from=self.pdf)
            writer.add_metadata({'/Title':'Updated question page'})
            with self.pdf.open('wb') as file:writer.write(file)
            self.ingest()
            return dict(summary='stale',explanation='stale',exercise_reviews=[exercise_review()])
        self.service.model.answer.side_effect=changed_page
        self.service.analyze(identifier)
        self.assertIsNone(self.service.store.note(identifier)['answer'])

    def test_inflight_answer_cannot_overwrite_newly_edited_note(self):
        identifier = self.ingest()['notes'][0]
        self.service.edit_note(identifier, {'question':'old question'})
        self.service.model = Mock()
        def answer(*args):
            self.service.edit_note(identifier, {'question':'new question'})
            return {'summary':'stale','explanation':'stale'}
        self.service.model.answer.side_effect = answer
        self.service.analyze(identifier)
        self.assertIsNone(self.service.store.note(identifier)['answer'])
        self.assertEqual(self.service.store.note(identifier)['question'],'new question')

    def test_clock_year_2000_is_not_recent_learning(self):
        fixture_pdf(self.pdf,date="D:20000601010000+08'00'")
        self.ingest()
        report = self.service.report()
        self.assertEqual(report['recent_notes'],0)
        self.assertEqual(report['uncertain_time'],1)

    def test_report_compares_instants_across_timezones(self):
        identifier = self.ingest()['notes'][0]
        old = (datetime.now(timezone.utc)-timedelta(hours=25)).astimezone(timezone(timedelta(hours=8))).isoformat()
        self.service.store.execute('UPDATE notes SET device_modified=?,time_trusted=1 WHERE id=?',(old,identifier))
        self.assertEqual(self.service.report(1)['recent_notes'],0)
        self.assertEqual(self.service.report(7)['recent_notes'],1)

    def test_manual_sync_forces_recent_file_and_deduplicates_notes(self):
        self.entry['reading_date']='2026-11-12T01:00:00Z'
        self.service.store.execute('UPDATE documents SET fingerprint=? WHERE id=?',(fingerprint(self.entry),self.docid))
        self.service.device.connect=Mock(return_value=object())
        self.service.device.inventory=Mock(return_value=[self.entry])
        def download(device, metadata, target):
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(self.pdf,target)
        self.service.device.download=Mock(side_effect=download)
        self.assertEqual(self.service.sync(manual=False)['files'],0)
        self.assertEqual(self.service.sync(manual=True)['files'],1)
        self.assertEqual(len(self.service.sync(manual=True)['changed_notes']),0)
        self.assertEqual(self.service.store.stats()['notes'],1)

    def test_settings_and_api_never_return_secret(self):
        self.service.save_settings({'model_api_key':'private-test-secret','model_name':'vision','model_base_url':'https://example.com/v1'})
        status, body = self.request('/api/state')
        self.assertEqual(status,200)
        self.assertNotIn(b'private-test-secret',body)
        self.assertEqual(self.service.config_path.stat().st_mode&0o777,0o600)
        self.assertNotIn('private-test-secret',self.service.safe_error(Exception('oops private-test-secret')))

    def test_http_host_origin_and_path_security(self):
        self.assertEqual(self.request('/api/health',host='evil.example:4387')[0],403)
        self.assertEqual(self.request('/api/sync','POST',origin='https://evil.example')[0],403)
        self.assertEqual(self.request('/../../.tools/dpt-auth/privatekey.dat')[0],404)
        self.assertEqual(self.request('/api/settings','POST',{'auto_sync':False},origin='http://localhost:4387')[0],200)

    def test_note_export_static_and_media_routes(self):
        identifier=self.ingest()['notes'][0]
        for path in ['/', '/app.js','/styles.css','/api/notes',f'/api/notes/{identifier}',f'/media/preview/{identifier}',f'/media/document/{self.docid}','/api/export.json','/api/export.md','/api/backup.sqlite3','/api/report']:
            with self.subTest(path=path):
                status,body=self.request(path)
                self.assertEqual(status,200)
                self.assertTrue(body)
        self.assertNotIn(str(self.service.data).encode(),self.request('/api/notes')[1])

    def test_tab_icons_are_served_as_svg_without_login(self):
        self.enable_web_auth()
        for path in ['/favicon.svg', '/favicon-unread.svg']:
            with self.subTest(path=path):
                status,body=self.request(path)
                self.assertEqual(status,200)
                self.assertIn('Content-Type: image/svg+xml',self.last_headers)
                self.assertIn(b'<svg',body)

    def test_daily_summary_api_download_and_view_tracking(self):
        identifier=self.ingest()['notes'][0]
        self.assertEqual(self.request(f'/api/notes/{identifier}/view','POST',{})[0],200)
        self.assertEqual(self.service.store.one('SELECT note_id FROM note_views')['note_id'],identifier)
        self.assertEqual(self.request('/api/daily-summaries?date=bad')[0],400)
        self.assertEqual(self.request('/api/daily-summaries/generate','POST',{'date':'2999-01-01'})[0],400)
        self.assertEqual(self.request('/api/daily-summaries/generate','POST',{'date':'2020-04-01'})[0],202)
        self.assertEqual(self.service.model_tasks.qsize(),1)
        _,kind,args,_=self.service.model_tasks.get_nowait()
        self.assertEqual(kind,'daily_summary')
        summary=self.service.daily.generate(**args)['summary_id']
        status,payload=self.request('/api/daily-summaries?date=2020-04-01')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(payload)['selected']['id'],summary)
        status,body=self.request(f'/api/daily-summaries/{summary}/download')
        self.assertEqual(status,200)
        self.assertIn('attachment; filename="study-summary-2020-04-01.md"',self.last_headers)
        self.assertIn('未调用 AI'.encode(),body)
        self.assertEqual(self.request('/api/daily-summaries/99999')[0],404)

    def test_upload_rejects_invalid_pdf_and_only_queues_valid_file(self):
        self.assertEqual(self.request('/api/upload','POST',raw=b'not a PDF',filename='test.pdf')[0],400)
        status,body=self.request('/api/upload','POST',raw=self.pdf.read_bytes(),filename='test.pdf')
        self.assertEqual(status,202)
        self.assertTrue(json.loads(body)['queued'])

    def test_relocation_preserves_data_and_changes_all_asset_paths(self):
        self.ingest()
        copied=self.root/'copied'
        shutil.copytree(self.service.data,copied)
        relocate(copied)
        other=Service(copied,self.root/'auth')
        note=other.store.notes()[0]
        self.assertTrue(Path(note['preview_path']).is_relative_to(copied.resolve()))
        snap=other.store.one('SELECT * FROM snapshots')
        self.assertTrue(Path(snap['pdf_path']).exists())

    def test_legacy_database_keeps_notes_answers_and_saved_settings(self):
        identifier=self.ingest()['notes'][0]
        self.service.store.save_answer(identifier,dict(summary='Saved answer',explanation='Details'),'previous-model')
        self.service.save_settings({'model_api_key':'preserved-test-key'})
        with self.service.store.db() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        current=self.service.data/DATABASE_NAME
        legacy=self.service.data/LEGACY_DATABASE_NAME
        self.assertTrue(current.exists())
        current.rename(legacy)
        reopened=Service(self.service.data,self.root/'auth')
        self.assertEqual(reopened.store.note(identifier)['answer']['summary'],'Saved answer')
        self.assertEqual(reopened.settings()['model_api_key'],'preserved-test-key')
        self.assertFalse(current.exists())

    def test_slow_model_does_not_block_device_operations(self):
        identifier=self.ingest()['notes'][0]
        self.service.edit_note(identifier,{'question':'question'})
        started,release,probed=threading.Event(),threading.Event(),threading.Event()
        self.service.model=Mock()
        def slow_answer(*args):
            started.set();release.wait(3)
            return dict(summary='answer',explanation='details')
        self.service.model.answer.side_effect=slow_answer
        self.service.device.probe=lambda: (probed.set() or dict(online=True))
        threads=[threading.Thread(target=self.service._worker,args=(work,),daemon=True) for work in (self.service.tasks,self.service.model_tasks)]
        for thread in threads:thread.start()
        try:
            self.service.enqueue('analyze',identifier)
            self.assertTrue(started.wait(2))
            self.service.enqueue('probe')
            self.assertTrue(probed.wait(1),'device probe was blocked by model inference')
        finally:
            release.set()
            self.service.stop_event.set()
            for thread in threads:thread.join(2)

    def test_upload_collision_never_targets_annotated_original(self):
        remote='Document/收件箱/note.pdf'
        device=Mock()
        device.list_document_info.return_value=dict(entry_path=remote)
        self.service.device.connect=Mock(return_value=device)
        self.service.device.upload(self.pdf,'Document/收件箱')
        self.assertNotEqual(device.upload_file.call_args.args[1],remote)

    def mock_inventory(self, entries, on_download=None):
        self.service.device.connect=Mock(return_value=object())
        self.service.device.inventory=Mock(return_value=entries)
        def download(device, metadata, target):
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(self.pdf,target)
            if on_download: on_download()
        self.service.device.download=Mock(side_effect=download)

    def fake_paper(self,entries=None):
        paper=FakePaper(entries or [self.entry],self.pdf.read_bytes())
        self.service.device.connect=Mock(return_value=paper)
        self.service.device.inventory=Mock(side_effect=lambda device:paper.inventory())
        def download(device,metadata,path):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(paper.payloads[metadata['entry_id']])
            return path
        self.service.device.download=Mock(side_effect=download)
        return paper

    def test_create_folder_is_available_without_changing_sync_scope(self):
        paper=self.fake_paper()
        self.service.save_settings({'sync_folders':[]})
        result=self.service.create_folder('Document/资料/书籍')
        self.assertEqual(result['path'],'Document/资料/书籍')
        self.assertIn('Document/资料/书籍',paper.folders)
        self.assertEqual(self.service.settings()['sync_folders'],[])
        self.service.device.download.assert_not_called()
        self.assertEqual(self.service.store.stats()['notes'],0)

    def test_native_move_keeps_handwriting_answers_ids_and_deduplication(self):
        noteid=self.ingest()['notes'][0]
        self.service.store.save_answer(noteid,dict(summary='原解读',explanation='原内容'),'previous')
        paper=self.fake_paper()
        result=self.service.move_documents([self.docid],'Document/资料')
        self.assertEqual(result['moved'],[self.docid])
        self.assertFalse(result['errors'])
        self.assertEqual(len(paper.moves),1)
        self.assertEqual(self.service.store.document_at('Document/资料/test.pdf')['id'],self.docid)
        self.assertEqual(self.service.store.note(noteid)['answer']['summary'],'原解读')
        before=list((self.service.data/'move-backups').glob('*/*.before.pdf'))
        self.assertEqual(len(before),1)
        self.assertEqual(before[0].read_bytes(),self.pdf.read_bytes())
        self.assertFalse(list((self.service.data/'move-backups').glob('*/*.after.pdf')))
        self.assertEqual(self.service.sync(manual=True)['changed_notes'],[])
        self.assertEqual(self.service.store.stats()['notes'],1)
        self.assertEqual(self.service.store.one('SELECT count(*) AS n FROM answers')['n'],1)

    def test_move_outside_sync_scope_does_not_extract_or_send_notes(self):
        self.fake_paper()
        self.service.save_settings({'sync_folders':['Document/工作']})
        self.service.model=Mock()
        result=self.service.move_documents([self.docid],'Document/归档')
        self.assertEqual(result['moved'],[self.docid])
        self.assertEqual(self.service.store.stats()['notes'],0)
        self.assertEqual(self.service.settings()['sync_folders'],['Document/工作'])
        self.service.model.recognize.assert_not_called()
        self.service.model.answer.assert_not_called()

    def test_destination_collision_is_rejected_before_any_move(self):
        other=dict(self.entry,entry_id='other',entry_path='Document/资料/test.pdf')
        paper=self.fake_paper([self.entry,other])
        with self.assertRaisesRegex(ValueError,'同名文件'):
            self.service.move_documents([self.docid],'Document/资料')
        self.assertEqual(paper.moves,[])
        self.service.device.download.assert_not_called()
        self.assertEqual(paper.entries[self.entry['entry_id']]['entry_path'],self.entry['entry_path'])

    def test_batch_duplicate_filenames_are_rejected_before_any_move(self):
        other=dict(self.entry,entry_id='other',entry_path='Document/其他/test.pdf')
        otherid=self.service.upsert_document(other)
        paper=self.fake_paper([self.entry,other])
        with self.assertRaisesRegex(ValueError,'同名文件'):
            self.service.move_documents([self.docid,otherid],'Document/资料')
        self.assertEqual(paper.moves,[])

    def test_failed_backup_prevents_device_move(self):
        paper=self.fake_paper()
        self.service.device.download.side_effect=requests.ConnectionError('offline')
        result=self.service.move_documents([self.docid],'Document/资料')
        self.assertEqual(paper.moves,[])
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(result['moved'],[])

    def test_failed_native_move_keeps_backup_and_original_file(self):
        paper=self.fake_paper()
        def fail(*args):raise requests.HTTPError('move failed')
        paper.move_file_to_folder_by_id=fail
        result=self.service.move_documents([self.docid],'Document/资料')
        self.assertTrue(result['errors'])
        self.assertEqual(paper.entries[self.entry['entry_id']]['entry_path'],self.entry['entry_path'])
        self.assertEqual(len(list((self.service.data/'move-backups').glob('*/*.before.pdf'))),1)

    def test_changed_readback_keeps_both_copies_and_reports_error(self):
        paper=self.fake_paper()
        original=paper.move_file_to_folder_by_id
        def move(*args):
            response=original(*args)
            paper.payloads[self.entry['entry_id']]=self.pdf.read_bytes()+b'\n% changed during move'
            return response
        paper.move_file_to_folder_by_id=move
        result=self.service.move_documents([self.docid],'Document/资料')
        self.assertTrue(result['errors'])
        self.assertEqual(result['moved'],[])
        self.assertEqual(len(list((self.service.data/'move-backups').glob('*/*.before.pdf'))),1)
        self.assertEqual(len(list((self.service.data/'move-backups').glob('*/*.after.pdf'))),1)
        self.assertEqual(self.service.store.document(self.docid)['remote_path'],'Document/资料/test.pdf')

    def test_already_in_target_is_a_noop(self):
        paper=self.fake_paper()
        result=self.service.move_documents([self.docid],'Document')
        self.assertEqual(result['skipped'],[self.docid])
        self.assertEqual(paper.moves,[])
        self.service.device.download.assert_not_called()

    def test_external_device_move_preserves_document_and_note_identity(self):
        noteid=self.ingest()['notes'][0]
        self.mock_inventory([dict(self.entry,entry_path='Document/新目录/test.pdf')])
        result=self.service.sync(manual=True)
        self.assertEqual(result['changed_notes'],[])
        self.assertEqual(self.service.store.document_at('Document/新目录/test.pdf')['id'],self.docid)
        self.assertEqual(self.service.store.note(noteid)['document_id'],self.docid)
        self.assertEqual(self.service.store.stats()['notes'],1)

    def test_new_file_at_old_path_does_not_overwrite_moved_archive(self):
        self.service.upsert_document(dict(self.entry,entry_path='Document/新目录/test.pdf'))
        new_id=self.service.upsert_document(dict(self.entry,entry_id='replacement'))
        self.assertNotEqual(new_id,self.docid)
        self.assertEqual(self.service.store.document(self.docid)['remote_path'],'Document/新目录/test.pdf')
        self.assertEqual(self.service.store.document(new_id)['remote_path'],'Document/test.pdf')

    def test_management_routes_validate_and_queue_without_model_or_scope(self):
        self.service.save_settings({'sync_folders':[]})
        self.assertEqual(self.request('/api/folders/create','POST',{'path':'Document/../坏路径'})[0],400)
        self.assertEqual(self.request('/api/documents/move','POST',{'ids':[],'folder':'Document/资料'})[0],400)
        self.assertEqual(self.request('/api/documents/move','POST',{'ids':['0'*24],'folder':'Document/资料'})[0],400)
        self.assertEqual(self.request('/api/folders/create','POST',{'path':'Document/资料'})[0],202)
        self.assertEqual(self.request('/api/documents/move','POST',{'ids':[self.docid],'folder':'Document/资料'})[0],202)

    def test_unauthenticated_requests_cannot_create_or_move(self):
        self.enable_web_auth()
        self.assertEqual(self.request('/api/folders/create','POST',{'path':'Document/资料'})[0],401)
        self.assertEqual(self.request('/api/documents/move','POST',{'ids':[self.docid],'folder':'Document/资料'})[0],401)
        self.assertEqual(self.service.store.query('SELECT * FROM jobs'),[])

    def test_new_install_requires_explicit_folder_choice(self):
        empty=Service(self.root/'empty',self.root/'auth')
        self.assertEqual(empty.settings()['sync_folders'],[])
        empty.device.connect=Mock()
        with self.assertRaisesRegex(ValueError,'选择同步文件夹'): empty.sync()
        empty.device.connect.assert_not_called()
        self.assertEqual(empty.store.stats()['notes'],0)

    def test_folder_boundaries_children_and_new_documents(self):
        self.service.save_settings({'sync_folders':['Document/工作']})
        inside=dict(self.entry,entry_path='Document/工作/new.pdf',entry_id='new-file')
        child=dict(self.entry,entry_path='Document/工作/项目/deep.pdf',entry_id='deep-file')
        outside=dict(self.entry,entry_path='Document/工作备份/private.pdf',entry_id='private-file')
        self.mock_inventory([inside,child,outside])
        result=self.service.sync(manual=False)
        self.assertEqual(result['files'],2)
        pulled=[call.args[1]['entry_path'] for call in self.service.device.download.call_args_list]
        self.assertEqual(pulled,[inside['entry_path'],child['entry_path']])
        self.assertFalse(self.service.store.document(document_id(outside['entry_path']))['tracked'])
        self.assertEqual(len(result['changed_notes']),2)
        self.assertEqual(self.service.settings()['upload_folder'],'Document/工作')

    def test_outside_folder_cannot_be_pulled_or_analyzed_via_api(self):
        noteid=self.ingest()['notes'][0]
        self.service.save_settings({'sync_folders':['Document/工作']})
        for route in [f'/api/documents/{self.docid}/pull',f'/api/notes/{noteid}/analyze',f'/api/documents/{self.docid}/track']:
            self.assertEqual(self.request(route,'POST',{'tracked':True})[0],400)
        self.assertEqual(self.service.store.query('SELECT * FROM jobs'),[])
        status,body=self.request(f'/api/notes/{noteid}')
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)['in_sync_scope'])

    def test_removing_scope_retains_answers_and_skips_pending_model_work(self):
        noteid=self.ingest()['notes'][0]
        self.service.store.save_answer(noteid,dict(summary='saved',explanation='saved'),'previous model')
        self.service.save_settings({'sync_folders':[]})
        self.service.model=Mock()
        self.service.analyze(noteid)
        self.service.model.recognize.assert_not_called()
        self.service.model.answer.assert_not_called()
        self.assertEqual(self.service.store.note(noteid)['answer']['summary'],'saved')

    def test_scope_change_during_download_does_not_ingest_outside_notes(self):
        self.mock_inventory([self.entry],lambda:self.service.save_settings({'sync_folders':[]}))
        result=self.service.sync()
        self.assertEqual(result['files'],0)
        self.assertEqual(self.service.store.stats()['notes'],0)
        self.assertFalse(any((self.service.data/'incoming').glob('*.pdf')))

    def test_scope_change_during_answer_discards_the_result(self):
        noteid=self.ingest()['notes'][0]
        self.service.edit_note(noteid,{'question':'解释这段设计'})
        self.service.model=Mock()
        def answer(*args):
            self.service.save_settings({'sync_folders':[]})
            return dict(summary='late answer',explanation='details')
        self.service.model.answer.side_effect=answer
        self.service.analyze(noteid)
        self.assertIsNone(self.service.store.note(noteid)['answer'])

    def test_scope_change_during_recognition_does_not_update_the_note(self):
        noteid=self.ingest()['notes'][0]
        self.service.model=Mock()
        def recognize(*args):
            self.service.save_settings({'sync_folders':[]})
            return dict(transcript='late text',question='late question',anchor='',title='late',kind='question',confidence=1,tags=[])
        self.service.model.recognize.side_effect=recognize
        self.service.analyze(noteid)
        self.service.model.answer.assert_not_called()
        self.assertEqual(self.service.store.note(noteid)['transcript'],'')

    def test_late_model_failure_after_scope_change_does_not_modify_archive(self):
        noteid=self.ingest()['notes'][0]
        self.service.edit_note(noteid,{'question':'问题'})
        self.service.model=Mock()
        def answer(*args):
            self.service.save_settings({'sync_folders':[]})
            raise ModelError('服务暂时不可用')
        self.service.model.answer.side_effect=answer
        with self.assertRaises(ModelError):self.service.analyze(noteid)
        note=self.service.store.note(noteid)
        self.assertEqual(note['status'],'recognized')
        self.assertEqual(note['error'],'')

    def test_related_notes_outside_scope_are_not_sent_to_model(self):
        work=dict(self.entry,entry_path='Document/工作/a.pdf',entry_id='work-file')
        workid=self.service.upsert_document(work)
        noteid=ingest_pdf(self.service.store,self.service.data,workid,self.pdf)['notes'][0]
        outside=self.ingest()['notes'][0]
        self.service.store.save_answer(outside,dict(summary='private context',explanation='private',tags=['架构']),'previous')
        self.service.edit_note(noteid,{'question':'解释架构'})
        self.service.store.execute('UPDATE notes SET tags=? WHERE id=?',(json.dumps(['架构']),noteid))
        self.service.save_settings({'sync_folders':['Document/工作']})
        self.service.model=Mock()
        self.service.model.answer.return_value=dict(summary='answer',explanation='details')
        self.service.analyze(noteid)
        self.assertEqual(self.service.model.answer.call_args.args[1],[])

    def test_generic_examples_survive_markdown_export(self):
        noteid=self.ingest()['notes'][0]
        self.service.store.save_answer(noteid,dict(summary='系统设计',explanation='上下文',examples=[dict(text='请求超时后重试',explanation='使用退避策略')]),'model')
        status,body=self.request('/api/export.md')
        self.assertEqual(status,200)
        self.assertIn('请求超时后重试'.encode(),body)
        self.assertIn('使用退避策略'.encode(),body)

    def test_invalid_scope_does_not_widen_existing_selection(self):
        self.service.save_settings({'sync_folders':['Document/工作']})
        self.assertEqual(self.request('/api/settings','POST',{'sync_folders':['Document/工作/../私人']})[0],400)
        self.assertEqual(self.service.settings()['sync_folders'],['Document/工作'])

    def test_automatic_analysis_only_uses_current_folder(self):
        inside_entry=dict(self.entry,entry_path='Document/工作/inside.pdf',entry_id='inside-file')
        inside_doc=self.service.upsert_document(inside_entry)
        inside=ingest_pdf(self.service.store,self.service.data,inside_doc,self.pdf)['notes'][0]
        entry=dict(self.entry,entry_path='Document/其他/private.pdf',entry_id='outside-file')
        docid=self.service.upsert_document(entry)
        outside=ingest_pdf(self.service.store,self.service.data,docid,self.pdf)['notes'][0]
        self.service.save_settings({'sync_folders':['Document/工作']})
        self.mock_inventory([inside_entry,entry])
        self.service.enqueue=Mock()
        self.service.model=Mock()
        self.service.model.ready.return_value=True
        self.service.sync(manual=False)
        queued=[call.args[1] for call in self.service.enqueue.call_args_list if call.args[0]=='analyze']
        self.assertEqual(queued,[inside])
        self.assertNotIn(outside,queued)

    def test_moved_document_is_retained_but_not_automatically_interpreted(self):
        entry=dict(self.entry,entry_path='Document/selected/test.pdf')
        docid=self.service.upsert_document(entry)
        noteid=ingest_pdf(self.service.store,self.service.data,docid,self.pdf)['notes'][0]
        self.service.save_settings({'sync_folders':['Document/selected']})
        self.mock_inventory([dict(self.entry,entry_path='Document/elsewhere/test.pdf')])
        self.service.sync()
        self.assertTrue(self.service.store.document(docid)['device_present'])
        self.assertFalse(self.service.store.document(docid)['tracked'])
        self.assertEqual(self.service.store.document(docid)['remote_path'],'Document/elsewhere/test.pdf')
        self.service.model=Mock()
        self.service.analyze(noteid)
        self.service.model.answer.assert_not_called()
        self.assertIsNotNone(self.service.store.note(noteid))

    def test_legacy_migration_preserves_credentials_and_saved_answers(self):
        noteid=self.ingest()['notes'][0]
        self.service.store.save_answer(noteid,dict(summary='saved',explanation='details'),'old')
        values=self.service.settings();values.pop('sync_folders');values['model_api_key']='preserved-secret'
        self.service.config_path.write_text(json.dumps(values))
        migrated=Service(self.service.data,self.root/'auth')
        self.assertEqual(migrated.settings()['sync_folders'],[])
        self.assertEqual(migrated.settings()['model_api_key'],'preserved-secret')
        self.assertFalse(migrated.store.document(self.docid)['tracked'])
        self.assertEqual(migrated.store.note(noteid)['answer']['summary'],'saved')

    def test_inventory_lists_empty_folders_without_downloading_files(self):
        self.mock_inventory([self.entry,dict(entry_type='folder',entry_path='Document/空文件夹',entry_id='folder-id')])
        self.service.refresh_inventory()
        self.service.device.download.assert_not_called()
        status,body=self.request('/api/folders')
        self.assertEqual(status,200)
        self.assertIn('Document/空文件夹',[row['path'] for row in json.loads(body)])


class ModelTests(unittest.TestCase):
    def test_group_matching_is_one_to_one_and_independent_of_input_order(self):
        from app.pdf_notes import match_note_groups
        def old(identifier,ids,archived=0):
            return dict(id=identifier,page=1,annotation_ids=json.dumps(ids),archived=archived,last_seen='2026-11-13')
        def group(*ids): return [dict(id=i,page=1) for i in ids]
        existing=[old(1,['a','b','c','d']),old(2,['e']),old(3,['a'],1)]
        groups=[group('a'),group('b','c','d'),group('e')]
        for rows in (existing,list(reversed(existing))):
            for incoming in (groups,list(reversed(groups))):
                result=match_note_groups(incoming,rows)
                mapping={tuple(sorted(a['id'] for a in incoming[i])):n['id'] for i,n in result.items()}
                self.assertEqual(mapping,{('b','c','d'):1,('e',):2})
        ties=[old(2,['a']),old(1,['b'])]
        self.assertEqual(match_note_groups([group('a','b')],ties)[0]['id'],1)

    def test_existing_note_database_migration_keeps_saved_replies_and_read_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'existing.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA)
                self.assertNotIn('ignored',{row[1] for row in db.execute('PRAGMA table_info(notes)')})
                db.execute("INSERT INTO documents(id,remote_path,name,title,created_at) VALUES(?,?,?,?,?)",('a'*24,'Document/existing.pdf','existing.pdf','Existing','2026-11-12T01:00:00Z'))
                db.execute("INSERT INTO snapshots(id,document_id,sha256,pdf_path,captured_at) VALUES(?,?,?,?,?)",(1,'a'*24,'hash','existing.pdf','2026-11-12T01:00:00Z'))
                db.execute("INSERT INTO notes(id,document_id,page,group_key,content_hash,annotation_ids,snapshot_id,title,first_seen,last_seen,unread,status,answer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(1,'a'*24,1,'group','hash','[]',1,'Question','2026-11-12T01:00:00Z','2026-11-12T04:00:00Z',1,'answered',1))
                db.execute("INSERT INTO answers(id,note_id,payload,model,provenance,created_at) VALUES(?,?,?,?,?,?)",(1,1,json.dumps(dict(summary='Saved',explanation='Details')),'model','model','2026-11-12T02:00:00Z'))
            store=Store(path)
            note=store.note(1)
            self.assertEqual(note['answer']['summary'],'Saved')
            self.assertEqual((note['ignored'],note['ignore_version'],note['unread']),(0,0,1))
            self.assertEqual(note['activity_at'],'2026-11-12T02:00:00Z')
            self.assertEqual((note['question_refs'],note['scope_status'],note['scope_version'],note['scope_conflicts']),([], 'unknown', 0, []))
            self.assertEqual(store.channels()[0]['unread'],1)
            Store(path)
            self.assertEqual(store.note(1)['activity_at'],note['activity_at'])

    def exercise_answer(self,reviews,source='Question and options',**note_changes):
        note=dict(question='',transcript='第1题选 A',anchor=source,document_title='练习文档',page=1,source_text=source,kind='answer',question_refs=['1','2'],scope_status='known')
        note.update(note_changes)
        client=ModelClient(lambda:{})
        client.completion=Mock(return_value=dict(summary='作答核对',explanation='解析',exercise_reviews=reviews))
        return client.answer(note,[])

    def test_scope_filter_removes_neighbour_reviews_and_all_summary_prose(self):
        note=dict(question='',transcript='第8题选 A',anchor='',document_title='练习',page=2,source_text='',kind='answer',question_refs=['8'],scope_status='known')
        client=ModelClient(lambda:{})
        client.completion=Mock(return_value=dict(summary='第7题错误，第8题正确，第9题错误',explanation='邻题解析',examples=[dict(text='邻题')],tags=['邻题'],uncertainties=['邻题'],exercise_reviews=[exercise_review(question_ref=str(i)) for i in (7,8,8,9)]))
        result=client.answer(note,[])
        self.assertEqual([r['question_ref'] for r in result['exercise_reviews']],['8'])
        for field in ('summary','explanation','examples','tags','uncertainties'):
            self.assertNotIn('邻题',str(result[field]))
            self.assertNotIn('第7题',str(result[field]))
            self.assertNotIn('第9题',str(result[field]))

    def test_reference_normalization_keeps_sections_distinct_and_unknown_empty(self):
        from app.note_scope import normalize_refs, recognition_scope
        self.assertEqual(normalize_refs(['第８题','8','問題2：第８题','問題3:8',False,None,'8-10']),['8','2:8','3:8'])
        self.assertEqual(recognition_scope(dict(kind='answer',scope_status='known',question_refs=[])),([], 'unknown'))
        self.assertEqual(recognition_scope(dict(kind='note',scope_status='not_applicable',question_refs=[])),([], 'not_applicable'))
        result=self.exercise_answer([exercise_review(question_ref='2:8'),exercise_review(question_ref='3:8')],question_refs=['2:8'])
        self.assertEqual([r['question_ref'] for r in result['exercise_reviews']],['2:8'])

    def test_answer_reference_accepts_printed_section_label_without_losing_scope(self):
        from app.note_scope import normalize_ref
        self.assertEqual(normalize_ref('問題2 第8题'), '2:8')
        self.assertEqual(normalize_ref('問題3 第8题'), '3:8')
        result=self.exercise_answer([exercise_review(question_ref='問題2 第8题'),exercise_review(question_ref='問題2 第9题')],question_refs=['8'])
        self.assertEqual([r['question_ref'] for r in result['exercise_reviews']], ['8'])
        result=self.exercise_answer([exercise_review(question_ref='問題2 第8题'),exercise_review(question_ref='問題3 第8题'),exercise_review(question_ref=8)],question_refs=['2:8'])
        self.assertEqual([r['question_ref'] for r in result['exercise_reviews']], ['2:8'])
        result=self.exercise_answer([exercise_review(question_ref=8)],question_refs=['8'])
        self.assertEqual([r['question_ref'] for r in result['exercise_reviews']], ['8'])

    def test_answer_text_context_is_anchor_centred_and_excludes_old_answers(self):
        source='unrelated prefix '*1000+'TARGET QUESTION'+' trailing '*1000
        note=dict(question='check',transcript='8: A',anchor='TARGET QUESTION',document_title='exercise',page=2,source_text=source,kind='answer',question_refs=['8'],scope_status='known')
        client=ModelClient(lambda:{})
        client.completion=Mock(return_value=dict(summary='answer',explanation='details',exercise_reviews=[]))
        client.answer(note,[dict(summary='old answer')])
        context=json.loads(client.completion.call_args.args[0][1]['content'][0]['text'])
        self.assertIn('TARGET QUESTION',context['source_text'])
        self.assertLess(len(context['source_text']),2000)
        self.assertEqual(context['related_notes'],[])
        note['anchor']='not present'
        client.answer(note,[])
        context=json.loads(client.completion.call_args.args[0][1]['content'][0]['text'])
        self.assertEqual(context['source_text'],'')

    def test_exercise_review_preserves_wrong_and_correct_answers(self):
        correct=exercise_review(question_ref='第2题',user_answer='B. 5',verdict='correct',explanation='2 + 3 = 5，选择正确。')
        result=self.exercise_answer([exercise_review(),correct])
        self.assertEqual([row['verdict'] for row in result['exercise_reviews']],['incorrect','correct'])
        self.assertEqual(result['exercise_reviews'][0]['explanation'],exercise_review()['explanation'])
        self.assertEqual(result['exercise_reviews'][0]['basis'],'reasoning')

    def test_incomplete_or_uncertain_exercises_cannot_be_marked_wrong(self):
        cases=[dict(user_answer=''),dict(question=''),dict(suggested_answer=''),dict(explanation=''),
               dict(confidence=.6),dict(confidence=float('nan')),dict(confidence='invalid'),
               dict(basis='insufficient'),dict(verdict='guaranteed-wrong'),dict(suggested_answer='A. 4')]
        for values in cases:
            with self.subTest(values=values):
                result=self.exercise_answer([exercise_review(**values)])['exercise_reviews'][0]
                self.assertEqual(result['verdict'],'uncertain')
                self.assertIn('暂不判断对错',result['explanation'])

    def test_document_answer_quote_must_occur_in_supplied_pdf_text(self):
        row=exercise_review(basis='document',reference_quote='第1题：B')
        supported=self.exercise_answer([row],source='参考答案\n第1题： B\n第2题：A')['exercise_reviews'][0]
        self.assertEqual(supported['basis'],'document')
        self.assertEqual(supported['verdict'],'incorrect')
        self.assertEqual(supported['reference_quote'],'第1题：B')
        unsupported=self.exercise_answer([row])['exercise_reviews'][0]
        self.assertEqual(unsupported['basis'],'reasoning')
        self.assertEqual(unsupported['verdict'],'uncertain')
        self.assertEqual(unsupported['reference_quote'],'')

    def test_reading_mark_in_exam_does_not_require_an_exercise_review(self):
        result=self.exercise_answer([],document_title='历年真题',kind='mark',transcript='',anchor='圈出需要解释的概念')
        self.assertEqual(result['exercise_reviews'],[])
        malformed=self.exercise_answer([None,True,'incorrect'])
        self.assertEqual(malformed['exercise_reviews'],[])

    def test_storage_defaults_and_ambiguous_databases(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('app.paths.ROOT',root),patch.dict(os.environ,{},clear=True):
                self.assertEqual(default_data_dir(),root/'output/data')
                legacy=root/'output/study'/LEGACY_DATABASE_NAME
                legacy.parent.mkdir(parents=True)
                legacy.touch()
                self.assertEqual(default_data_dir(),legacy.parent)
                with patch.dict(os.environ,{'APP_DATA_DIR':str(root/'chosen')}):
                    self.assertEqual(default_data_dir(),root/'chosen')
            self.assertEqual(database_path(legacy.parent),legacy)
            (legacy.parent/DATABASE_NAME).touch()
            with self.assertRaises(ValueError):database_path(legacy.parent)

    def test_remote_locations_are_configurable_and_reject_unsafe_paths(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(deployment_location(),('apps/dpt-syncer','dpt-syncer'))
            with patch.dict(os.environ,{'DPT_REMOTE_ROOT':'apps/reader-archive','DPT_DEPLOY_PROJECT':'reader-archive'}):
                self.assertEqual(deployment_location(),('apps/reader-archive','reader-archive'))
            for root in ['/tmp/app','../app','apps/../app','apps/./app','apps//app','apps/app/','apps/a;command','apps/$(id)']:
                with self.subTest(root=root),patch.dict(os.environ,{'DPT_REMOTE_ROOT':root}):
                    with self.assertRaises(ValueError):deployment_location()
            for project in ['-p','app name','app;command','Uppercase']:
                with self.subTest(project=project),patch.dict(os.environ,{'DPT_DEPLOY_PROJECT':project}):
                    with self.assertRaises(ValueError):deployment_location()

    def test_deployment_uses_same_custom_location_and_project_at_every_step(self):
        import scripts.deploy as deployment
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'scripts').mkdir()
            (root/'scripts/install_remote.sh').write_text('example installer')
            with patch.object(deployment,'ROOT',root),patch.dict(os.environ,{'DPT_REMOTE_ROOT':'apps/reader-archive','DPT_DEPLOY_PROJECT':'reader-archive'},clear=True),patch.object(deployment.subprocess,'run') as run,patch.object(deployment.subprocess,'check_output',return_value=b'http://192.0.2.10:4387\n'),contextlib.redirect_stdout(io.StringIO()):
                deploy('user@server.example')
            commands=[call.args[0] for call in run.call_args_list]
            self.assertIn('apps/reader-archive/incoming',commands[1][-1])
            self.assertEqual(commands[2][-1],'user@server.example:apps/reader-archive/incoming/')
            self.assertEqual(commands[3],['ssh','user@server.example','bash -s -- apps/reader-archive reader-archive'])
            self.assertEqual((root/'output/deploy/service-url.txt').read_text(),'http://192.0.2.10:4387\n')

    def test_remote_network_settings_use_options_or_existing_private_configuration(self):
        script=Path('scripts/install_remote.sh').read_text()
        code=script.split('APP_LISTEN_ADDRESS="$(python3 - <<\'PY\'\n',1)[1].split('\nPY\n)"',1)[0]
        cases=[
            ({'APP_LISTEN_ADDRESS':'192.0.2.10','DPT_ADDRESS':'reader.example','APP_ALLOWED_HOSTS':'notes.example'},{},{}),
            ({},{'APP_LISTEN_ADDRESS':'192.0.2.10','APP_ALLOWED_HOSTS':'notes.example'},{'device_address':'reader.example'}),
            ({'DPT_ADDRESS':'reader.example'},{},{})
        ]
        for options,old,settings in cases:
            with self.subTest(options=options,old=old),tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                for name in ['incoming','current','data']:(root/name).mkdir()
                (root/'incoming/deploy-options.json').write_text(json.dumps(options))
                (root/'current/.env').write_text('\n'.join(key+'='+value for key,value in old.items()))
                (root/'data/settings.json').write_text(json.dumps(settings))
                connection=Mock();connection.getsockname.return_value=('192.0.2.10',1234)
                socket_context=Mock();socket_context.__enter__=Mock(return_value=connection);socket_context.__exit__=Mock(return_value=False)
                with patch.dict(os.environ,{'DPT_INSTALL_ROOT':str(root)},clear=True),patch('socket.socket',return_value=socket_context),contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(code,'remote-network-settings','exec'),{})
                result=json.loads((root/'data/deploy-network.json').read_text())
                self.assertEqual(result['listen'],'192.0.2.10')
                self.assertEqual(result['reader'],'reader.example')

    def test_source_packages_exclude_runtime_files_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);app=root/'app';app.mkdir()
            for name in ['service.py','requirements.txt','settings.json','privatekey.dat','notes.pdf','notes.sqlite3']:
                (app/name).write_text('example')
            external=root/'external.txt';external.write_text('not-source')
            (app/'link.py').symlink_to(external)
            names={path.name for path in application_sources(root)}
            self.assertEqual(names,{'service.py','requirements.txt'})

    def test_environment_file_is_data_and_process_settings_take_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'.env'
            marker=Path(folder)/'must-not-exist'
            path.write_text("# private settings\nDPT_ADDRESS=reader.example\nAPP_ALLOWED_HOSTS='reader.example,notes.example'\nDPT_WEB_URL='$(touch "+str(marker)+")'\nIGNORED_SETTING=ignored\n")
            values=read_env(path)
            self.assertNotIn('IGNORED_SETTING',values)
            self.assertIn('$(touch ',values['DPT_WEB_URL'])
            with patch.dict(os.environ,{'DPT_ADDRESS':'process.example'}):
                load_env(path)
                self.assertEqual(os.environ['DPT_ADDRESS'],'process.example')
            self.assertFalse(marker.exists())

    def test_blank_device_default_and_private_environment_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ,{'DPT_ADDRESS':''}):
                service=Service(Path(folder)/'empty',Path(folder)/'auth')
                self.assertEqual(service.settings()['device_address'],'')
            with patch.dict(os.environ,{'DPT_ADDRESS':'reader.example'}):
                data=Path(folder)/'configured'
                service=Service(data,Path(folder)/'auth')
                self.assertEqual(service.settings()['device_address'],'reader.example')
                service.save_settings({'device_address':'saved.example'})
                self.assertEqual(Service(data,Path(folder)/'auth').settings()['device_address'],'saved.example')

    def test_deployment_options_are_private_and_exclude_unrelated_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'options.json'
            with patch.dict(os.environ,{'DPT_ADDRESS':'reader.example','APP_LISTEN_ADDRESS':'192.0.2.10','APP_ALLOWED_HOSTS':'notes.example','UNRELATED_API_KEY':'example-test-value'},clear=True):
                write_options(path)
            values=json.loads(path.read_text())
            self.assertEqual(set(values),{'DPT_ADDRESS','APP_LISTEN_ADDRESS','APP_ALLOWED_HOSTS'})
            self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(validate_target('user@server.example'),'user@server.example')
        for target in ['',None,'-V','server;command','server name']:
            with self.assertRaises(ValueError):validate_target(target)
        self.assertEqual(validate_web_url('https://notes.example/'),'https://notes.example')
        with self.assertRaises(ValueError):validate_web_url('file:///tmp/example')

    def test_pairing_credentials_require_explicit_packaging_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';source.mkdir()
            (source/'privatekey.dat').write_text('example-test-key')
            (source/'deviceid.dat').write_text('example-test-device')
            destination=Path(folder)/'safe-default'
            self.assertEqual(copy_pairing_credentials(source,destination),[])
            self.assertFalse(destination.exists())
            names=copy_pairing_credentials(source,destination,include=True)
            self.assertEqual(set(names),{'privatekey.dat','deviceid.dat'})
            self.assertEqual((destination/'privatekey.dat').stat().st_mode&0o777,0o600)
            self.assertEqual(destination.stat().st_mode&0o777,0o700)

    def test_existing_database_migrates_stable_device_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'legacy.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA)
                db.execute('INSERT INTO documents(id,remote_path,name,title,metadata,created_at) VALUES(?,?,?,?,?,?)',
                    ('a'*24,'Document/existing.pdf','existing.pdf','Existing',json.dumps({'entry_id':'stable-device-id'}),'2026-11-12T00:00:00Z'))
            store=Store(path)
            document=store.document('a'*24)
            self.assertEqual(document['device_entry_id'],'stable-device-id')
            self.assertEqual(document['remote_path'],'Document/existing.pdf')
            self.assertTrue(document['device_present'])

    def test_folder_selection_normalization_and_validation(self):
        self.assertEqual(normalize_folders(['Document/A/sub','Document/A/','Document/A']),['Document/A'])
        self.assertTrue(in_folders('Document/A/sub/file.pdf',['Document/A']))
        self.assertFalse(in_folders('Document/AB/file.pdf',['Document/A']))
        for value in ['Other/A','/Document/A','Document/../private','Document//A','Document/./A','Document/file.pdf']:
            with self.assertRaises(ValueError): normalize_folders([value])
        with self.assertRaises(ValueError): normalize_folders('Document/A')
    def test_source_extracted_with_private_umask_remains_readable_to_runtime_user(self):
        from scripts.package import build_source_archive
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'project'
            source=root/'app'/'__init__.py'
            source.parent.mkdir(parents=True,mode=0o700)
            source.write_text('"""source"""')
            source.chmod(0o600)
            archive_path=Path(temporary)/'code.tar.gz'
            build_source_archive([source],archive_path,root)
            extracted=Path(temporary)/'extracted'
            extracted.mkdir()
            previous=os.umask(0o077)
            try:
                subprocess.run(['tar','--no-same-owner','--same-permissions','-xzf',str(archive_path),'-C',str(extracted)],check=True,capture_output=True)
            finally:
                os.umask(previous)
            self.assertEqual((extracted/'app').stat().st_mode&0o777,0o755)
            self.assertEqual((extracted/'app/__init__.py').stat().st_mode&0o777,0o644)

    def test_model_cannot_invent_verified_grades_or_external_sources(self):
        client=ModelClient(lambda:{})
        client.completion=Mock(return_value=dict(summary='answer',explanation='details',grading={'is_correct':True},sources=['https://invented.example/']))
        note=dict(question='question',transcript='',anchor='',document_title='Original PDF',page=11,source_text='source')
        result=client.answer(note,[])
        self.assertNotIn('grading',result)
        self.assertEqual(result['sources'],['Original PDF，第11页'])

    def test_url_validation_and_json(self):
        self.assertEqual(validate_model_url('https://example.com/v1/chat/completions'),'https://example.com/v1')
        self.assertEqual(validate_model_url('http://127.0.0.1:11434/v1'),'http://127.0.0.1:11434/v1')
        for url in ['file:///etc/passwd','https://key:secret@example.com/v1','http://public.example/v1','https://example.com/v1?key=secret']:
            with self.assertRaises(ValueError): validate_model_url(url)
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'),{'ok':True})
        for content in ['[]','{"x":NaN}','not-json']:
            with self.assertRaises(ModelError): parse_json(content)

    def test_compatible_parameter_fallback_and_secret_redaction(self):
        def response(status,payload):
            result=requests.Response();result.status_code=status;result._content=json.dumps(payload).encode();return result
        session=Mock()
        session.post.side_effect=[response(400,{'error':'use max_completion_tokens instead of max_tokens'}),response(400,{'error':'response_format unsupported'}),response(200,{'choices':[{'message':{'content':'{"ok":true}'}}]})]
        config=dict(model_base_url='https://compatible.example/v1',model_name='vision',model_api_key='secret-123')
        client=ModelClient(lambda:config,session)
        self.assertEqual(client.completion([{'role':'user','content':'JSON'}]),{'ok':True})
        self.assertEqual(session.post.call_count,3)
        session.post.side_effect=None
        session.post.return_value=response(401,{'error':'bad secret-123'})
        with self.assertRaises(ModelError) as error: client.completion([])
        self.assertNotIn('secret-123',str(error.exception))

    def test_device_pdf_time_parsing(self):
        self.assertEqual(pdf_date("D:20261112094848+08'00'").utcoffset(),timedelta(hours=8))
        self.assertFalse(trusted_date('D:20000601000000Z'))
        self.assertIsNone(pdf_date('broken'))


if __name__=='__main__': unittest.main()
