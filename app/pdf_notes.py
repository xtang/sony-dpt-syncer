from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pdf_date(value):
    match = re.match(r"(?:D:)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(Z|[+-]\d{2}'?\d{2}'?)?", str(value or ""))
    if not match:
        return None
    try:
        parts = [int(v) for v in match.groups()[:6]]
        offset = match[7]
        if not offset:
            return None
        zone = timezone.utc
        if offset != "Z":
            digits = offset[1:].replace("'", "")
            zone = timezone((1 if offset[0] == "+" else -1) * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        return datetime(*parts, tzinfo=zone)
    except (ValueError, TypeError):
        return None


def trusted_date(value):
    date = pdf_date(value)
    return bool(date and 2015 <= date.year and date <= datetime.now(timezone.utc) + timedelta(days=1))


def extract_annotations(path):
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("无法读取加密 PDF，请先导出可阅读的副本")
    annotations = []
    page_texts = {}
    for number, page in enumerate(reader.pages, 1):
        page_texts[number] = (page.extract_text() or "")[:10000]
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            subtype = str(annotation.get("/Subtype", ""))
            if subtype in ("/Link", "/Widget", "/Popup"):
                continue
            if int(annotation.get("/F", 0)) & 3:
                continue
            rect = [round(float(v), 2) for v in annotation.get("/Rect", [])]
            if len(rect) != 4:
                continue
            strokes = [[round(float(v), 2) for v in stroke] for stroke in annotation.get("/InkList", [])]
            payload = {"subtype": subtype, "rect": rect, "strokes": strokes,
                       "contents": str(annotation.get("/Contents", "")),
                       "quads": [round(float(v), 2) for v in annotation.get("/QuadPoints", [])],
                       "color": [float(v) for v in annotation.get("/C", [])]}
            fingerprint = digest(payload)
            identifier = str(annotation.get("/NM", "")) or fingerprint
            annotations.append(dict(id=identifier, page=number, fingerprint=fingerprint,
                                    created=str(annotation.get("/CreationDate", "")),
                                    modified=str(annotation.get("/M", "")), **payload))
    return reader, annotations, page_texts


def nearby(a, b):
    x0, y0, x1, y1 = a["rect"]
    u0, v0, u1, v1 = b["rect"]
    dx = max(0, x0-u1, u0-x1)
    dy = max(0, y0-v1, v0-y1)
    return math.hypot(dx, dy) <= 26 or (dy <= 8 and dx <= 155)


def group_annotations(annotations):
    result = []
    remaining = list(annotations)
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for item in remaining[:]:
                if item["page"] == group[0]["page"] and any(nearby(item, other) for other in group):
                    group.append(item)
                    remaining.remove(item)
                    changed = True
        result.append(group)
    return result


def crop_group(pdf, page_number, group, destination, page):
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    left = max(0, min(item["rect"][0] for item in group) - 32)
    right = min(width, max(item["rect"][2] for item in group) + 38)
    bottom = max(0, min(item["rect"][1] for item in group) - 75)
    top = min(height, max(item["rect"][3] for item in group) + 62)
    # Add printed context around a short handwritten question or circled answer.
    if right-left < width*.65:
        center = (left+right)/2
        left = max(0, center-width*.38)
        right = min(width, center+width*.38)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dpt-note-") as work:
        prefix = Path(work)/"page"
        process = subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                                  "-singlefile", "-r", "150", "-png", str(pdf), str(prefix)],
                                 capture_output=True, timeout=75)
        if process.returncode:
            raise RuntimeError("PDF 页面渲染失败：" + process.stderr.decode(errors="replace")[:200])
        with Image.open(prefix.with_suffix(".png")) as image:
            rotation = int(page.get("/Rotate", 0)) % 360
            if rotation in (90, 270):
                sx, sy = image.width/height, image.height/width
                coords = (bottom, left, top, right) if rotation == 90 else (height-top, width-right, height-bottom, width-left)
            else:
                sx, sy = image.width/width, image.height/height
                coords = (left, height-top, right, height-bottom) if rotation == 0 else (width-right, bottom, width-left, top)
            box = (int(coords[0]*sx), int(coords[1]*sy), math.ceil(coords[2]*sx), math.ceil(coords[3]*sy))
            image.crop(box).convert("RGB").save(destination, "PNG")
    return [left, bottom, right, top]


def group_time(group):
    valid = [pdf_date(item["modified"]) for item in group if trusted_date(item["modified"])]
    created = [pdf_date(item["created"]) for item in group if trusted_date(item["created"])]
    return ((min(created).astimezone(timezone.utc).isoformat() if created else None),
            (max(valid).astimezone(timezone.utc).isoformat() if valid else None), bool(valid))


def render_page_context(pdf, page_number, destination):
    """Cache one annotated page so scanned questions retain their printed context."""
    destination = Path(destination)
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="dpt-page-") as temporary:
        prefix = Path(temporary)/"page"
        result = subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                                 "-singlefile", "-scale-to", "2400", "-jpeg", "-jpegopt", "quality=90",
                                 str(pdf), str(prefix)], capture_output=True, timeout=75)
        if result.returncode:
            raise RuntimeError("PDF 页面渲染失败："+result.stderr.decode(errors="replace")[:200])
        destination.write_bytes(prefix.with_suffix(".jpg").read_bytes())
    return destination


def ingest_pdf(store, data_dir, document_id, path, metadata=None):
    data_dir, path = Path(data_dir), Path(path)
    binary = path.read_bytes()
    sha = hashlib.sha256(binary).hexdigest()
    target = data_dir/"documents"/document_id/(sha+".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(binary)
    reader, annotations, page_texts = extract_annotations(target)
    from .store import now, encode
    store.execute("INSERT OR IGNORE INTO snapshots(document_id,sha256,pdf_path,captured_at) VALUES(?,?,?,?)", (document_id, sha, str(target), now()))
    snapshot = store.one("SELECT id FROM snapshots WHERE document_id=? AND sha256=?", (document_id, sha))["id"]
    store.execute("UPDATE documents SET snapshot_id=?,pages=?,last_seen=? WHERE id=?", (snapshot, len(reader.pages), now(), document_id))
    store.execute("UPDATE annotations SET removed=1 WHERE document_id=?", (document_id,))
    for item in annotations:
        identifier = document_id+":"+item["id"]
        store.execute("""INSERT INTO annotations(id,document_id,page,fingerprint,payload,first_seen,last_seen,removed)
            VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET page=excluded.page,
            fingerprint=excluded.fingerprint,payload=excluded.payload,last_seen=excluded.last_seen,removed=0""",
                      (identifier, document_id, item["page"], item["fingerprint"], encode(item), now(), now()))
    active, changed_notes = [], []
    for group in group_annotations(annotations):
        identifiers = sorted(item["id"] for item in group)
        group_key = digest([document_id, group[0]["page"], identifiers])
        content_hash = digest(sorted(item["fingerprint"] for item in group))
        old = store.one("SELECT * FROM notes WHERE group_key=?", (group_key,))
        active.append(group_key)
        if old and old["content_hash"] == content_hash:
            store.execute("UPDATE notes SET last_seen=?,archived=0,snapshot_id=? WHERE id=?", (now(), snapshot, old["id"]))
            continue
        preview = data_dir/"previews"/(group_key[:20]+"-"+content_hash[:12]+".png")
        crop_group(target, group[0]["page"], group, preview, reader.pages[group[0]["page"]-1])
        created, modified, reliable = group_time(group)
        text = "\n".join(item["contents"] for item in group if item["contents"].strip())
        title = "手写笔记" if any(item["subtype"] == "/Ink" for item in group) else "阅读标记"
        values = dict(snapshot_id=snapshot, content_hash=content_hash, preview_path=str(preview),
                      source_text=page_texts[group[0]["page"]], device_created=created,
                      device_modified=modified, time_trusted=int(reliable), last_seen=now())
        if old:
            store.execute("""UPDATE notes SET snapshot_id=?,content_hash=?,preview_path=?,source_text=?,
                device_created=?,device_modified=?,time_trusted=?,last_seen=?,status='pending',
                confidence=NULL,manual_transcript=0,archived=0,error='',unread=0,activity_at=? WHERE id=?""", (*values.values(), now(), old["id"]))
            store.execute("UPDATE notes SET transcript=?,question='',anchor='' WHERE id=?", (text, old["id"]))
            changed_notes.append(old["id"])
        else:
            identifier = store.execute("""INSERT INTO notes(document_id,page,group_key,content_hash,
                annotation_ids,snapshot_id,preview_path,source_text,title,transcript,device_created,
                device_modified,time_trusted,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, group[0]["page"], group_key, content_hash, encode(identifiers), snapshot,
                 str(preview), page_texts[group[0]["page"]], title, text, created, modified,
                 int(reliable), now(), now()))
            changed_notes.append(identifier)
            store.execute("UPDATE notes SET activity_at=first_seen WHERE id=?", (identifier,))
    for old in store.query("SELECT id,group_key FROM notes WHERE document_id=? AND archived=0", (document_id,)):
        if old["group_key"] not in active:
            store.execute("UPDATE notes SET archived=1 WHERE id=?", (old["id"],))
    return {"notes": changed_notes, "annotations": len(annotations), "groups": len(active), "snapshot_id": snapshot, "sha256": sha}
