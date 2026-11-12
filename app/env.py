"""Load supported local settings without executing shell code or expanding values."""
import os
import re
import shlex
from pathlib import Path

ENV_KEYS = frozenset({"DPT_DEPLOY_HOST", "DPT_ADDRESS", "DPT_WEB_URL", "DPT_AUTH_DIR",
    "DPT_REMOTE_ROOT", "DPT_DEPLOY_PROJECT",
    "APP_DATA_DIR", "APP_BIND", "APP_PORT", "APP_LISTEN_ADDRESS", "APP_ALLOWED_HOSTS"})


def read_env(path):
    path = Path(path)
    if not path.exists():
        return {}
    result = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"环境配置第 {number} 行格式不正确")
        if key not in ENV_KEYS:
            continue
        try:
            values = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"环境配置第 {number} 行引号不完整") from None
        if len(values) > 1:
            raise ValueError(f"环境配置第 {number} 行包含空格的值需要引号")
        result[key] = values[0] if values else ""
    return result


def load_env(path):
    values = read_env(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values
