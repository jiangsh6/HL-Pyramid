from __future__ import annotations

import os
import re
from pathlib import Path

_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    return _INLINE_COMMENT.sub("", value).strip()


def load_env_file(path: str = ".env", *, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        if "=" not in s:
            continue
        key, raw_value = s.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _parse_env_value(raw_value)
        if override or key not in os.environ:
            os.environ[key] = value


def ensure_runtime_env_loaded() -> None:
    """
    Load `.env` for real runtime paths without making the test suite depend on
    workspace-local secret files.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    load_env_file()
