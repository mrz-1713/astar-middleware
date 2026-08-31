"""The two log panes have to show what actually happened on the wire.

Both used to hide the thing an operator opens them for. The simulator muted
secsgem's `communication` logger, which is the only source of the decoded
SECS body of every request and reply. The middleware panel read the
per-machine `middleware.log`, which is written through a filter that drops
records it cannot attribute to an endpoint - the whole wire trace included.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from simulator.cli import configure_logging
from simulator.config import simulator_config_from_dict


def test_simulator_logging_does_not_mute_the_wire_trace(tmp_path: Path) -> None:
    config = simulator_config_from_dict(
        {
            "connection": {
                "role": "equipment",
                "mode": "passive",
                "address": "127.0.0.1",
                "port": 5000,
            },
            "logging": {"level": "INFO", "directory": str(tmp_path)},
        }
    )
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    # configure_logging() calls basicConfig(force=True), which CLOSES every
    # handler it displaces - including pytest's own capture handler, which
    # then breaks every later test in the session. Detach them first so
    # force=True finds nothing to close, and hand them back afterwards.
    root.handlers = []
    try:
        log_path = configure_logging(config)
        wire = logging.getLogger("communication")
        assert wire.isEnabledFor(logging.INFO), "the SECS body is logged at INFO"
        wire.info("> S6F11 body")
        for handler in root.handlers:
            handler.flush()
        assert "S6F11 body" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level


def test_middleware_panel_tail_is_bounded_and_line_aligned(tmp_path: Path) -> None:
    pytest.importorskip("tkinter")
    from gui import app

    path = tmp_path / "eap_middleware.log"
    path.write_text("\n".join(f"line {n}" for n in range(10_000)), encoding="utf-8")

    assert app._tail_lines(path, 5) == [f"line {n}" for n in range(9_995, 10_000)]
    assert app._tail_lines(tmp_path / "missing.log", 5) == []

    # A file longer than the tail window: every returned line must be whole,
    # never the back half of one the seek landed in the middle of.
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * (app.LOG_TAIL_BYTES + 500) + b"\nlast line\n")
    lines = app._tail_lines(big, 5000)
    assert lines[-1] == "last line"
    assert all(set(line) <= {"x"} for line in lines[:-1])
