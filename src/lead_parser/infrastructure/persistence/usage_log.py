"""Durable accounting for paid AI requests without prompts or personal data."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from filelock import FileLock


class UsageLogError(OSError):
    """The request journal could not be persisted."""


def _identifier(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}", value):
        return value
    return None


def _tokens(value: object) -> int | None:
    # JSON booleans are integers in Python but are not valid token counts.
    return value if type(value) is int and value >= 0 else None


def _cost(value: object) -> str | None:
    if type(value) not in {int, float, str, Decimal}:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        text = str(value)
    except ValueError:
        return None
    if len(text) > 128:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return str(amount) if amount.is_finite() and amount >= 0 else None


def usage_fields(data: object) -> dict:
    """Extract only provider accounting fields; absent or malformed means unknown."""
    response = data if isinstance(data, dict) else {}
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "generation_id": _identifier(response.get("id")),
        "model": _identifier(response.get("model")),
        "prompt_tokens": _tokens(usage.get("prompt_tokens")),
        "completion_tokens": _tokens(usage.get("completion_tokens")),
        "cost_usd": _cost(usage.get("cost")),
    }


class UsageLog:
    """Append one start and one finish record per HTTP attempt, across processes.

    A missing finish is intentionally retained as an unknown charge. Opening this
    object performs no I/O, so disabled/offline classification creates no files.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = FileLock(str(self.path) + ".lock", timeout=10, mode=0o600)

    def _append(self, record: dict) -> None:
        line = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._lock:
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.path, flags, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise UsageLogError("AI usage journal must be a regular file")
                    os.fchmod(fd, 0o600)
                    remaining = memoryview(line)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise UsageLogError("AI usage journal write failed")
                        remaining = remaining[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                # Persist a newly created directory entry as well as file data.
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, TimeoutError) as error:
            raise UsageLogError("Cannot persist AI usage journal") from error

    def start(
        self,
        *,
        requested_model: str,
        batch_size: int,
        purpose: str = "pipeline",
        source_counts: dict[str, int] | None = None,
    ) -> dict:
        if purpose not in {"pipeline", "evaluation"}:
            raise ValueError("Unknown AI usage purpose")
        now = datetime.now(UTC).isoformat()
        record = {
            "schema_version": 1,
            "event": "started",
            "request_id": str(uuid4()),
            "started_at": now,
            "occurred_at": now,
            "purpose": purpose,
            "provider": "openrouter",
            "requested_model": _identifier(requested_model),
            "batch_size": batch_size,
            "source_counts": source_counts or {},
        }
        self._append(record)
        return record

    def finish(self, request: dict, *, response: object, outcome: str, error: BaseException | None) -> None:
        record = {
            **request,
            "event": "finished",
            "occurred_at": datetime.now(UTC).isoformat(),
            **usage_fields(response),
            "outcome": outcome,
            "error_type": _identifier(type(error).__name__) if error is not None else None,
        }
        self._append(record)
