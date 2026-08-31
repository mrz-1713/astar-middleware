"""Local capacity guard for persist-before-ack service operation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .models import StorageSafetyConfig

logger = logging.getLogger(__name__)

NORMAL = "normal"
WARNING = "warning"
CRITICAL = "critical"
RECOVERING = "recovering"


class StorageBackpressureError(OSError):
    """Raised before durable ingress when the local reserve is unsafe."""


@dataclass(frozen=True)
class CapacitySample:
    path: str
    free_bytes: int
    total_bytes: int

    @property
    def free_percent(self) -> float:
        return 100.0 * self.free_bytes / max(1, self.total_bytes)


Probe = Callable[[Path], CapacitySample]
Transition = Callable[[str, str, Mapping[str, object]], bool | None]
Alert = Callable[[str, Mapping[str, object]], None]


def filesystem_probe(path: Path) -> CapacitySample:
    """Measure the filesystem containing *path*, including absent children."""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    usage = shutil.disk_usage(candidate)
    return CapacitySample(str(path), int(usage.free), int(usage.total))


def windows_event_alert(state: str, details: Mapping[str, object]) -> None:
    """Emit an alert independent of every network publisher/outbox.

    ``eventcreate`` is part of supported Windows editions and needs no Python
    package. Arguments are passed as an array (never through a shell), and the
    diagnostic intentionally contains no payloads or credentials.
    """
    if os.name != "nt":
        return
    event_type = "ERROR" if state == CRITICAL else "WARNING"
    event_id = "9103" if state == CRITICAL else "9102"
    message = (
        f"ASTAR storage state={state}; path={details.get('path', '')}; "
        f"free_bytes={details.get('free_bytes', '')}; "
        f"free_percent={details.get('free_percent', '')}"
    )
    subprocess.run(  # noqa: S603 - fixed native executable, no shell
        [
            "eventcreate.exe",
            "/T",
            event_type,
            "/ID",
            event_id,
            "/L",
            "APPLICATION",
            "/SO",
            "ASTAR Middleware Storage",
            "/D",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class StorageSafetyMonitor:
    """Debounced normal/warning/critical/recovering state machine."""

    def __init__(
        self,
        config: StorageSafetyConfig,
        paths: Callable[[], Iterable[Path]],
        transition: Transition,
        *,
        probe: Probe = filesystem_probe,
        alert: Alert = windows_event_alert,
        integrity_check: Callable[[], bool] = lambda: True,
    ) -> None:
        self.config = config
        self._paths = paths
        self._transition = transition
        self._probe = probe
        self._alert = alert
        self._integrity_check = integrity_check
        self._state = NORMAL
        self._candidate: Optional[str] = None
        self._candidate_count = 0
        self._samples: tuple[CapacitySample, ...] = ()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def accepting_ingress(self) -> bool:
        return self._state in (NORMAL, WARNING)

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict[str, object]:
        worst = min(
            self._samples,
            key=lambda item: (item.free_percent, item.free_bytes),
            default=None,
        )
        return {
            "state": self._state,
            "accepting_ingress": self.accepting_ingress,
            "thresholds": asdict(self.config),
            "worst": self._details(worst) if worst else {},
            "filesystems": [self._details(item) for item in self._samples],
        }

    def require_ingress_capacity(self) -> None:
        if not self.accepting_ingress:
            raise StorageBackpressureError(
                f"durable ingress is quiesced while storage is {self._state}"
            )

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        # Fail closed on restart: sample synchronously before any equipment
        # session is started. Critical is immediate on this first sample.
        self.sample(force=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="StorageSafetyMonitor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.config.sample_interval_sec):
            try:
                self.sample()
            except Exception:
                # A probe failure cannot silently restore acceptance. Surface
                # it independently and drive the same fail-closed path.
                logger.exception("Storage capacity sampling failed")
                self._apply_candidate(CRITICAL, {}, force=True)

    def sample(self, *, force: bool = False) -> str:
        unique: dict[str, Path] = {}
        for raw in self._paths():
            path = Path(raw)
            unique[str(path.resolve(strict=False))] = path
        self._samples = tuple(self._probe(path) for path in unique.values())
        if not self._samples:
            raise RuntimeError("storage safety has no configured paths to monitor")
        worst = min(
            self._samples,
            key=lambda item: (item.free_percent, item.free_bytes),
        )
        target = self._target_state(worst)
        self._apply_candidate(target, self._details(worst), force=force)
        return self._state

    def _target_state(self, sample: CapacitySample) -> str:
        cfg = self.config
        critical = (
            sample.free_bytes <= cfg.critical_free_bytes
            or sample.free_percent <= cfg.critical_free_percent
        )
        warning = (
            sample.free_bytes <= cfg.warning_free_bytes
            or sample.free_percent <= cfg.warning_free_percent
        )
        recovered = (
            sample.free_bytes >= cfg.recovery_free_bytes
            and sample.free_percent >= cfg.recovery_free_percent
        )
        if critical:
            return CRITICAL
        if self._state in (CRITICAL, RECOVERING):
            return NORMAL if recovered else RECOVERING
        return WARNING if warning else NORMAL

    def _apply_candidate(
        self, target: str, details: Mapping[str, object], *, force: bool
    ) -> None:
        if target == self._state:
            self._candidate = None
            self._candidate_count = 0
            return
        if target != self._candidate:
            self._candidate = target
            self._candidate_count = 1
        else:
            self._candidate_count += 1
        immediate = force and target == CRITICAL
        if not immediate and self._candidate_count < self.config.debounce_samples:
            return
        previous = self._state
        if target == NORMAL and previous in (CRITICAL, RECOVERING):
            if not self._integrity_check():
                target = RECOVERING
        self._state = target
        self._candidate = None
        self._candidate_count = 0
        logger.log(
            logging.ERROR if target == CRITICAL else logging.WARNING,
            "Storage safety transition %s -> %s: %s",
            previous,
            target,
            dict(details),
        )
        try:
            self._alert(target, details)
        except Exception:
            logger.exception("Independent storage alert failed")
        # Alert failure must never prevent quiescence.
        self._transition(previous, target, details)

    @staticmethod
    def _details(sample: Optional[CapacitySample]) -> dict[str, object]:
        if sample is None:
            return {}
        return {
            "path": sample.path,
            "free_bytes": sample.free_bytes,
            "total_bytes": sample.total_bytes,
            "free_percent": round(sample.free_percent, 3),
        }
