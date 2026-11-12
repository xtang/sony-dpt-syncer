#!/usr/bin/env python3
"""Build a source release and empty initialization data; pairing migration is opt-in."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_source_archive(files, destination, root=ROOT):
    root = Path(root)
    entries = set(files)
    for path in files:
        parent = path.parent
        while parent != root:
            if not parent.is_relative_to(root):
                raise ValueError("Source must be inside the project")
            entries.add(parent)
            parent = parent.parent
    def source_permissions(member):
        member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        return member
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(entries, key=lambda item: (len(item.relative_to(root).parts), str(item))):
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False, filter=source_permissions)


def copy_pairing_credentials(source, destination, include=False):
    if not include or not Path(source).exists():
        return []
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    copied = []
    for name in ("deviceid.dat", "privatekey.dat", "device-ca.pem"):
        if (Path(source)/name).exists():
            shutil.copyfile(Path(source)/name, destination/name)
            (destination/name).chmod(0o600)
            copied.append(name)
    return copied


def application_sources(root):
    root = Path(root)
    extensions = {".py", ".html", ".css", ".js", ".svg"}
    return [path for path in (root/"app").rglob("*") if path.is_file() and not path.is_symlink()
            and "__pycache__" not in path.parts and (path.suffix in extensions or path == root/"app/requirements.txt")]


def package(destination, include_pairing=False):
    from app.service import Service
    os.umask(0o077)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source = destination/"dpt-syncer-code.tar.gz"
    files = application_sources(ROOT)
    files += [ROOT/p for p in ["Dockerfile", "compose.yaml", ".dockerignore", ".gitignore", ".env.example", "README.md", "LICENSE", "requirements.txt",
        "scripts/install_remote.sh", "scripts/repair_report.py", "scripts/repair_notes.py",
        "scripts/deploy.sh", "scripts/deploy.py", "scripts/package.py", "scripts/open.command", "scripts/start.command",
        "docs/usage.md"]]
    files += list((ROOT/"tests").glob("test_*.py"))
    files += list((ROOT/"tests").glob("test_*.cjs"))
    build_source_archive(files, source)
    seed = destination/"dpt-syncer-private-seed.tar.gz"
    with tempfile.TemporaryDirectory(prefix="dpt-syncer-seed-") as temporary:
        data = Path(temporary)/"data"
        service = Service(data, ROOT/".tools/dpt-auth")
        auth = Path(os.environ.get("DPT_AUTH_DIR", ROOT/".tools/dpt-auth"))
        copied = copy_pairing_credentials(auth, data/"auth", include_pairing)
        # Checkpoint before packaging; no running service owns this temporary database.
        with service.store.db() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        def private_metadata(item):
            if item.name.endswith(("-wal", "-shm")):
                return None
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            return item
        with tarfile.open(seed, "w:gz") as archive:
            archive.add(data, arcname="data", filter=private_metadata)
    manifest = dict(release=release, files=[dict(name=p.name, bytes=p.stat().st_size,
        sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in (source, seed)],
        contains_pairing_credentials=bool(copied),
        private_seed="包含配对凭据，仅供私有部署，禁止公开分享" if copied else "仅包含空数据库，不含配对凭据或文档")
    (destination/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for path in destination.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"output/deploy")
    parser.add_argument("--include-pairing", action="store_true", help="显式将本机配对凭据放入私有初始化包，禁止公开该包")
    args = parser.parse_args()
    package(args.output, args.include_pairing)
