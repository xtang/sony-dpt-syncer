#!/usr/bin/env python3
"""Build and deploy to a user-configured SSH host, or open the saved web address."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import webbrowser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.env import load_env


def validate_target(target):
    if not target or target.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", target):
        raise ValueError("请通过参数或 DPT_DEPLOY_HOST 指定有效的 SSH 目标")
    return target


def validate_web_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("网页地址需要是 HTTP(S) 根地址")
    return value.rstrip("/")


def deployment_location():
    root = os.environ.get("DPT_REMOTE_ROOT") or "apps/dpt-syncer"
    project = os.environ.get("DPT_DEPLOY_PROJECT") or "dpt-syncer"
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", root) or PurePosixPath(root).is_absolute() or any(part in ("", ".", "..") for part in root.split("/")):
        raise ValueError("DPT_REMOTE_ROOT 必须是相对远端用户主目录的安全目录路径")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("DPT_DEPLOY_PROJECT 必须是有效的 Compose 项目名")
    return root, project


def write_options(path):
    values = {key: os.environ[key] for key in ("DPT_ADDRESS", "APP_LISTEN_ADDRESS", "APP_ALLOWED_HOSTS") if os.environ.get(key)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(values, file)
    path.chmod(0o600)
    return values


def deploy(target, include_pairing=False):
    target = validate_target(target)
    remote_root, project = deployment_location()
    command = [sys.executable, str(ROOT/"scripts/package.py")]
    if include_pairing:
        command.append("--include-pairing")
    subprocess.run(command, cwd=ROOT, check=True)
    output = ROOT/"output/deploy"
    options = output/"deploy-options.json"
    write_options(options)
    remote_path = '"$HOME/"'+shlex.quote(remote_root)
    preflight = 'set -eu; test "$(uname -s)" = Linux; command -v docker >/dev/null; docker compose version >/dev/null; docker info >/dev/null; command -v python3 >/dev/null; umask 077; mkdir -p '+remote_path+'/incoming'
    subprocess.run(["ssh", target, "sh -c "+shlex.quote(preflight)], check=True)
    files = [output/name for name in ("dpt-syncer-code.tar.gz", "dpt-syncer-private-seed.tar.gz", "manifest.json", "deploy-options.json")]
    subprocess.run(["scp", *map(str, files), target+":"+remote_root+"/incoming/"], check=True)
    with (ROOT/"scripts/install_remote.sh").open("rb") as script:
        subprocess.run(["ssh", target, "bash -s -- "+shlex.quote(remote_root)+" "+shlex.quote(project)], stdin=script, check=True)
    url = subprocess.check_output(["ssh", target, "sh -c "+shlex.quote("cat "+remote_path+"/service-url.txt")]).decode().strip()
    url = validate_web_url(url)
    try:
        subprocess.run(["curl", "--silent", "--show-error", "--fail", "--noproxy", "*", "--max-time", "5", url+"/api/health"], stdout=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        raise RuntimeError("容器已启动，但本机暂时无法直连网页，请检查服务器的局域网连接和 4387 端口") from None
    (output/"service-url.txt").write_text(url+"\n")
    print("部署检查通过。以后直接在浏览器打开："+url)


def main():
    load_env(ROOT/".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=os.environ.get("DPT_DEPLOY_HOST"))
    parser.add_argument("--include-pairing", action="store_true", help="显式迁移本机已有的 DPT 配对凭据")
    parser.add_argument("--open", action="store_true", help="打开配置或上次部署保存的网页地址")
    args = parser.parse_args()
    try:
        if args.open:
            saved = ROOT/"output/deploy/service-url.txt"
            url = os.environ.get("DPT_WEB_URL") or (saved.read_text().strip() if saved.exists() else "http://localhost:4387")
            webbrowser.open(validate_web_url(url))
        else:
            deploy(args.target, args.include_pairing)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error)+"\n")


if __name__ == "__main__":
    main()
