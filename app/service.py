from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .device import DeviceClient, validate_address
from .llm import ModelClient, validate_model_url
from .pdf_notes import digest, ingest_pdf, render_note_images
from .note_scope import SCOPE_VERSION, recognition_scope, normalize_ref, normalize_refs, overlapping_refs, scope_answer
from .store import Store, encode, now
from .scope import folder_path, in_folders, normalize_folders
from .paths import ROOT, database_path

DEFAULTS = dict(device_address="", sync_clock=True, auto_sync=True,
                sync_interval=180, auto_analyze=True, model_base_url="", model_name="",
                model_api_key="", upload_folder="Document/收件箱", sync_folders=[], last_sync=None)
ANALYSIS_FIELDS = ("content_hash", "snapshot_id", "ignore_version", "transcript", "question", "anchor", "kind", "question_refs", "scope_status", "scope_version", "manual_transcript", "confirmation_stale")


def analysis_version(note):
    return digest([note[key] for key in ANALYSIS_FIELDS])


def document_id(remote):
    return hashlib.sha256(remote.encode()).hexdigest()[:24]


def fingerprint(entry):
    return digest([entry.get(key) for key in ("file_revision", "modified_date", "file_size")])


class Service:
    def __init__(self, data_dir, auth_dir, relay=None):
        self.data = Path(data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data.chmod(0o700)
        self.config_path = self.data/"settings.json"
        self.config_lock = threading.RLock()
        self.config = dict(DEFAULTS)
        if os.environ.get("DPT_ADDRESS"):
            self.config["device_address"] = validate_address(os.environ["DPT_ADDRESS"])
        previous = {}
        if self.config_path.exists():
            previous = json.loads(self.config_path.read_text())
            self.config.update(previous)
        self.config["sync_folders"] = normalize_folders(self.config["sync_folders"])
        self.store = Store(database_path(self.data))
        self.device = DeviceClient(self.settings, auth_dir, relay)
        self.model = ModelClient(self.settings)
        self.tasks = queue.Queue()
        self.model_tasks = queue.Queue()
        self.note_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pending = set()
        self.pending_lock = threading.Lock()
        self.pin_event = threading.Event()
        self.pin = None
        self.pairing_job = None
        self.online = None
        old_tracking = self.store.one("SELECT COUNT(*) AS count FROM documents WHERE tracked=1")["count"]
        self._refresh_tracking()
        if "sync_folders" not in previous and (previous or old_tracking):
            self._write_settings()
            self.store.event("settings", "已切换为按文件夹同步，请先选择同步文件夹；已有笔记和解答已保留")

    def settings(self):
        with self.config_lock:
            return dict(self.config, sync_folders=list(self.config["sync_folders"]))

    def public_settings(self):
        result = self.settings()
        result["has_api_key"] = bool(result.pop("model_api_key", ""))
        result["model_ready"] = self.model.ready()
        result["paired"] = (self.device.auth_dir/"privatekey.dat").exists()
        return result

    def save_settings(self, values):
        allowed = set(DEFAULTS)-{"last_sync"}
        if not set(values) <= allowed:
            raise ValueError("不支持的设置项")
        if "device_address" in values:
            values["device_address"] = validate_address(values["device_address"])
        if "model_base_url" in values and values["model_base_url"]:
            values["model_base_url"] = validate_model_url(values["model_base_url"])
        if "sync_interval" in values:
            values["sync_interval"] = max(60, min(3600, int(values["sync_interval"])))
        if "sync_folders" in values:
            values["sync_folders"] = normalize_folders(values["sync_folders"])
        if "upload_folder" in values:
            values["upload_folder"] = folder_path(values["upload_folder"])
        for key in ("auto_sync", "sync_clock", "auto_analyze"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError("开关设置需要布尔值")
        for key in ("model_name", "model_api_key", "model_base_url", "upload_folder"):
            if key in values and (not isinstance(values[key], str) or len(values[key]) > 2000):
                raise ValueError("设置内容过长或格式不正确")
        with self.note_lock, self.config_lock:
            updated = dict(self.config, **values)
            if updated["sync_folders"] and not in_folders(updated["upload_folder"], updated["sync_folders"]):
                if "sync_folders" in values:
                    updated["upload_folder"] = updated["sync_folders"][0]
                elif "upload_folder" in values:
                    raise ValueError("上传目录需要位于已选择的同步文件夹内")
            self.config = updated
            self._write_settings()
            self._refresh_tracking()
        return self.public_settings()

    def in_scope(self, path):
        return in_folders(path, self.settings()["sync_folders"])

    def document_active(self, identifier):
        doc = self.store.document(identifier)
        return bool(doc and doc["device_present"] and self.in_scope(doc["remote_path"]))

    def _refresh_tracking(self):
        with self.store.db() as db:
            for row in db.execute("SELECT id,remote_path,tracked,device_present FROM documents").fetchall():
                tracked = int(bool(row["device_present"]) and self.in_scope(row["remote_path"]))
                db.execute("UPDATE documents SET tracked=?,fingerprint=CASE WHEN tracked=0 AND ?=1 THEN NULL ELSE fingerprint END WHERE id=?", (tracked, tracked, row["id"]))

    def _write_settings(self):
        temporary = self.config_path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.config, file, ensure_ascii=False)
        temporary.replace(self.config_path)
        self.config_path.chmod(0o600)

    def upsert_document(self, entry, tracked=None):
        remote = entry["entry_path"]
        name = Path(remote).name
        old = self.store.document_at(remote)
        if not old and entry.get("entry_id"):
            old = self.store.one("""SELECT d.* FROM documents d WHERE device_entry_id=?
                ORDER BY (SELECT COUNT(*) FROM notes n WHERE n.document_id=d.id) DESC,d.created_at LIMIT 1""", (entry["entry_id"],))
        identifier = old["id"] if old else document_id(remote)
        if not old and self.store.document(identifier):
            # A previous file may have moved away from the same original path.
            identifier = uuid.uuid4().hex[:24]
        track = int(self.in_scope(remote))
        self.store.execute("""INSERT INTO documents(id,remote_path,name,title,pages,tracked,
            device_modified,revision,file_size,metadata,last_seen,created_at,device_entry_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            remote_path=excluded.remote_path,device_entry_id=excluded.device_entry_id,
            title=excluded.title,pages=excluded.pages,tracked=excluded.tracked,
            device_modified=excluded.device_modified,revision=excluded.revision,
            file_size=excluded.file_size,metadata=excluded.metadata,last_seen=excluded.last_seen,device_present=1""",
            (identifier, remote, name, entry.get("title") or name, int(entry.get("total_page") or 0), track,
             entry.get("modified_date"), entry.get("file_revision"), int(entry.get("file_size") or 0),
             encode(entry), now(), now(), entry.get("entry_id")))
        return identifier

    def documents(self):
        return self.store.query("""SELECT d.id,d.name,d.title,d.remote_path,d.pages,d.tracked,
            d.device_modified,d.file_size,d.snapshot_id,d.device_present,d.baseline_path IS NOT NULL AS has_baseline,
            (SELECT COUNT(*) FROM notes n WHERE n.document_id=d.id AND n.archived=0) AS notes
            FROM documents d ORDER BY d.tracked DESC,d.remote_path""")

    def set_tracking(self, identifier, tracked):
        raise ValueError("同步范围已改为文件夹，请在设备文件中选择同步文件夹")

    def folders(self):
        paths = {"Document"} | set(self.settings()["sync_folders"])
        paths.update(row["path"] for row in self.store.query("SELECT path FROM folders"))
        docs = self.store.query("SELECT remote_path FROM documents WHERE device_present=1")
        for doc in docs:
            parts = doc["remote_path"].split("/")
            paths.update("/".join(parts[:index]) for index in range(1, len(parts)))
        return [dict(path=path, documents=sum(in_folders(doc["remote_path"], [path]) for doc in docs)) for path in sorted(paths)]

    def update_inventory(self, entries):
        self.store.execute("UPDATE documents SET device_present=0,tracked=0")
        self.store.execute("DELETE FROM folders")
        documents = []
        for entry in entries:
            remote = entry.get("entry_path", "")
            if entry.get("entry_type") == "folder" and remote:
                try:
                    remote = folder_path(remote)
                except ValueError:
                    continue
                self.store.execute("INSERT OR REPLACE INTO folders(path,entry_id,last_seen) VALUES(?,?,?)", (remote, entry.get("entry_id"), now()))
            elif entry.get("entry_type") == "document" and remote.lower().endswith(".pdf"):
                self.upsert_document(entry)
                documents.append(entry)
        return documents

    def refresh_inventory(self):
        with self.device.lock:
            device = self.device.connect()
            raw = self.device.inventory(device)
            with self.note_lock:
                entries = self.update_inventory(raw)
            self.online = True
        return {"message": f"目录已更新，共 {len(entries)} 份 PDF", "folders": len(self.folders())}

    def enqueue(self, kind, argument=None):
        key = (kind, str(argument) if kind in ("analyze", "pull", "upload", "mkdir") else "")
        with self.pending_lock:
            if key in self.pending:
                return {"queued": False, "message": "同一任务正在处理中"}
            job = self.store.job(kind)
            self.pending.add(key)
            work_queue = self.model_tasks if kind in ("analyze", "model_test") else self.tasks
            work_queue.put((job, kind, argument, key))
        return {"queued": True, "job_id": job}

    def start(self):
        threading.Thread(target=self._worker, args=(self.tasks,), daemon=True, name="dpt-device").start()
        threading.Thread(target=self._worker, args=(self.model_tasks,), daemon=True, name="dpt-model").start()
        threading.Thread(target=self._scheduler, daemon=True, name="dpt-sync").start()

    def _scheduler(self):
        # Wait before the first poll so settings and imported notes are available immediately.
        while not self.stop_event.wait(self.settings()["sync_interval"]):
            if self.settings()["auto_sync"] and self.settings()["sync_folders"]:
                self.enqueue("sync", False)

    def _worker(self, work_queue):
        while not self.stop_event.is_set():
            try:
                job, kind, argument, key = work_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.store.update_job(job, status="running", message="正在处理")
                if kind == "sync":
                    result = self.sync(job, bool(argument))
                elif kind == "analyze":
                    result = self.analyze(int(argument))
                elif kind == "pull":
                    result = self.sync(job, False, only=str(argument))
                elif kind == "upload":
                    result = self.upload(argument, job)
                elif kind == "inventory":
                    result = self.refresh_inventory()
                elif kind == "mkdir":
                    result = self.create_folder(argument)
                elif kind == "move":
                    result = self.move_documents(argument["ids"], argument["folder"], job)
                elif kind == "probe":
                    result = self.device.probe()
                    self.online = result["online"]
                    if not self.online:
                        raise RuntimeError("DPT 无回应，请唤醒设备并检查 Wi-Fi / IP")
                elif kind == "model_test":
                    result = self.model.test()
                elif kind == "pair":
                    result = self.pair(job)
                else:
                    raise ValueError("未知任务")
                self.store.update_job(job, status="error" if kind == "move" and result.get("errors") else "complete", message=result.get("message", "已完成"), result=result)
            except Exception as error:
                # Do not expose request bodies, authorization headers or local key paths.
                detail = self.safe_error(error)
                if kind in ("sync", "pull", "probe", "inventory", "mkdir", "move"):
                    self.online = False
                self.store.update_job(job, status="error", message=detail)
                self.store.event("error", detail)
            finally:
                with self.pending_lock:
                    self.pending.discard(key)
                work_queue.task_done()

    def safe_error(self, error):
        message = str(error)[:700] or type(error).__name__
        secret = self.settings().get("model_api_key")
        if secret:
            message = message.replace(secret, "[redacted]")
        return message

    def sync(self, job=None, manual=True, only=None):
        if not self.settings()["sync_folders"]:
            raise ValueError("请先在设备文件中选择同步文件夹")
        if only:
            doc = self.store.document(only)
            if not doc or not self.in_scope(doc["remote_path"]):
                raise ValueError("这份文档不在同步范围，请先添加它所在的文件夹")
        changed = []
        pulled = 0
        errors = []
        with self.device.lock:
            if job:
                self.store.update_job(job, message="连接 DPT，读取文件版本")
            device = self.device.connect()
            self.online = True
            raw = self.device.inventory(device)
            with self.note_lock:
                entries = self.update_inventory(raw)
            tracked = [entry for entry in entries if self.in_scope(entry["entry_path"])]
            # Force the most recently read tracked document on manual sync: DPT metadata can lag handwriting.
            latest = max(tracked, key=lambda e: e.get("reading_date", ""), default={}).get("entry_path")
            targets = []
            for entry in entries:
                doc = self.store.document_at(entry["entry_path"])
                if not self.in_scope(doc["remote_path"]):
                    continue
                if (only and doc["id"] == only) or (not only and doc["tracked"] and
                        (doc["fingerprint"] != fingerprint(entry) or (manual and entry["entry_path"] == latest))):
                    targets.append((entry, doc))
            for index, (entry, doc) in enumerate(targets):
                if job:
                    self.store.update_job(job, message="拉取 "+doc["name"], progress=index, total=len(targets))
                temporary = self.data/"incoming"/(doc["id"]+".pdf")
                try:
                    if not self.in_scope(doc["remote_path"]):
                        continue
                    self.device.download(device, entry, temporary)
                    with self.note_lock:
                        if not self.in_scope(doc["remote_path"]):
                            continue
                        result = ingest_pdf(self.store, self.data, doc["id"], temporary)
                    changed += result["notes"]
                    pulled += 1
                    self.store.execute("UPDATE documents SET fingerprint=? WHERE id=?", (fingerprint(entry), doc["id"]))
                except Exception as error:
                    errors.append(doc["name"]+"："+self.safe_error(error))
                finally:
                    temporary.unlink(missing_ok=True)
            if job:
                self.store.update_job(job, progress=len(targets), total=len(targets))
        with self.config_lock:
            self.config["last_sync"] = now()
            self._write_settings()
        # Recover pending notes after adding API settings or a previous model outage too.
        if self.settings()["auto_analyze"] and self.model.ready():
            current = {self.store.document_at(entry["entry_path"])["id"] for entry in tracked}
            for note in self.store.notes(status="pending"):
                if note["document_id"] in current and self.in_scope(note["remote_path"]):
                    self.enqueue("analyze", note["id"])
        message = f"拉取 {pulled} 份文件，发现 {len(changed)} 处新笔记或修改"
        if errors:
            message += f"；{len(errors)} 份失败，可重试"
        self.store.event("sync", message)
        return dict(message=message, files=pulled, changed_notes=changed, errors=errors)

    def create_folder(self, path):
        path = folder_path(path)
        with self.device.lock:
            device = self.device.connect()
            info = self.device.ensure_folder(device, path)
            self.store.execute("INSERT OR REPLACE INTO folders(path,entry_id,last_seen) VALUES(?,?,?)", (path, info["entry_id"], now()))
            self.online = True
        self.store.event("folder", "文件夹已就绪："+path)
        return {"message": "文件夹已就绪："+path, "path": path}

    def validate_move(self, identifiers, destination):
        destination = folder_path(destination)
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 1000:
            raise ValueError("请选择 1 至 1000 份文档")
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{24}", value) for value in identifiers):
            raise ValueError("无效的文档选择")
        identifiers = list(dict.fromkeys(identifiers))
        if any(not self.store.document(value) for value in identifiers):
            raise ValueError("选择中包含不存在的文档")
        return identifiers, destination

    def move_documents(self, identifiers, destination, job=None):
        identifiers, destination = self.validate_move(identifiers, destination)
        moved, skipped, errors = [], [], []
        with self.device.lock:
            device = self.device.connect()
            raw = self.device.inventory(device)
            with self.note_lock:
                current = self.update_inventory(raw)
            occupied = {entry["entry_path"]: entry for entry in current}
            plans, targets = [], set()
            # Reject conflicting destinations before any device file is moved.
            for identifier in identifiers:
                doc = self.store.document(identifier)
                if not doc["device_present"]:
                    raise ValueError("文档已从设备移除，请刷新目录："+doc["name"])
                target = destination+"/"+doc["name"]
                if doc["remote_path"] == target:
                    skipped.append(identifier)
                    continue
                if target in targets or target in occupied:
                    raise ValueError("目标存在同名文件，未移动任何文档："+doc["name"])
                history = self.store.document_at(target)
                if history and history["id"] != identifier:
                    raise ValueError("目标路径已有其他文档的归档，请选择其他目录："+doc["name"])
                targets.add(target)
                plans.append(doc)
            folder = self.device.ensure_folder(device, destination)
            batch = self.data/"move-backups"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
            if plans:
                batch.mkdir(parents=True, mode=0o700)
            for index, doc in enumerate(plans):
                if job:
                    self.store.update_job(job, message="备份并移动 "+doc["name"], progress=index, total=len(plans))
                before, after = batch/(doc["id"]+".before.pdf"), batch/(doc["id"]+".after.pdf")
                metadata = json.loads(doc["metadata"])
                record = dict(document_id=doc["id"], device_entry_id=doc["device_entry_id"],
                              source=doc["remote_path"], destination=destination+"/"+doc["name"], captured_at=now(), status="preparing")
                manifest = batch/(doc["id"]+".json")
                try:
                    # Explicit file management may back up out-of-scope files, but never ingests their notes.
                    self.device.download(device, metadata, before)
                    record["before_sha256"] = hashlib.sha256(before.read_bytes()).hexdigest()
                    record["status"] = "backed_up"
                    manifest.write_text(encode(record))
                    info = self.device.move_document(device, metadata, folder)
                    with self.note_lock:
                        moved_id = self.upsert_document(info)
                        if moved_id != doc["id"]:
                            raise ValueError("文档归档关联未通过检查，请刷新目录核对")
                        self.store.execute("UPDATE documents SET fingerprint=NULL WHERE id=?", (moved_id,))
                    record["status"] = "moved"
                    manifest.write_text(encode(record))
                    if job:
                        self.store.update_job(job, message="校验移动后的批注 "+doc["name"])
                    self.device.download(device, info, after)
                    record["after_sha256"] = hashlib.sha256(after.read_bytes()).hexdigest()
                    if record["after_sha256"] != record["before_sha256"]:
                        raise ValueError("移动前后 PDF 内容有变化，已保留两份副本，请核对")
                    after.unlink()
                    moved.append(doc["id"])
                    record["status"] = "verified"
                except Exception as error:
                    detail = doc["name"]+"："+self.safe_error(error)
                    errors.append(detail)
                    record.update(status="error", error=detail)
                finally:
                    manifest.write_text(encode(record))
            # A timeout may occur after the device has moved a file: reconcile by its stable device ID.
            try:
                raw = self.device.inventory(device)
                with self.note_lock:
                    self.update_inventory(raw)
                self.online = True
            except Exception as error:
                errors.append("目录刷新失败，请重新刷新设备目录："+self.safe_error(error))
                self.online = False
            if job:
                self.store.update_job(job, progress=len(plans), total=len(plans))
        message = f"移动并校验 {len(moved)} 份文档，{len(skipped)} 份已在目标目录"
        if errors:
            message += f"；{len(errors)} 项未完成："+errors[0]
        self.store.event("move", message)
        return dict(message=message, moved=moved, skipped=skipped, errors=errors, destination=destination)

    def edit_note(self, identifier, values):
        note = self.store.note(identifier)
        if not note:
            raise ValueError("笔记不存在")
        if note["ignored"]:
            raise ValueError("请先恢复这条笔记，再校正或解读")
        fields = {key: str(values[key])[:8000] for key in ("transcript", "question", "anchor", "title") if key in values}
        if not fields:
            raise ValueError("没有可保存的内容")
        refs = None
        if "question_refs" in values:
            raw = values["question_refs"]
            if isinstance(raw, str):
                raw = [value.strip() for value in re.split(r"[,，\s]+", raw) if value.strip()]
            refs = normalize_refs(raw)
            if not isinstance(raw, list) or any(not normalize_ref(value) for value in raw):
                raise ValueError("题号格式无效，请用逗号分隔；分节题号使用 2:8")
        with self.note_lock, self.store.transaction():
            note = self.store.note(identifier)
            if note["confirmation_stale"] or any(note[key] != value for key, value in fields.items()) or refs is not None and refs != note["question_refs"]:
                self.store.capture_revision(note, "manual_edit")
            self.store.execute("UPDATE notes SET "+",".join(key+"=?" for key in fields)+
                               ",manual_transcript=1,confirmation_stale=0,status='recognized',confidence=1,error='',unread=0,scope_status='unknown',scope_version=0,question_refs='[]',scope_conflicts='[]',activity_at=? WHERE id=?", (*fields.values(), now(), identifier))
            if refs is not None:
                self.store.execute("UPDATE notes SET question_refs=?,scope_status=?,scope_version=? WHERE id=?",
                                   (encode(refs), "known" if refs else "unknown", SCOPE_VERSION, identifier))
        return self.store.note(identifier)

    def ignore_note(self, identifier, ignored):
        if not isinstance(ignored, bool):
            raise ValueError("请选择是否忽略笔记")
        with self.note_lock:
            note = self.store.note(identifier)
            if not note or note["archived"]:
                raise ValueError("笔记已移除或不存在")
            if bool(note["ignored"]) != ignored:
                self.store.execute("UPDATE notes SET ignored=?,ignore_version=ignore_version+1,unread=0 WHERE id=?", (int(ignored),identifier))
                self.store.event("ignore" if ignored else "restore", "已忽略一条笔记" if ignored else "已恢复一条笔记", identifier)
        return self.store.note(identifier)

    def analyze(self, identifier):
        with self.note_lock:
            note = self.store.note(identifier)
            if not note or note["archived"]:
                raise ValueError("笔记已移除或不存在")
            if note["ignored"]:
                return {"message": "笔记已忽略，已跳过解读"}
            if not self.document_active(note["document_id"]):
                return {"message": "文档不在当前同步范围，已跳过解读"}
            if note["confirmation_stale"]:
                return {"message": "笔迹已变更，请先核对并确认转写；先前文字和解答已保留", "needs_review": True}
        start_hash = analysis_version(note)
        recognition = None
        try:
            snapshot = self.store.one("SELECT sha256,pdf_path FROM snapshots WHERE id=? AND document_id=?", (note["snapshot_id"], note["document_id"]))
            if not snapshot:
                raise ValueError("缺少笔记对应的 PDF 版本，请重新同步文档")
            images = render_note_images(snapshot["pdf_path"], note, self.data, snapshot["sha256"])
            note.update(images)
            with self.note_lock:
                current = self.store.note(identifier)
                if current["archived"] or current["ignored"] or not self.document_active(note["document_id"]) or analysis_version(current)!=start_hash:
                    return {"message": "笔记或同步范围已更新，已跳过解读"}
            needs_scope = note["scope_version"] != SCOPE_VERSION or note["scope_status"] == "unknown"
            if (not note["manual_transcript"] and (needs_scope or note["status"] not in ("recognized", "answered"))) or (note["manual_transcript"] and note["kind"] == "answer" and needs_scope):
                recognition = self.model.recognize(note)
                with self.note_lock:
                    current = self.store.note(identifier)
                    if current["archived"] or current["ignored"] or not self.document_active(current["document_id"]) or analysis_version(current) != start_hash:
                        return {"message": "笔记已更新，本次识别结果已丢弃，请重新解读"}
                    refs, scope_status = recognition_scope(recognition)
                    status = "recognized" if recognition["confidence"] >= .85 else "needs_review"
                    if recognition["kind"] == "answer" and scope_status != "known":
                        status = "needs_review"
                    if note["manual_transcript"]:
                        # Scope discovery must never overwrite human-confirmed wording.
                        for field in ("transcript", "question", "anchor", "title", "kind"):
                            recognition[field] = note[field]
                        if note["kind"] == "answer" and scope_status != "known":
                            status = "needs_review"
                    self.store.execute("""UPDATE notes SET transcript=?,question=?,anchor=?,title=?,kind=?,
                        confidence=?,tags=?,status=?,error='' WHERE id=?""", (recognition["transcript"], recognition["question"],
                        recognition["anchor"], recognition["title"] or "手写笔记", recognition["kind"], recognition["confidence"],
                        encode(recognition["tags"]), status, identifier))
                    self.store.execute("UPDATE notes SET question_refs=?,scope_status=?,scope_version=?,scope_conflicts='[]' WHERE id=?",
                                       (encode(refs), scope_status, SCOPE_VERSION, identifier))
                    note = self.store.note(identifier)
                    note.update(images)
                if status == "needs_review":
                    return {"message": "笔迹不够确定，请在笔记里核对转写后再解读", "needs_review": True}
            elif note["status"] == "needs_review" and not note["manual_transcript"]:
                return {"message": "请先确认转写文字", "needs_review": True}
            if not (note["question"].strip() or note["transcript"].strip() or note["anchor"].strip()):
                self.store.execute("UPDATE notes SET status='needs_review',error='请补充希望解读的问题' WHERE id=?", (identifier,))
                return {"message": "请补充希望解读的问题", "needs_review": True}
            before_answer = analysis_version(note)
            candidates = []
            for item in self.store.notes(status="answered"):
                if item["id"] == identifier or not item["answer"] or not self.in_scope(item["remote_path"]) or not self.store.document(item["document_id"])["device_present"]:
                    continue
                score = len(set(note["tags"]) & set(item["tags"]))
                if note["anchor"] and note["anchor"] in item["anchor"]+item["question"]:
                    score += 3
                if score:
                    candidates.append((score, item))
            related = [dict(title=item["title"], question=item["question"], summary=item["answer"]["summary"][:1200])
                       for _, item in sorted(candidates, key=lambda pair: -pair[0])[:4]]
            with self.note_lock:
                current = self.store.note(identifier)
                if current["archived"] or current["ignored"] or not self.document_active(note["document_id"]) or analysis_version(current)!=before_answer:
                    return {"message": "笔记或同步范围已更新，已跳过解读"}
                if self.scope_conflicts(current):
                    return {"message": "同页笔记的题目归属冲突，请核对", "needs_review": True}
            answer = scope_answer(self.model.answer(note, related), note)
            with self.note_lock:
                current = self.store.note(identifier)
                if current["archived"] or current["ignored"] or not self.document_active(current["document_id"]) or analysis_version(current) != before_answer:
                    return {"message": "笔记已更新，本次解读已丢弃，请重新解读"}
                if self.scope_conflicts(current):
                    return {"message": "同页笔记的题目归属冲突，请核对", "needs_review": True}
                accepted_refs = {r["question_ref"] for r in answer.get("exercise_reviews", [])}
                if note["kind"] == "answer" and (not accepted_refs or set(normalize_refs(note["question_refs"])) != accepted_refs):
                    self.store.execute("UPDATE notes SET status='needs_review',error='作答范围未完整核对，请重新识别或核对目标题目' WHERE id=?", (identifier,))
                    return {"message": "作答范围未完整核对", "needs_review": True}
                self.store.save_answer(identifier, answer, self.settings()["model_name"])
            return {"message": "解读完成，已加入知识库", "note_id": identifier}
        except Exception as error:
            with self.note_lock:
                current = self.store.note(identifier)
                if current and not current["archived"] and not current["ignored"] and self.document_active(current["document_id"]) and analysis_version(current)==analysis_version(note):
                    self.store.execute("UPDATE notes SET status='error',error=? WHERE id=?", (self.safe_error(error), identifier))
            raise

    def scope_conflicts(self, note):
        """Called under note_lock before inference and again before publishing."""
        if note["scope_version"] != SCOPE_VERSION or note["scope_status"] != "known":
            return []
        refs = set(normalize_refs(note["question_refs"]))
        conflicts = []
        for other in self.store.query("SELECT id,question_refs FROM notes WHERE document_id=? AND page=? AND id!=? AND archived=0 AND ignored=0 AND scope_version=? AND scope_status='known'",
                                      (note["document_id"], note["page"], note["id"], SCOPE_VERSION)):
            if overlapping_refs(list(refs), json.loads(other["question_refs"])):
                conflicts.append(other["id"])
        if conflicts:
            # Preserve old answers as history, but withdraw both claims from current answers.
            for identifier in [note["id"], *conflicts]:
                current = self.store.note(identifier)
                peers = set(current["scope_conflicts"]) | ({note["id"]} if identifier != note["id"] else set(conflicts))
                self.store.execute("UPDATE notes SET status='needs_review',unread=0,scope_conflicts=?,error='同页笔记的题目归属冲突，请核对目标笔迹' WHERE id=?",
                                   (encode(sorted(peers)), identifier))
        elif note["scope_conflicts"]:
            self.store.execute("UPDATE notes SET scope_conflicts='[]' WHERE id=?", (note["id"],))
        return conflicts

    def upload(self, path, job=None):
        path = Path(path)
        try:
            folder = self.settings()["upload_folder"]
            if not self.in_scope(folder):
                raise ValueError("请先选择同步文件夹，并把上传目录设在同步范围内")
            if job:
                self.store.update_job(job, message="上传 PDF，同名文件会另存")
            entry = self.device.upload(path, folder)
            identifier = self.upsert_document(entry)
            # Read it back before calling an upload verified.
            result = self.sync(None, False, only=identifier)
            if result["errors"]:
                raise RuntimeError("文件已上传，但回读校验失败："+"；".join(result["errors"]))
            snapshot = self.store.one("SELECT s.* FROM snapshots s JOIN documents d ON d.snapshot_id=s.id WHERE d.id=?", (identifier,))
            if not snapshot or snapshot["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise RuntimeError("文件已上传，但设备回读内容发生变化，请在文件列表核对")
            return {"message": "上传完成，设备回读校验通过", "document_id": identifier}
        finally:
            path.unlink(missing_ok=True)
            if path.parent.parent == self.data/"uploads":
                path.parent.rmdir()

    def pair(self, job):
        self.pin = None
        self.pin_event.clear()
        self.pairing_job = job
        def request_pin():
            self.store.update_job(job, status="waiting_pin", message="请在设置里输入 DPT 屏幕上的配对码")
            if not self.pin_event.wait(180):
                raise TimeoutError("配对码等待超时，请重新开始配对")
            return self.pin
        try:
            self.device.pair(request_pin)
            return {"message": "配对成功，可以同步笔记"}
        finally:
            self.pin = None
            self.pairing_job = None

    def submit_pin(self, pin):
        if not self.pairing_job or not re.fullmatch(r"\d{4,12}", str(pin)):
            raise ValueError("当前没有等待配对，或配对码格式不正确")
        self.pin = str(pin)
        self.pin_event.set()

    def report(self, days=7):
        boundary = datetime.now(timezone.utc)-timedelta(days=days)
        notes = [note for note in self.store.notes() if self.in_scope(note["remote_path"])]
        recent = []
        for note in notes:
            if note["time_trusted"] and note["device_modified"]:
                try:
                    modified = datetime.fromisoformat(note["device_modified"].replace("Z", "+00:00"))
                    if modified.tzinfo and boundary <= modified <= datetime.now(timezone.utc)+timedelta(minutes=2):
                        recent.append(note)
                except ValueError:
                    pass
        uncertain = [note for note in notes if not note["time_trusted"]]
        topics = {}
        for note in recent:
            for tag in note["tags"]:
                topics[tag] = topics.get(tag, 0)+1
        return dict(days=days, generated_at=now(), recent_notes=len(recent), answered=sum(n["status"] == "answered" for n in recent),
                    needs_review=sum(n["status"] == "needs_review" for n in notes), uncertain_time=len(uncertain),
                    topics=sorted(topics.items(), key=lambda item: -item[1]),
                    next_steps=[dict(id=n["id"], title=n["title"], reason="核对手写转写" if n["status"] == "needs_review" else "解答这条笔记的疑问") for n in notes if n["status"] != "answered"][:8],
                    limitation="回顾仅统计可信的批注修改记录，不推断阅读时长、任务完成或知识掌握程度。异常日期的历史笔记不归入本期。")

    def export(self):
        return dict(exported_at=now(), notes=self.store.notes(include_ignored=True), report=self.report())

    def state(self):
        return dict(stats=self.store.stats(), settings=self.public_settings(), online=self.online,
                    jobs=self.store.query("SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT 8"),
                    events=self.store.query("SELECT * FROM events ORDER BY id DESC LIMIT 8"))
