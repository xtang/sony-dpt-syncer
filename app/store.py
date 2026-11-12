from __future__ import annotations

import json
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS note_revisions (
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES notes(id),
 reason TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_note ON note_revisions(note_id,id);
CREATE TABLE IF NOT EXISTS note_links (
 parent_id INTEGER NOT NULL REFERENCES notes(id), child_id INTEGER NOT NULL REFERENCES notes(id),
 relation TEXT NOT NULL, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), created_at TEXT NOT NULL,
 UNIQUE(parent_id,child_id,relation,snapshot_id)
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
        self._transactions = threading.local()
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(notes)")}
            for name, declaration in (("ignored", "INTEGER NOT NULL DEFAULT 0"), ("ignore_version", "INTEGER NOT NULL DEFAULT 0"), ("activity_at", "TEXT"),
                                      ("question_refs", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("scope_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                                      ("scope_version", "INTEGER NOT NULL DEFAULT 0"),
                                      ("scope_conflicts", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("confirmation_stale", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE notes ADD COLUMN {name} {declaration}")
            db.execute("UPDATE notes SET activity_at=COALESCE((SELECT created_at FROM answers WHERE id=notes.answer_id),first_seen) WHERE activity_at IS NULL")
            db.execute("UPDATE jobs SET status='interrupted',message='上次运行中断，可重新同步',updated_at=? WHERE status IN ('queued','running','waiting_pin')", (now(),))

    @contextmanager
    def db(self):
        active = getattr(self._transactions, "connection", None)
        if active is not None:
            yield active
            return
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        """Keep note reassignment, revisions and archival atomic for one ingestion."""
        if getattr(self._transactions, "connection", None) is not None:
            yield
            return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._transactions.connection = db
            try:
                yield
            finally:
                self._transactions.connection = None

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

    def notes(self, search="", status="all", document_id=None, include_ignored=False, order="latest"):
        where, values = ["n.archived=0"], []
        if status == "ignored":
            where.append("n.ignored=1")
        elif not include_ignored:
            where.append("n.ignored=0")
        if search:
            where.append("(n.title LIKE ? OR n.transcript LIKE ? OR n.question LIKE ? OR n.tags LIKE ? OR a.payload LIKE ?)")
            values += ["%" + search + "%"] * 5
        if status == "unread":
            where.append("n.unread=1")
        elif status == "pending":
            where.append("n.status IN ('pending','recognized','error')")
        elif status not in ("all", "ignored"):
            where.append("n.status=?")
            values.append(status)
        if document_id:
            where.append("n.document_id=?")
            values.append(document_id)
        ordering = "julianday(n.first_seen),n.id" if order == "thread" else "n.unread DESC,n.time_trusted DESC,COALESCE(n.device_modified,n.last_seen) DESC,n.id DESC"
        rows = self.query("""SELECT n.*,d.title AS document_title,d.name AS document_name,
            d.remote_path,a.payload AS answer_payload,a.model AS answer_model,a.provenance AS answer_provenance,
            a.created_at AS answered_at FROM notes n JOIN documents d ON d.id=n.document_id
            LEFT JOIN answers a ON a.id=n.answer_id WHERE """ + " AND ".join(where) + " ORDER BY "+ordering, values)
        return [self.decode_note(row) for row in rows]

    def channels(self, search="", status="all"):
        conditions, values = [], [1 if status == "ignored" else 0]
        if search:
            conditions.append("(d.title LIKE ? OR d.name LIKE ? OR EXISTS (SELECT 1 FROM visible v WHERE v.document_id=d.id AND (v.title LIKE ? OR v.transcript LIKE ? OR v.question LIKE ? OR v.tags LIKE ? OR v.payload LIKE ?)))")
            values += ["%"+search+"%"]*7
        filters = {"unread": "SUM(n.unread)>0", "pending": "SUM(n.status IN ('pending','recognized','error'))>0", "needs_review": "SUM(n.status='needs_review')>0"}
        rows = self.query("""WITH visible AS (
            SELECT n.*,a.payload FROM notes n LEFT JOIN answers a ON a.id=n.answer_id
            WHERE n.archived=0 AND n.ignored=?
        ), ranked AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY document_id ORDER BY julianday(COALESCE(activity_at,first_seen)) DESC,id DESC) AS rank
            FROM visible
        ) SELECT d.id,d.title,d.name,d.remote_path,d.device_present,d.tracked,
            COUNT(*) AS notes,SUM(n.unread) AS unread,SUM(n.status='needs_review') AS needs_review,
            SUM(n.status IN ('pending','recognized','error')) AS pending,
            MAX(CASE WHEN n.rank=1 THEN COALESCE(n.activity_at,n.first_seen) END) AS last_activity,
            MAX(CASE WHEN n.rank=1 THEN COALESCE(NULLIF(n.question,''),NULLIF(n.transcript,''),n.title) END) AS last_snippet,
            MAX(CASE WHEN n.rank=1 AND n.status='answered' THEN n.payload END) AS latest_answer
            FROM ranked n JOIN documents d ON d.id=n.document_id"""+
            (" WHERE "+" AND ".join(conditions) if conditions else "")+" GROUP BY d.id"+
            (" HAVING "+filters[status] if status in filters else "")+
            " ORDER BY (SUM(n.unread)>0) DESC,julianday(last_activity) DESC,d.id", values)
        for row in rows:
            answer = json.loads(row.pop("latest_answer") or "null")
            if isinstance(answer, dict) and isinstance(answer.get("summary"), str):
                row["last_snippet"] = answer["summary"]
            row["last_snippet"] = (row["last_snippet"] or "")[:200]
        return rows

    def mark_answers_read(self, document_id, answer_ids):
        if not answer_ids:
            return []
        placeholders = ",".join("?" for _ in answer_ids)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            condition = "document_id=? AND archived=0 AND ignored=0 AND unread=1 AND answer_id IN ("+placeholders+")"
            rows = db.execute("SELECT answer_id FROM notes WHERE "+condition, (document_id,*answer_ids)).fetchall()
            db.execute("UPDATE notes SET unread=0 WHERE "+condition, (document_id,*answer_ids))
        return [row["answer_id"] for row in rows]

    @staticmethod
    def decode_note(row):
        row["tags"] = json.loads(row["tags"] or "[]")
        row["annotation_ids"] = json.loads(row["annotation_ids"])
        row["question_refs"] = json.loads(row["question_refs"])
        row["scope_conflicts"] = json.loads(row["scope_conflicts"])
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
        result["revisions"] = self.query("SELECT id,reason,payload,created_at FROM note_revisions WHERE note_id=? ORDER BY id DESC", (identifier,))
        for revision in result["revisions"]:
            revision["note"] = json.loads(revision.pop("payload"))
        result["lineage"] = self.query("""SELECT l.parent_id,l.child_id,l.relation,l.snapshot_id,
            n.id,n.title,n.archived,n.transcript,n.question,n.anchor,a.payload AS answer_payload
            FROM note_links l JOIN notes n
            ON n.id=CASE WHEN l.parent_id=? THEN l.child_id ELSE l.parent_id END
            LEFT JOIN answers a ON a.id=n.answer_id
            WHERE l.parent_id=? OR l.child_id=? ORDER BY l.snapshot_id DESC,n.id""", (identifier,identifier,identifier))
        for related in result["lineage"]:
            related["answer"] = json.loads(related.pop("answer_payload") or "null")
        return result

    def capture_revision(self, note, reason):
        fields = ("title", "transcript", "question", "anchor", "manual_transcript", "confirmation_stale",
                  "kind", "question_refs", "annotation_ids", "content_hash", "snapshot_id", "answer_id", "ignored")
        payload = {key: note.get(key) for key in fields}
        for key in ("question_refs", "annotation_ids"):
            if isinstance(payload[key], str):
                payload[key] = json.loads(payload[key])
        return self.execute("INSERT INTO note_revisions(note_id,reason,payload,created_at) VALUES(?,?,?,?)",
                            (note["id"], reason, encode(payload), now()))

    def save_answer(self, note_id, answer, model, provenance="model"):
        with self.db() as db:
            cursor = db.execute("INSERT INTO answers(note_id,payload,model,provenance,created_at) VALUES(?,?,?,?,?)", (note_id, encode(answer), model, provenance, now()))
            db.execute("UPDATE notes SET answer_id=?,status='answered',unread=1,error='',last_seen=?,activity_at=? WHERE id=?", (cursor.lastrowid, now(), now(), note_id))
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
            COALESCE(SUM(unread),0) AS unread FROM notes WHERE archived=0 AND ignored=0""")
        row["ignored"] = self.one("SELECT COUNT(*) AS count FROM notes WHERE archived=0 AND ignored=1")["count"]
        row["documents"] = self.one("SELECT COUNT(*) AS count FROM documents WHERE tracked=1")["count"]
        return row

    def backup(self, destination):
        with self.db() as db, sqlite3.connect(destination) as target:
            db.backup(target)
