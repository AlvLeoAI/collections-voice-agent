from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class InvalidIdError(ValueError):
    """Raised when an identifier fails safe-character / path-traversal validation."""


def validate_safe_id(value: str, *, label: str = "id") -> str:
    if not isinstance(value, str) or not value or not _SAFE_ID_RE.fullmatch(value):
        raise InvalidIdError(f"Invalid {label}: {value!r}")
    return value


def atomic_write_json(path: Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.stem}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_name = f.name
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except Exception:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise
