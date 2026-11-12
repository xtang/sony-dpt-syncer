"""Storage paths, including compatibility with existing local archives."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME = "notes.sqlite3"
LEGACY_DATABASE_NAME = "study.sqlite3"


def database_path(data):
    data = Path(data)
    current = data/DATABASE_NAME
    legacy = data/LEGACY_DATABASE_NAME
    if current.exists() and legacy.exists():
        raise ValueError("数据目录包含两个数据库，请先确认应使用哪个归档")
    return legacy if legacy.exists() else current


def default_data_dir():
    if os.environ.get("APP_DATA_DIR"):
        return Path(os.environ["APP_DATA_DIR"])
    legacy = ROOT/"output/study"
    if (legacy/LEGACY_DATABASE_NAME).exists():
        return legacy
    return ROOT/"output/data"
