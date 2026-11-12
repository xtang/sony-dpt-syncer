"""Relocate existing document archives without adding sample content."""
from __future__ import annotations

import argparse
from pathlib import Path

from .service import Service
from .paths import ROOT, default_data_dir
from .env import load_env


def relocate(data):
    """Make a copied bootstrap data directory portable between macOS and Linux."""
    service = Service(data, Path(data)/"auth")
    with service.store.db() as db:
        for row in db.execute("SELECT id,baseline_path FROM documents WHERE baseline_path IS NOT NULL").fetchall():
            db.execute("UPDATE documents SET baseline_path=? WHERE id=?", (str(service.data/"library"/Path(row["baseline_path"]).name), row["id"]))
        for row in db.execute("SELECT id,document_id,sha256 FROM snapshots").fetchall():
            db.execute("UPDATE snapshots SET pdf_path=? WHERE id=?", (str(service.data/"documents"/row["document_id"]/(row["sha256"]+".pdf")), row["id"]))
        for row in db.execute("SELECT id,preview_path FROM notes WHERE preview_path IS NOT NULL").fetchall():
            db.execute("UPDATE notes SET preview_path=? WHERE id=?", (str(service.data/"previews"/Path(row["preview_path"]).name), row["id"]))


if __name__ == "__main__":
    load_env(ROOT/".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=default_data_dir())
    parser.add_argument("--relocate", action="store_true")
    args = parser.parse_args()
    relocate(args.data)
