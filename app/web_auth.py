"""Single-user access password and signed browser sessions for LAN access."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

COOKIE = "dpt_syncer_session"
SESSION_SECONDS = 30*24*60*60


def create_access_file(path, password=None, reset=False):
    path = Path(path)
    if path.exists() and not reset:
        return None
    password = password or secrets.token_urlsafe(15)
    salt = secrets.token_bytes(16)
    payload = dict(version=1, salt=salt.hex(),
                   password_hash=hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex(),
                   session_secret=secrets.token_hex(32))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(payload, file)
    temporary.replace(path)
    path.chmod(0o600)
    return password


class WebAuth:
    def __init__(self, path):
        self.config = json.loads(Path(path).read_text())
        self.secret = bytes.fromhex(self.config["session_secret"])
        self.failures = {}
        self.lock = threading.Lock()

    def login(self, password, address):
        clock = time.time()
        with self.lock:
            self.failures = {key: [t for t in values if t > clock-600]
                             for key, values in self.failures.items() if any(t > clock-600 for t in values)}
            if len(self.failures.get(address, [])) >= 10:
                raise PermissionError("尝试次数过多，请十分钟后再试")
        if not isinstance(password, str) or not 1 <= len(password) <= 512:
            valid = False
        else:
            hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(self.config["salt"]), n=16384, r=8, p=1)
            valid = hmac.compare_digest(hashed.hex(), self.config["password_hash"])
        if not valid:
            with self.lock:
                self.failures.setdefault(address, []).append(clock)
            raise PermissionError("访问口令不正确")
        with self.lock:
            self.failures.pop(address, None)
        payload = str(int(clock))+":"+secrets.token_urlsafe(18)
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded+"."+signature

    def valid(self, cookie_header):
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            token = cookie[COOKIE].value
            encoded, signature = token.split(".", 1)
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)).decode()
            issued = int(payload.split(":", 1)[0])
            age = time.time()-issued
            return -30 <= age <= SESSION_SECONDS
        except (KeyError, ValueError, UnicodeError, TypeError, CookieError):
            return False

    @staticmethod
    def cookie(token, clear=False):
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else SESSION_SECONDS}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="创建或重置网页访问口令")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    value = create_access_file(args.file, reset=args.reset)
    if value:
        print("网页访问口令（请保存）："+value)
    else:
        print("保留现有网页访问口令")
