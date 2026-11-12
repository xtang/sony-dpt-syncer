#!/usr/bin/env python3
"""Read-only evidence and call estimate for historical note repair. No model calls."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.note_scope import SCOPE_VERSION, normalize_refs, overlapping_refs
from app.pdf_notes import extract_annotations

REPORT_VERSION = 1


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def legacy_crop_bounds(page, group):
    """The historical padding intentionally remains fixed, independent of new renderers."""
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    ox, oy = float(page.mediabox.left), float(page.mediabox.bottom)
    left = max(0, min(a['rect'][0]-ox for a in group)-32)
    right = min(width, max(a['rect'][2]-ox for a in group)+38)
    bottom = max(0, min(a['rect'][1]-oy for a in group)-75)
    top = min(height, max(a['rect'][3]-oy for a in group)+62)
    if right-left < width*.65:
        center = (left+right)/2
        left, right = max(0, center-width*.38), min(width, center+width*.38)
    return [left+ox, bottom+oy, right+ox, top+oy]


def intersects(a, b):
    return min(a[2],b[2]) > max(a[0],b[0]) and min(a[3],b[3]) > max(a[1],b[1])


def normalized_text(value):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', value or '').casefold())


def build_report(database):
    """Never use Store/Service: their constructors perform migrations and job updates."""
    database = Path(database).resolve(strict=True)
    connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        notes = [dict(row) for row in connection.execute('''SELECT n.*,d.title AS document_title,
            d.tracked,d.device_present,s.sha256 AS snapshot_sha,s.pdf_path,
            a.payload AS answer_payload,a.provenance AS answer_provenance
            FROM notes n JOIN documents d ON d.id=n.document_id
            LEFT JOIN snapshots s ON s.id=n.snapshot_id
            LEFT JOIN answers a ON a.id=n.answer_id WHERE n.archived=0 ORDER BY n.id''')]
        archived_count = connection.execute('SELECT COUNT(*) FROM notes WHERE archived=1').fetchone()[0]
    finally:
        connection.close()
    state_hash = fingerprint(notes)
    groups = {}
    snapshots = {}
    entries = {}
    for note in notes:
        entry = dict(id=note['id'],document_id=note['document_id'],document_title=note['document_title'],
                     page=note['page'],title=note['title'],snapshot_id=note['snapshot_id'],
                     state_hash=fingerprint(note),scope_version=note.get('scope_version',0),
                     reasons=[],inspection_errors=[],skip_reasons=[],evidence={})
        entries[note['id']] = entry
        key = (note['document_id'],note['page'])
        groups.setdefault(key, []).append(note)
        if note.get('manual_transcript') or note.get('confirmation_stale') or note['answer_provenance'] == 'verified_import':
            entry['skip_reasons'].append('protected_confirmation')
        if note.get('ignored'):
            entry['skip_reasons'].append('ignored')
        if not note['tracked'] or not note['device_present']:
            entry['skip_reasons'].append('out_of_scope')
        if note.get('scope_version',0) >= SCOPE_VERSION:
            entry['skip_reasons'].append('already_scoped')
        try:
            ids = set(json.loads(note['annotation_ids']))
            cache_key = (note['snapshot_id'],note['pdf_path'],note['snapshot_sha'])
            if cache_key not in snapshots:
                try:
                    path = Path(note['pdf_path'])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != note['snapshot_sha']:
                        raise ValueError('snapshot checksum mismatch')
                    reader, annotations, _ = extract_annotations(path)
                    snapshots[cache_key] = (reader, annotations)
                except Exception as error:
                    snapshots[cache_key] = error
            cached = snapshots[cache_key]
            if isinstance(cached, Exception):
                raise cached
            reader, annotations = cached
            page_annotations = [a for a in annotations if a['page'] == note['page']]
            target = [a for a in page_annotations if a['id'] in ids]
            if not target or {a['id'] for a in target} != ids:
                raise ValueError('target annotations missing from snapshot')
            bounds = legacy_crop_bounds(reader.pages[note['page']-1], target)
            foreign = sorted(a['id'] for a in page_annotations if a['id'] not in ids and intersects(bounds,a['rect']))
            entry['evidence']['crop_bounds'] = bounds
            entry['evidence']['foreign_annotation_ids'] = foreign
            if foreign and note.get('scope_version',0) < SCOPE_VERSION:
                entry['reasons'].append('legacy_crop_contains_other_strokes')
        except Exception as error:
            # No configuration or model error text is read by this command.
            entry['inspection_errors'].append(type(error).__name__+': '+str(error))
    duplicate_pairs, transcript_pairs = [], []
    for page_notes in groups.values():
        for note in page_notes:
            entry = entries[note['id']]
            foreign = set(entry['evidence'].get('foreign_annotation_ids', []))
            entry['evidence']['neighbour_note_ids'] = [other['id'] for other in page_notes if other['id'] != note['id'] and
                                                       foreign & set(json.loads(other['annotation_ids']))]
        for left,right in itertools.combinations(page_notes,2):
            le,re = entries[left['id']],entries[right['id']]
            # Prior answers attached to needs_review notes are history, not current claims.
            claims = []
            for note in (left,right):
                answer = json.loads(note['answer_payload'] or 'null')
                reviews = answer.get('exercise_reviews',[]) if isinstance(answer,dict) and note['status']=='answered' else []
                claims.append(normalize_refs([r.get('question_ref') for r in reviews if isinstance(r,dict)]) if isinstance(reviews,list) else [])
            shared = [(a,b) for a in claims[0] for b in claims[1] if overlapping_refs([a],[b])]
            if shared:
                duplicate_pairs.append(dict(note_ids=[left['id'],right['id']],references=shared))
                for entry in (le,re):
                    if 'duplicate_question_review' not in entry['reasons']:
                        entry['reasons'].append('duplicate_question_review')
            a,b = normalized_text(left['transcript']),normalized_text(right['transcript'])
            bounds = [entry['evidence'].get('crop_bounds') for entry in (le,re)]
            if min(len(a),len(b))>=8 and all(bounds) and intersects(*bounds):
                ratio = SequenceMatcher(None,a,b,autojunk=False).ratio()
                if ratio>=.9:
                    transcript_pairs.append(dict(note_ids=[left['id'],right['id']],similarity=round(ratio,3),
                                                 excerpts=[left['transcript'][:500],right['transcript'][:500]]))
                    for entry in (le,re):
                        if 'similar_neighbour_transcript' not in entry['reasons']:
                            entry['reasons'].append('similar_neighbour_transcript')
    for entry in entries.values():
        if entry['inspection_errors']:
            entry['skip_reasons'].append('inspection_failed')
        entry['candidate'] = bool(entry['reasons'])
        entry['eligible'] = entry['candidate'] and not entry['skip_reasons']
    candidates = [e for e in entries.values() if e['candidate']]
    eligible = [e['id'] for e in candidates if e['eligible']]
    counts = Counter(reason for entry in candidates for reason in entry['skip_reasons'])
    return dict(report_version=REPORT_VERSION,generated_at=datetime.now(timezone.utc).isoformat(),
                database=str(database),state_hash=state_hash,read_only=True,model_calls=0,
                summary=dict(active_notes=len(notes),archived_notes_excluded=archived_count,
                             candidate_notes=len(candidates),eligible_notes=len(eligible),
                             candidate_skip_counts=dict(counts),duplicate_review_pairs=len(duplicate_pairs),
                             similar_transcript_pairs=len(transcript_pairs),
                             inspection_failures=sum(bool(e['inspection_errors']) for e in entries.values())),
                eligible_note_ids=eligible,
                model_call_estimate=dict(recognition_calls=len(eligible),answer_calls_at_most=len(eligible),
                                         total_at_most_without_transport_retries=2*len(eligible)),
                policy=dict(approval_required_before_repair=True,delete_old_answers=False,
                            skip_confirmed_content=True,refresh_report_if_state_changes=True,
                            interpretation='Candidates are evidence of possible overlap, not proof that a note is wrong.'),
                notes=list(entries.values()),duplicate_review_pairs=duplicate_pairs,similar_transcript_pairs=transcript_pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--output', default='-', help='New JSON file, or - for stdout; existing files are never overwritten')
    args = parser.parse_args()
    report = build_report(args.database)
    rendered = json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.output=='-':
        print(rendered,end='')
    else:
        with Path(args.output).open('x',encoding='utf-8') as output:
            output.write(rendered)


if __name__ == '__main__':
    main()
