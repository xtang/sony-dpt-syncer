from __future__ import annotations

import contextlib
import hashlib
import io
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests
from dptrp1.dptrp1 import DigitalPaper, ResolveObjectFailed
from .scope import folder_path


class DeviceError(RuntimeError):
    pass


def validate_address(address):
    address = str(address).strip()
    if not address or len(address) > 200 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:" for c in address):
        raise ValueError("请输入 DPT 的 IP 地址或本地域名")
    return address


class DeviceClient:
    def __init__(self, config, auth_dir, relay=None):
        self.config = config
        self.auth_dir = Path(auth_dir)
        self.relay = relay
        self.lock = threading.RLock()

    def probe(self):
        address = validate_address(self.config()["device_address"])
        session = requests.Session()
        session.trust_env = False
        if self.relay:
            session.send = self.relay.send
        try:
            response = session.get(f"http://{address}:8080/register/information", timeout=(2, 3))
            response.raise_for_status()
            info = response.json()
            return {"online": True, "address": address, "model": info.get("model_name", "Digital Paper"),
                    "paired": (self.auth_dir/"privatekey.dat").exists() and (self.auth_dir/"deviceid.dat").exists()}
        except (requests.RequestException, ValueError, OSError):
            return {"online": False, "address": address, "model": "Sony DPT", "paired": (self.auth_dir/"privatekey.dat").exists()}

    def _raw(self):
        device = DigitalPaper(addr=validate_address(self.config()["device_address"]))
        device.session.trust_env = False
        if self.relay:
            device.session.send = self.relay.send
        else:
            original = device.session.send
            def send(request, **kwargs):
                kwargs.setdefault("timeout", (4, 20))
                return original(request, **kwargs)
            device.session.send = send
        return device

    def _authenticate(self, device):
        try:
            identity = (self.auth_dir/"deviceid.dat").read_text().strip()
            key = (self.auth_dir/"privatekey.dat").read_bytes()
        except FileNotFoundError:
            raise DeviceError("设备尚未配对，请到设置中输入设备屏幕上的配对码")
        try:
            response = device.authenticate(identity, key)
            response.raise_for_status()
        except (requests.RequestException, KeyError, ValueError) as error:
            raise DeviceError("连接或配对失败，请确认 DPT 已唤醒、Wi-Fi 已连接及配对仍有效") from error

    def connect(self):
        device = self._raw()
        self._authenticate(device)
        if self.config().get("sync_clock", True):
            # Import the already verified write-only datetime / HTTP Date helper.
            from .device_clock import read_clock, synchronize_clock
            _, clock = read_clock(device)
            if abs((datetime.now(timezone.utc)-clock).total_seconds()) > 5:
                synchronize_clock(device, reauthenticate=lambda: self._authenticate(device))
        return device

    def inventory(self, device):
        response = device._get_endpoint("/documents2?entry_type=all")
        response.raise_for_status()
        return response.json()["entry_list"]

    def download(self, device, metadata, destination, limit=128*1024*1024):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        identifier = metadata.get("entry_id")
        if not identifier:
            metadata = device.list_document_info(metadata["entry_path"])
            identifier = metadata["entry_id"]
        url = device.base_url+"/documents/"+identifier+"/file"
        response = device.session.get(url, stream=not bool(self.relay), timeout=(4, 120))
        if response.status_code == 401:
            self._authenticate(device)
            response = device.session.get(url, stream=not bool(self.relay), timeout=(4, 120))
        response.raise_for_status()
        temporary = destination.with_suffix(".part")
        size = 0
        try:
            with temporary.open("wb") as target:
                chunks = [response.content] if self.relay else response.iter_content(128*1024)
                for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise DeviceError("这份 PDF 超过 128 MB，请拆分后同步")
                    target.write(chunk)
            with temporary.open("rb") as check:
                if check.read(5) != b"%PDF-":
                    raise DeviceError("设备没有返回有效的 PDF")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
            response.close()
        return destination

    def upload(self, path, folder):
        folder = str(folder).strip().rstrip("/")
        if PurePosixPath(folder).parts[0:1] != ("Document",) or ".." in PurePosixPath(folder).parts:
            raise ValueError("设备目录必须位于 Document 下")
        with self.lock:
            device = self.connect()
            device.new_folder(folder)
            name = Path(path).name
            remote = folder+"/"+name
            try:
                device.list_document_info(remote)
            except ResolveObjectFailed:
                pass
            else:
                remote = folder+"/"+Path(name).stem+datetime.now().strftime("_%Y%m%d_%H%M%S_")+uuid.uuid4().hex[:6]+".pdf"
            device.upload_file(str(path), remote)
            info = device.list_document_info(remote)
            return info

    def ensure_folder(self, device, path):
        path = folder_path(path)
        try:
            info = device.list_document_info(path)
        except ResolveObjectFailed:
            device.new_folder(path)
            info = device.list_document_info(path)
        if info.get("entry_type") != "folder" or not info.get("entry_id"):
            raise DeviceError("目标路径不是可用的设备文件夹")
        return info

    def move_document(self, device, metadata, folder):
        destination = folder["entry_path"].rstrip("/")+"/"+Path(metadata["entry_path"]).name
        try:
            occupied = device.list_document_info(destination)
        except ResolveObjectFailed:
            occupied = None
        if occupied:
            if occupied.get("entry_id") == metadata["entry_id"]:
                return occupied
            raise DeviceError("目标文件夹已有同名文件，未覆盖："+Path(destination).name)
        response = device.move_file_to_folder_by_id(metadata["entry_id"], folder["entry_id"])
        response.raise_for_status()
        result = device.list_document_info(destination)
        if result.get("entry_id") != metadata["entry_id"]:
            raise DeviceError("移动后的文档标识不一致，请刷新设备目录核对")
        return result

    def pair(self, pin_callback):
        import builtins
        with self.lock:
            device = self._raw()
            original_input = builtins.input
            try:
                builtins.input = lambda prompt="": pin_callback()
                with contextlib.redirect_stdout(io.StringIO()):
                    result = device.register()
                if not result:
                    raise DeviceError("配对未完成，请重试")
                certificate, key, identity = result
                self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.auth_dir.chmod(0o700)
                for name, value in [("device-ca.pem", certificate), ("privatekey.dat", key), ("deviceid.dat", identity)]:
                    path = self.auth_dir/name
                    path.write_text(value)
                    path.chmod(0o600)
            finally:
                builtins.input = original_input

