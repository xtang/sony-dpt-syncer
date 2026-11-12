from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pypdf import PdfReader

from .bootstrap import relocate
from .paths import database_path, default_data_dir
from .service import ROOT, Service
from .web_auth import WebAuth
from .scope import in_folders
from .env import load_env

STATIC = Path(__file__).parent/"static"


def public_note(note, folders=None):
    if not note:
        return None
    result = {key: value for key, value in note.items() if key != "preview_path"}
    result["preview_url"] = f"/media/preview/{note['id']}" if note.get("preview_path") else None
    result["pdf_url"] = f"/media/document/{note['document_id']}#page={note['page']}"
    if folders is not None:
        result["in_sync_scope"] = in_folders(note["remote_path"], folders)
    return result


def markdown_export(service):
    lines = ["# 文档笔记归档", "", "导出自墨记。手写识别与模型解答可能需要核对。", ""]
    for note in service.store.notes(include_ignored=True):
        lines += ["## "+note["title"], "", f"来源：{note['document_title']}，第 {note['page']} 页", "",
                  "笔迹："+note["transcript"], "", "问题："+note["question"], ""]
        if note["ignored"]:
            lines += ["状态：已忽略（保留归档，可恢复）", ""]
        if note["answer"]:
            answer = note["answer"]
            lines += [answer["summary"], "", answer["explanation"], ""]
            reviews = answer.get("exercise_reviews", [])
            for review in reviews if isinstance(reviews, list) else []:
                if not isinstance(review, dict):
                    continue
                verdict = {"correct": "模型判断：答对", "incorrect": "模型判断：答错"}.get(review.get("verdict"), "暂不能判定")
                basis = {"document": "依据文中答案（模型核对）", "reasoning": "模型推导的参考解答"}.get(review.get("basis"), "需要补充题目或作答信息")
                lines += ["### "+str(review.get("question_ref") or "当前题目"), "", verdict, "", str(review.get("question", "")), "",
                          "你的作答："+str(review.get("user_answer") or "待确认"), "", "建议答案："+str(review.get("suggested_answer") or "待补充"), "",
                          basis, "", str(review.get("explanation", "")), ""]
                if review.get("reference_quote"):
                    lines += ["原文答案引用："+str(review["reference_quote"]), ""]
                points = review.get("knowledge_points", [])
                if isinstance(points, list):
                    lines += ["- "+point for point in points if isinstance(point, str)]
            for example in answer.get("examples", []):
                if isinstance(example, dict):
                    lines += ["- "+str(example.get("text", example.get("ja", "")))+" — "+str(example.get("explanation", example.get("zh", "")))]
            for source in answer.get("sources", []):
                lines += ["", "参考："+str(source)]
            lines += ["", "来源与生成方式："+note["answer_model"], ""]
        else:
            lines += ["尚未解读。", ""]
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "DPTNotes/2.0"
    protocol_version = "HTTP/1.1"

    @property
    def service(self):
        return self.server.service

    @property
    def web_auth(self):
        return getattr(self.server, "web_auth", None)

    def log_message(self, format, *args):
        # Paths may contain user input; never log body, query string, PIN or API key.
        return

    def guard(self, mutation=False):
        host = self.headers.get("Host", "")
        try:
            parsed_host = urlsplit("http://"+host)
            hostname = parsed_host.hostname
            port = parsed_host.port
        except ValueError:
            raise PermissionError("无效的访问地址")
        allowed = {"localhost", "127.0.0.1", "::1"} | self.server.allowed_hosts
        if not hostname or hostname not in allowed or (port or 80) != self.server.server_port:
            raise PermissionError("请使用已配置的服务器地址访问")
        if mutation:
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/") != "http://"+host:
                raise PermissionError("不接受来自其他网页的修改请求")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise PermissionError("不接受跨站请求")

    def send_bytes(self, data, content_type="application/json; charset=utf-8", status=200, attachment=None, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="'+attachment+'"')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), status=status)

    def file(self, path, content_type=None, attachment=None):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError("文件暂未同步到服务器")
        # Local paths are selected exclusively from application-controlled rows/static allowlists.
        self.send_bytes(path.read_bytes(), content_type or mimetypes.guess_type(path)[0] or "application/octet-stream", attachment=attachment)

    def body(self, limit=1024*1024):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("请使用带长度的请求")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > limit:
            raise ValueError("请求内容过大")
        self.connection.settimeout(30)
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("请求未传输完整")
        return data

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求需要使用 JSON")
        result = json.loads(self.body(), parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
        if not isinstance(result, dict):
            raise ValueError("请求需要是 JSON 对象")
        return result

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, mutation):
        try:
            self.guard(mutation)
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            authenticated = not self.web_auth or self.web_auth.valid(self.headers.get("Cookie"))
            if not mutation and path == "/api/health":
                return self.json({"ok": True, "service": "dpt-syncer", "version": 1})
            if mutation and path == "/api/login":
                body = self.json_body()
                if not self.web_auth:
                    return self.json({"ok": True})
                try:
                    token = self.web_auth.login(body.get("password"), getattr(self, "client_address", ("local", 0))[0])
                except PermissionError as error:
                    return self.json({"error": str(error)}, 401)
                return self.send_bytes(b'{"ok":true}', extra_headers={"Set-Cookie": self.web_auth.cookie(token)})
            if not mutation and path in ("/login.js", "/styles.css"):
                return self.file(STATIC/path[1:])
            if not authenticated:
                if not mutation and path in ("/", "/index.html"):
                    return self.file(STATIC/"login.html")
                self.close_connection = True
                if not mutation and path.startswith("/media/"):
                    return self.send_bytes(b"", "text/plain", status=303, extra_headers={"Location": "/"})
                return self.json({"error": "请先输入网页访问口令"}, 401)
            if mutation and path == "/api/logout":
                self.json_body()
                return self.send_bytes(b'{"ok":true}', extra_headers={"Set-Cookie": WebAuth.cookie("", clear=True)})
            if mutation:
                return self.post(path)
            if path in ("/", "/index.html", "/app.js", "/styles.css"):
                return self.file(STATIC/("index.html" if path == "/" else path[1:]))
            if path == "/api/state":
                return self.json(dict(self.service.state(), auth_required=bool(self.web_auth)))
            if path == "/api/notes":
                document = query.get("document_id", [None])[0]
                if document and not re.fullmatch(r"[a-f0-9]{24}", document):
                    raise ValueError("无效的文档标识")
                return self.json([public_note(note, self.service.settings()["sync_folders"]) for note in self.service.store.notes(
                    query.get("q", [""])[0][:200], query.get("status", ["all"])[0], document,
                    order=query.get("order", ["latest"])[0])])
            if path == "/api/channels":
                channels = self.service.store.channels(query.get("q", [""])[0][:200], query.get("status", ["all"])[0])
                for channel in channels:
                    channel["in_sync_scope"] = self.service.in_scope(channel["remote_path"])
                return self.json(channels)
            match = re.fullmatch(r"/api/notes/(\d+)", path)
            if match:
                note = self.service.store.note(int(match[1]))
                if not note:
                    raise FileNotFoundError("笔记不存在")
                return self.json(public_note(note, self.service.settings()["sync_folders"]))
            if path == "/api/documents":
                return self.json(self.service.documents())
            if path == "/api/folders":
                return self.json(self.service.folders())
            if path == "/api/report":
                return self.json(self.service.report(max(1, min(365, int(query.get("days", [7])[0])))))
            if path == "/api/export.json":
                result = self.service.export()
                result["notes"] = [public_note(note) for note in result["notes"]]
                return self.send_bytes(json.dumps(result, ensure_ascii=False, indent=2).encode(), attachment="dpt-notes.json")
            if path == "/api/export.md":
                return self.send_bytes(markdown_export(self.service).encode(), "text/markdown; charset=utf-8", attachment="dpt-notes.md")
            if path == "/api/backup.sqlite3":
                with tempfile.TemporaryDirectory(prefix="dpt-backup-") as folder:
                    backup = Path(folder)/"notes.sqlite3"
                    self.service.store.backup(backup)
                    return self.file(backup, "application/vnd.sqlite3", "dpt-archive.sqlite3")
            match = re.fullmatch(r"/api/answers/(\d+)", path)
            if match:
                answer = self.service.store.one("SELECT id,note_id,payload,model,provenance,created_at FROM answers WHERE id=?", (int(match[1]),))
                if not answer:
                    raise FileNotFoundError("解读不存在")
                answer["payload"] = json.loads(answer["payload"])
                return self.json(answer)
            match = re.fullmatch(r"/media/preview/(\d+)", path)
            if match:
                note = self.service.store.note(int(match[1]))
                if not note or not note["preview_path"]:
                    raise FileNotFoundError("截图不存在")
                return self.file(note["preview_path"], "image/png")
            match = re.fullmatch(r"/media/document/([a-f0-9]{24})", path)
            if match:
                doc = self.service.store.document(match[1])
                if not doc:
                    raise FileNotFoundError("文件不存在")
                snapshot = self.service.store.one("SELECT pdf_path FROM snapshots WHERE id=?", (doc["snapshot_id"],))
                target = snapshot["pdf_path"] if snapshot else doc["baseline_path"]
                if not target:
                    raise FileNotFoundError("请先在文件列表里拉取此文件")
                return self.file(target, "application/pdf")
            raise FileNotFoundError("页面不存在")
        except PermissionError as error:
            self.close_connection = True
            self.json({"error": str(error)}, 403)
        except FileNotFoundError as error:
            self.json({"error": str(error)}, 404)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self.json({"error": self.service.safe_error(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.json({"error": "处理失败，请检查服务日志或稍后重试"}, 500)
            traceback.print_exc()

    def post(self, path):
        if path == "/api/upload":
            if not self.service.in_scope(self.service.settings()["upload_folder"]):
                raise ValueError("请先在设备文件中选择同步文件夹")
            name = Path(unquote(self.headers.get("X-Filename", "upload.pdf"))).name
            if not name.lower().endswith(".pdf") or len(name) > 180 or any(ord(c) < 32 for c in name):
                raise ValueError("请选择 PDF 文件，文件名不超过 180 个字符")
            data = self.body(128*1024*1024)
            if not data.startswith(b"%PDF-"):
                raise ValueError("文件不是有效 PDF")
            folder = self.service.data/"uploads"/os.urandom(8).hex()
            folder.mkdir(parents=True, mode=0o700)
            target = folder/name
            target.write_bytes(data)
            try:
                reader = PdfReader(target)
                if reader.is_encrypted or not reader.pages:
                    raise ValueError("请选择未加密且有页面的 PDF")
            except Exception:
                target.unlink(missing_ok=True)
                folder.rmdir()
                raise ValueError("PDF 无法读取或已加密")
            return self.json(self.service.enqueue("upload", str(target)), 202)
        body = self.json_body()
        simple_jobs = {"/api/sync": ("sync", True), "/api/device/probe": ("probe", None),
                       "/api/device/inventory": ("inventory", None),
                       "/api/device/pair": ("pair", None), "/api/model/test": ("model_test", None)}
        if path in simple_jobs:
            return self.json(self.service.enqueue(*simple_jobs[path]), 202)
        if path == "/api/settings":
            return self.json(self.service.save_settings(body))
        if path == "/api/folders/create":
            from .scope import folder_path
            return self.json(self.service.enqueue("mkdir", folder_path(body.get("path"))), 202)
        if path == "/api/documents/move":
            identifiers, destination = self.service.validate_move(body.get("ids"), body.get("folder"))
            return self.json(self.service.enqueue("move", {"ids": identifiers, "folder": destination}), 202)
        if path == "/api/device/pin":
            self.service.submit_pin(body.get("pin", ""))
            return self.json({"ok": True})
        match = re.fullmatch(r"/api/notes/(\d+)/(edit|analyze|read|ignore)", path)
        if match:
            identifier, action = int(match[1]), match[2]
            if not self.service.store.note(identifier):
                raise FileNotFoundError("笔记不存在")
            if action == "edit":
                return self.json(public_note(self.service.edit_note(identifier, body)))
            if action == "ignore":
                return self.json(public_note(self.service.ignore_note(identifier, body.get("ignored")), self.service.settings()["sync_folders"]))
            if action == "analyze":
                if self.service.store.note(identifier)["ignored"]:
                    raise ValueError("请先恢复这条笔记，再解读")
                if not self.service.in_scope(self.service.store.note(identifier)["remote_path"]):
                    raise ValueError("这条笔记不在同步范围，请先添加它所在的文件夹")
                return self.json(self.service.enqueue("analyze", identifier), 202)
            note = self.service.store.note(identifier)
            answer_id = body.get("answer_id")
            if type(answer_id) is not int or answer_id <= 0:
                raise ValueError("请提供已看到的解读标识")
            if answer_id != note["answer_id"]:
                return self.json({"ok": True, "marked": []})
            marked = self.service.store.mark_answers_read(note["document_id"], [answer_id] if answer_id else [])
            return self.json({"ok": True, "marked": marked})
        match = re.fullmatch(r"/api/documents/([a-f0-9]{24})/read", path)
        if match:
            if not self.service.store.document(match[1]):
                raise FileNotFoundError("文件不存在")
            answers = body.get("answer_ids")
            if not isinstance(answers, list) or len(answers)>500 or any(type(value) is not int or value<=0 for value in answers):
                raise ValueError("请选择最多 500 条已看到的回复")
            return self.json({"ok": True, "marked": self.service.store.mark_answers_read(match[1], list(dict.fromkeys(answers)))})
        match = re.fullmatch(r"/api/documents/([a-f0-9]{24})/(track|pull)", path)
        if match:
            if not self.service.store.document(match[1]):
                raise FileNotFoundError("文件不存在")
            if match[2] == "pull":
                if not self.service.in_scope(self.service.store.document(match[1])["remote_path"]):
                    raise ValueError("这份文档不在同步范围，请先添加它所在的文件夹")
                return self.json(self.service.enqueue("pull", match[1]), 202)
            if not isinstance(body.get("tracked"), bool):
                raise ValueError("请选择是否自动同步")
            self.service.set_tracking(match[1], body["tracked"])
            return self.json({"ok": True})
        raise FileNotFoundError("接口不存在")


def main():
    load_env(ROOT/".env")
    parser = argparse.ArgumentParser(description="墨记 · 文档与笔记助手")
    parser.add_argument("--data", type=Path, default=default_data_dir())
    parser.add_argument("--auth", type=Path, default=Path(os.environ.get("DPT_AUTH_DIR", ROOT/".tools/dpt-auth")))
    parser.add_argument("--bind", default=os.environ.get("APP_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", 4387)))
    parser.add_argument("--no-bootstrap", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if database_path(args.data).exists():
        relocate(args.data)
    service = Service(args.data, args.auth)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.service = service
    server.allowed_hosts = {host.strip().lower() for host in os.environ.get("APP_ALLOWED_HOSTS", "").split(",") if host.strip()}
    access_file = os.environ.get("APP_ACCESS_FILE")
    server.web_auth = WebAuth(access_file) if access_file else None
    service.start()
    print(f"墨记已启动：http://localhost:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
