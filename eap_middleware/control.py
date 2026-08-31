"""Atomic local files shared by the Windows service and passive GUI."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

STATUS_FILE = "runtime_status.json"
COMMAND_DIR = "commands"


class StaleConfigError(RuntimeError):
    """The config changed since the caller loaded it."""


def content_revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_revision(path: str | Path) -> str:
    target = Path(path)
    return content_revision(target.read_bytes()) if target.exists() else ""


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def leading_comment_block(text: str) -> str:
    """The comment header at the top of a YAML file, up to the first setting.

    `yaml.safe_dump` emits data, not the document it came from, so saving from
    the control panel used to erase every comment in production.yaml - the
    shipped template is heavily annotated, including the manual sections that
    justify each machine's timers and its request_online setting, and one save
    took all of it. This preserves at least the header, which is where the
    file-level guidance lives. Per-key comments further down cannot survive a
    round-trip through safe_dump and are documented as lost.
    """
    kept: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            kept.append(line)
            continue
        break
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def save_config_atomic(
    path: str | Path,
    raw: Mapping[str, Any],
    *,
    expected_revision: Optional[str] = None,
) -> str:
    target = Path(path)
    current = file_revision(target)
    if expected_revision is not None and current != expected_revision:
        raise StaleConfigError(
            "production.yaml changed since it was loaded; reload before saving"
        )
    header = ""
    try:
        header = leading_comment_block(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        header = ""  # first save, or an unreadable file; write the data alone
    body = yaml.safe_dump(dict(raw), sort_keys=False)
    document = f"{header}\n{body}" if header else body
    content = document.encode("utf-8")
    _write_atomic(target, content)
    return content_revision(content)


def write_status(data_dir: str | Path, status: Mapping[str, Any]) -> None:
    content = json.dumps(status, sort_keys=True, indent=2, default=str).encode(
        "utf-8"
    )
    _write_atomic(Path(data_dir) / STATUS_FILE, content)


def load_status(data_dir: str | Path) -> Dict[str, Any]:
    path = Path(data_dir) / STATUS_FILE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def submit_command(data_dir: str | Path, action: str, endpoint_id: str) -> str:
    request_id = uuid.uuid4().hex
    command = {
        "request_id": request_id,
        "action": action,
        "endpoint_id": endpoint_id,
        "created_at": time.time(),
    }
    directory = Path(data_dir) / COMMAND_DIR
    filename = f"{time.time_ns():020d}-{request_id}.json"
    _write_atomic(
        directory / filename,
        json.dumps(command, sort_keys=True).encode("utf-8"),
    )
    return request_id


def consume_commands(data_dir: str | Path) -> List[Dict[str, Any]]:
    directory = Path(data_dir) / COMMAND_DIR
    if not directory.exists():
        return []
    commands: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    commands.append(value)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return commands
