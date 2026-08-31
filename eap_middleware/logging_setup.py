"""Logging setup for service and CLI entrypoints."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

from .models import LoggingConfig


_WORD_EDGE = re.compile(r"[A-Za-z0-9_]")


class _MachineFilter(logging.Filter):
    """Decide whether one record belongs in one machine's own log file.

    Selection is by name because most of the record sources carry no machine
    identity: secsgem's `communication` wire trace, `gateway.event_subscription`
    and `eap_middleware.outbox` all log without knowing which endpoint they are
    serving. The old rule - keep the record only if `endpoint_id` appears
    somewhere in the message text - therefore threw away nearly everything an
    operator needs. A NexGen MG rig's `middleware.log` was, for forty minutes,
    nothing but "Alarm SET"/"Alarm CLEARED" and the reconnect warning: the
    entire S1F13/S2F33/S6F11 trace, the subscription result and every CSV write
    (which names the machine by its *display name*) had been filtered out, so
    the file could not show that the connection had never been established.

    Three changes:

      * `display_name` counts too, so csv_store/linkstuffs records land here.
      * Matching is on word boundaries. Plain substring matching put every
        TOOL_1 record into TOOL_10's log as well, which silently mixes two
        machines together in a 22-machine install.
      * WARNING and above is always kept, whoever logged it. A warning that
        cannot be attributed to a machine is exactly the one worth having in
        front of you, and losing it is far worse than repeating it.
    """

    def __init__(
        self,
        endpoint_id: str,
        simulator: bool = False,
        display_name: str = "",
    ) -> None:
        super().__init__()
        self.endpoint_id = endpoint_id
        self.simulator = simulator
        self.display_name = display_name or ""

    def _names_this_machine(self, message: str) -> bool:
        for name in (self.endpoint_id, self.display_name):
            if not name:
                continue
            start = message.find(name)
            while start != -1:
                before = message[start - 1] if start else ""
                after_index = start + len(name)
                after = message[after_index] if after_index < len(message) else ""
                if not _WORD_EDGE.match(before or " ") and not _WORD_EDGE.match(
                    after or " "
                ):
                    return True
                start = message.find(name, start + 1)
        return False

    def filter(self, record: logging.LogRecord) -> bool:
        simulator_thread = record.threadName == f"Simulator-{self.endpoint_id}"
        if self.simulator:
            return simulator_thread
        if simulator_thread:
            return False
        if getattr(record, "endpoint_id", None) == self.endpoint_id:
            return True
        if record.levelno >= logging.WARNING:
            return True
        return self._names_this_machine(record.getMessage())


class MachineLogManager:
    """Rotate the two handlers owned by one endpoint without reconnecting it."""

    def __init__(self, config: LoggingConfig) -> None:
        self.config = config
        self._handlers: dict[str, tuple[logging.Handler, logging.Handler]] = {}
        self._paths: dict[str, tuple[tuple[Path, Path], str]] = {}

    def apply(
        self,
        endpoint_id: str,
        log_dir: Path,
        simulator_log_dir: Path,
        display_name: str = "",
    ) -> None:
        paths = (log_dir / "middleware.log", simulator_log_dir / "simulator.log")
        if os.name != "nt" and any(
            len(str(path)) > 2 and str(path)[1:3] == ":/" for path in paths
        ):
            return
        if self._paths.get(endpoint_id) == (paths, display_name):
            return
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handlers: list[logging.Handler] = []
        try:
            for simulator, path in ((False, paths[0]), (True, paths[1])):
                handler = logging.handlers.RotatingFileHandler(
                    path,
                    maxBytes=self.config.max_size_mb * 1024 * 1024,
                    backupCount=self.config.backup_count,
                    encoding="utf-8",
                )
                handler.setLevel(
                    getattr(logging, self.config.level.upper(), logging.INFO)
                )
                handler.setFormatter(formatter)
                handler.addFilter(
                    _MachineFilter(
                        endpoint_id,
                        simulator=simulator,
                        display_name=display_name,
                    )
                )
                handlers.append(handler)
        except Exception:
            for handler in handlers:
                handler.close()
            raise
        self.remove(endpoint_id)
        for handler in handlers:
            logging.getLogger().addHandler(handler)
        self._handlers[endpoint_id] = (handlers[0], handlers[1])
        self._paths[endpoint_id] = (paths, display_name)

    def remove(self, endpoint_id: str) -> None:
        for handler in self._handlers.pop(endpoint_id, ()):
            logging.getLogger().removeHandler(handler)
            handler.close()
        self._paths.pop(endpoint_id, None)


def configure_logging(config: LoggingConfig, log_dir: str) -> None:
    level = getattr(logging, config.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        path / "eap_middleware.log",
        maxBytes=config.max_size_mb * 1024 * 1024,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)
