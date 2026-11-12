#!/usr/bin/env python3
"""Execute an approved repair report using a staging database and guarded publication."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.service import Service
from app.store import encode, now
from app.note_scope import SCOPE_VERSION, overlapping_refs
from scripts.repair_report import REPORT_VERSION, build_report, fingerprint, report_rows

PATCH_FIELDS = ('title','transcript','question','anchor','kind','confidence','tags','status','error',
                'question_refs','scope_status','scope_version','scope_conflicts')
REVISION_FIELDS = ('title','transcript','question','anchor','manual_transcript','confirmation_stale',
                   'kind','question_refs','annotation_ids','content_hash','snapshot_id','answer_id','ignored')


def read_rows(database):
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return report_rows(db)


def apply_results(database, approved, staged, identifiers):
    """Only patch still-identical approved notes. All old answer rows remain untouched."""
    expected={n['id']:n for n in approved['notes'] if n['eligible']}
    staged_rows={n['id']:n for n in read_rows(staged.store.path)}
    result={'applied':[], 'skipped':[]}
    db=sqlite3.connect(database,timeout=30)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        with db:
            db.execute('BEGIN IMMEDIATE')
            live={n['id']:n for n in report_rows(db)}
            selected={}
            for identifier in identifiers:
                current=live.get(identifier)
                if identifier not in expected or not current or fingerprint(current)!=expected[identifier]['state_hash']:
                    result['skipped'].append(dict(id=identifier,reason='changed_since_report'))
                    continue
                candidate=staged_rows[identifier]
                if candidate['status'] not in ('answered','needs_review') or candidate['scope_version']!=SCOPE_VERSION:
                    result['skipped'].append(dict(id=identifier,reason='analysis_incomplete',status=candidate['status']))
                    continue
                selected[identifier]=dict(candidate)
            projected={**live,**selected}
            for identifier,candidate in selected.items():
                conflicts=[]
                if candidate['scope_status']=='known':
                    for other in projected.values():
                        if (other['id']!=identifier and not other['ignored'] and
                            other['document_id']==candidate['document_id'] and other['page']==candidate['page'] and
                            other['scope_status']=='known' and other['scope_version']==SCOPE_VERSION and
                            overlapping_refs(json.loads(candidate['question_refs']),json.loads(other['question_refs']))):
                            conflicts.append(other['id'])
                if conflicts:
                    candidate.update(status='needs_review',scope_conflicts=encode(sorted(conflicts)),error='同页笔记的题目归属冲突，请核对目标笔迹')
                old=live[identifier]
                revision={key:old.get(key) for key in REVISION_FIELDS}
                for key in ('question_refs','annotation_ids'):
                    revision[key]=json.loads(revision[key])
                db.execute('INSERT INTO note_revisions(note_id,reason,payload,created_at) VALUES(?,?,?,?)',
                           (identifier,'historical_repair',encode(revision),now()))
                answer_id=old['answer_id']
                if candidate['answer_id'] and candidate['answer_id']!=old['answer_id']:
                    answer=staged.store.one('SELECT * FROM answers WHERE id=? AND note_id=?',(candidate['answer_id'],identifier))
                    if not answer: raise ValueError('Missing staged answer')
                    answer_id=db.execute('INSERT INTO answers(note_id,payload,model,provenance,created_at) VALUES(?,?,?,?,?)',
                                         (identifier,answer['payload'],answer['model'],answer['provenance'],answer['created_at'])).lastrowid
                patch={key:candidate[key] for key in PATCH_FIELDS}
                patch.update(answer_id=answer_id,unread=int(candidate['status']=='answered'),activity_at=now(),last_seen=now())
                db.execute('UPDATE notes SET '+','.join(k+'=?' for k in patch)+' WHERE id=?',(*patch.values(),identifier))
                db.execute('INSERT INTO events(kind,message,note_id,created_at) VALUES(?,?,?,?)',
                           ('repair','已重新识别历史笔记，先前文字与解答已保留',identifier,now()))
                result['applied'].append(dict(id=identifier,status=candidate['status'],question_refs=json.loads(candidate['question_refs']),answer_id=answer_id,conflicts=conflicts))
    finally:
        db.close()
    return result


def run_repair(database, approved, run_dir, settings, model=None, workers=3):
    if approved.get('report_version')!=REPORT_VERSION or approved.get('read_only') is not True:
        raise ValueError('Unsupported approval report')
    approved_ids=approved.get('eligible_note_ids',[])
    if not approved_ids or len(approved_ids)!=len(set(approved_ids)):
        raise ValueError('Approval report has no unique eligible notes')
    run_dir=Path(run_dir).resolve()
    run_dir.mkdir(parents=True,exist_ok=False,mode=0o700)
    (run_dir/'approved-report.json').write_text(json.dumps(approved,ensure_ascii=False,indent=2))
    backup=run_dir/'before.sqlite3'
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    fresh=build_report(backup)
    old={n['id']:n for n in approved['notes'] if n['eligible']}
    valid=[n['id'] for n in fresh['notes'] if n['eligible'] and n['id'] in approved_ids and
           n['id'] in old and n['state_hash']==old[n['id']]['state_hash']]
    (run_dir/'preflight-report.json').write_text(json.dumps(fresh,ensure_ascii=False,indent=2))
    print(encode(dict(event='preflight',approved=len(approved_ids),eligible=len(valid),backup=str(backup))),flush=True)
    stage_dir=run_dir/'stage';stage_dir.mkdir(mode=0o700)
    shutil.copy2(backup,stage_dir/'notes.sqlite3')
    # Constructor migrations/job recovery run only against this disposable database.
    (stage_dir/'settings.json').write_text(encode({'sync_folders':settings.get('sync_folders',[]),'auto_sync':False,'auto_analyze':False}))
    service=Service(stage_dir,stage_dir/'unused-auth')
    service.config.update(settings,auto_sync=False,auto_analyze=False)
    service._refresh_tracking()
    if model is not None: service.model=model
    counts={'recognition':0,'answer':0};lock=threading.Lock()
    for name in ('recognize','answer'):
        original=getattr(service.model,name)
        def counted(*args,_original=original,_key='recognition' if name=='recognize' else 'answer',**kwargs):
            with lock: counts[_key]+=1
            return _original(*args,**kwargs)
        setattr(service.model,name,counted)
    outcomes=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(service.analyze,i):i for i in valid}
        for future in as_completed(futures):
            identifier=futures[future]
            try: outcome=dict(id=identifier,result=future.result())
            except Exception as error: outcome=dict(id=identifier,error=service.safe_error(error))
            outcomes.append(outcome)
            print(encode(dict(event='analyzed',**outcome)),flush=True)
    # Keep the proposed outcomes before publication, including any later conflict changes.
    proposals={str(i):service.store.note(i) for i in valid}
    (run_dir/'proposals.json').write_text(json.dumps(proposals,ensure_ascii=False,indent=2))
    published=apply_results(database,approved,service,valid)
    result=dict(backup=str(backup),approved_note_ids=approved_ids,model_calls=counts,
                skipped_before_analysis=sorted(set(approved_ids)-set(valid)),outcomes=outcomes,**published)
    (run_dir/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(encode(dict(event='published',applied=len(result['applied']),skipped=len(result['skipped']),model_calls=counts)),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--settings',type=Path,required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--execute-approved-report',action='store_true',required=True)
    args=parser.parse_args()
    run_repair(args.database,json.loads(args.report.read_text()),args.run_dir,json.loads(args.settings.read_text()))


if __name__=='__main__': main()
