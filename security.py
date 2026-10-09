"""Private atomic files, bounded HTTP and credential-safe errors."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def private_write(path: str | Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_json(path: str | Path, value: dict) -> None:
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def safe_error(error: BaseException) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return type(error).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "")
