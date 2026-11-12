from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, remote_path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 title TEXT NOT NULL, pages INTEGER DEFAULT 0, tracked INTEGER DEFAULT 0,
 baseline_path TEXT, fingerprint TEXT, device_modified TEXT, revision TEXT,
 file_size INTEGER DEFAULT 0, metadata TEXT DEFAULT '{}', last_seen TEXT,
 snapshot_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 sha256 TEXT NOT NULL, pdf_path TEXT NOT NULL, captured_at TEXT NOT NULL,
 UNIQUE(document_id,sha256)
);
CREATE TABLE IF NOT EXISTS annotations (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 page INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, removed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notes (
 id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 page INTEGER NOT NULL, group_key TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL,
 annotation_ids TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
 preview_path TEXT, source_text TEXT DEFAULT '', anchor TEXT DEFAULT '',
 title TEXT NOT NULL, transcript TEXT DEFAULT '', question TEXT DEFAULT '',
 kind TEXT DEFAULT 'unknown', status TEXT DEFAULT 'pending',
 confidence REAL, tags TEXT DEFAULT '[]', manual_transcript INTEGER DEFAULT 0,
 device_created TEXT, device_modified TEXT, time_trusted INTEGER DEFAULT 0,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, answer_id INTEGER,
 unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS answers (
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES notes(id),
 payload TEXT NOT NULL, model TEXT NOT NULL, provenance TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 message TEXT DEFAULT '', progress INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
 result TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, message TEXT NOT NULL,
 note_id INTEGER, document_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_status ON notes(archived,status,last_seen);
CREATE INDEX IF NOT EXISTS notes_document ON notes(document_id,page);
CREATE INDEX IF NOT EXISTS annotations_document ON annotations(document_id);
CREATE TABLE IF NOT EXISTS folders (
 path TEXT PRIMARY KEY, entry_id TEXT, last_seen TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            if "device_present" not in {row["name"] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN device_present INTEGER NOT NULL DEFAULT 1")
            if "device_entry_id" not in {row["name"] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN device_entry_id TEXT")
                for row in db.execute("SELECT id,metadata FROM documents").fetchall():
                    metadata = json.loads(row["metadata"] or "{}")
                    if metadata.get("entry_id"):
                        db.execute("UPDATE documents SET device_entry_id=? WHERE id=?", (metadata["entry_id"], row["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS documents_device_id ON documents(device_entry_id)")
            db.execute("UPDATE jobs SET status='interrupted',message='上次运行中断，可重新同步',updated_at=? WHERE status IN ('queued','running','waiting_pin')", (now(),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql, values=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, values)]

    def one(self, sql, values=()):
        rows = self.query(sql, values)
        return rows[0] if rows else None

    def execute(self, sql, values=()):
        with self.db() as db:
            cursor = db.execute(sql, values)
            return cursor.lastrowid

    def event(self, kind, message, note_id=None, document_id=None):
        self.execute("INSERT INTO events(kind,message,note_id,document_id,created_at) VALUES(?,?,?,?,?)", (kind, message, note_id, document_id, now()))

    def job(self, kind, message="等待处理"):
        identifier = uuid.uuid4().hex
        self.execute("INSERT INTO jobs(id,kind,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?)", (identifier, kind, "queued", message, now(), now()))
        return identifier

    def update_job(self, identifier, **fields):
        allowed = {"status", "message", "progress", "total", "result"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid job fields")
        if "result" in fields and not isinstance(fields["result"], str):
            fields["result"] = encode(fields["result"])
        fields["updated_at"] = now()
        self.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", (*fields.values(), identifier))

    def document(self, identifier):
        return self.one("SELECT * FROM documents WHERE id=?", (identifier,))

    def document_at(self, path):
        return self.one("SELECT * FROM documents WHERE remote_path=?", (path,))

    def notes(self, search="", status="all", document_id=None):
        where, values = ["n.archived=0"], []
        if search:
            where.append("(n.title LIKE ? OR n.transcript LIKE ? OR n.question LIKE ? OR n.tags LIKE ? OR a.payload LIKE ?)")
            values += ["%" + search + "%"] * 5
        if status == "unread":
            where.append("n.unread=1")
        elif status == "pending":
            where.append("n.status IN ('pending','recognized','error')")
        elif status != "all":
            where.append("n.status=?")
            values.append(status)
        if document_id:
            where.append("n.document_id=?")
            values.append(document_id)
        rows = self.query("""SELECT n.*,d.title AS document_title,d.name AS document_name,
            d.remote_path,a.payload AS answer_payload,a.model AS answer_model,a.provenance AS answer_provenance,
            a.created_at AS answered_at FROM notes n JOIN documents d ON d.id=n.document_id
            LEFT JOIN answers a ON a.id=n.answer_id WHERE """ + " AND ".join(where) + " ORDER BY n.unread DESC,n.time_trusted DESC,COALESCE(n.device_modified,n.last_seen) DESC,n.id DESC", values)
        return [self.decode_note(row) for row in rows]

    @staticmethod
    def decode_note(row):
        row["tags"] = json.loads(row["tags"] or "[]")
        row["annotation_ids"] = json.loads(row["annotation_ids"])
        row["answer"] = json.loads(row.pop("answer_payload", None) or "null")
        return row

    def note(self, identifier):
        row = self.one("""SELECT n.*,d.title AS document_title,d.name AS document_name,d.remote_path,
            a.payload AS answer_payload,a.model AS answer_model,a.provenance AS answer_provenance,
            a.created_at AS answered_at FROM notes n JOIN documents d ON d.id=n.document_id
            LEFT JOIN answers a ON a.id=n.answer_id WHERE n.id=?""", (identifier,))
        if not row:
            return None
        result = self.decode_note(row)
        result["history"] = self.query("SELECT id,model,provenance,created_at FROM answers WHERE note_id=? ORDER BY id DESC", (identifier,))
        return result

    def save_answer(self, note_id, answer, model, provenance="model"):
        with self.db() as db:
            cursor = db.execute("INSERT INTO answers(note_id,payload,model,provenance,created_at) VALUES(?,?,?,?,?)", (note_id, encode(answer), model, provenance, now()))
            db.execute("UPDATE notes SET answer_id=?,status='answered',unread=1,error='',last_seen=? WHERE id=?", (cursor.lastrowid, now(), note_id))
            row = db.execute("SELECT tags,title FROM notes WHERE id=?", (note_id,)).fetchone()
            existing = json.loads(row["tags"] or "[]")
            tags = answer.get("tags", [])
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, str) and tag[:30] not in existing:
                        existing.append(tag[:30])
                db.execute("UPDATE notes SET tags=? WHERE id=?", (encode(existing[:6]), note_id))
            if row["title"] in ("手写笔记", "阅读标记") and isinstance(answer.get("title"), str):
                db.execute("UPDATE notes SET title=? WHERE id=?", (answer["title"][:150], note_id))
        self.event("answer", "有一条新的笔记解读", note_id)

    def stats(self):
        row = self.one("""SELECT COUNT(*) AS notes,
            COALESCE(SUM(status IN ('pending','recognized','error')),0) AS pending,
            COALESCE(SUM(status='needs_review'),0) AS needs_review,
            COALESCE(SUM(status='answered'),0) AS answered,
            COALESCE(SUM(unread),0) AS unread FROM notes WHERE archived=0""")
        row["documents"] = self.one("SELECT COUNT(*) AS count FROM documents WHERE tracked=1")["count"]
        return row

    def backup(self, destination):
        with self.db() as db, sqlite3.connect(destination) as target:
            db.backup(target)
