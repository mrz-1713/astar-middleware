"""Pure helper functions - no service state, so they stay unit-testable."""


from __future__ import annotations


import hashlib


import os


from pathlib import Path


from typing import (
    Optional,
)


def machine_http_outbox_path(base_db: Path, endpoint_id: str) -> Path:
    """Per-machine HTTPS outbox file for an endpoint.

    The readable part is lossy: "LINE-A/TOOL 1" and "LINE-A?TOOL_1" both
    sanitise to the same string, so two machines ended up sharing one queue
    file. A digest of the real endpoint id decides the name, and the
    sanitised form is kept only so the file is recognisable to a human.
    """
    suffix = base_db.suffix or ".sqlite3"
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in endpoint_id
    )
    # Non-security filename collision tag; never used for authenticity,
    # passwords, signatures, or content integrity.
    digest = hashlib.sha1(  # nosec B324
        endpoint_id.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:10]
    return base_db.with_name(f"{base_db.stem}.{safe}.{digest}{suffix}")


def optional_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def resolve_data_path(configured: str, install_dir: str, *fallback: str) -> Path:
    """Resolve a data-file path, remapping a Windows default on POSIX.

    The shipped defaults are Windows absolute paths. Off Windows, "C:/..." is
    just a relative path, so an unconfigured default would create a directory
    literally named "C:" under whatever the current working directory happens
    to be - scattering state around and, worse, sharing one file between runs
    that meant to be isolated.
    """
    text = str(configured)
    if os.name != "nt" and len(text) > 2 and text[1:3] == ":/":
        return Path(install_dir).joinpath(*fallback)
    return Path(text)


def reconnect_delay(base: float, failure_count: int, jitter: float) -> float:
    """Bounded exponential retry delay with up to twenty percent jitter."""
    base = max(1.0, float(base))
    cap = max(base, min(300.0, base * 16.0))
    delay = min(cap, base * (2 ** max(0, failure_count - 1)))
    return delay + delay * 0.2 * max(0.0, min(1.0, jitter))


def event_liveness_decision(
    *,
    baseline: Optional[object],
    current: Optional[object],
    delivered: bool,
    seconds_since_connect: float,
    grace: float,
    alarmed: bool,
) -> Optional[str]:
    """Pure decision for the event-liveness watchdog.

    Returns:
        "alarm" - the tool fired collection events (LastEventID advanced past
                  the post-connect baseline) but NO S6F11 report has been
                  delivered -> the subscription is acked-but-ineffective (E40
                  event style / spooling). Raise once.
        "clear" - S6F11 reports are now flowing again after a prior alarm.
        None    - nothing to do (idle tool, still inside grace, already alarmed,
                  or no readings yet).

    Idle tools never alarm: an idle DaVinci does not advance LastEventID, so
    current == baseline and we stay silent.
    """
    if delivered:
        return "clear" if alarmed else None
    if alarmed:
        return None
    if baseline is None or current is None:
        return None
    if seconds_since_connect < grace:
        return None
    if current != baseline:
        return "alarm"
    return None
