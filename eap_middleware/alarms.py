"""Per-machine alarm rate limiter.

A misconfigured tool can fire thousands of S5F1 alarms per second. Without
throttling, the SQLite outbox and Linkstuffs get flooded - the storm itself
becomes the outage. AlarmRateLimiter admits up to N alarms per machine per
window; everything beyond that is silently dropped but counted, and a single
synthetic 'AlarmStormSummary' event surfaces the count so operators still
know a storm happened.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass
class _MachineBucket:
    """Sliding-window alarm counters for a single machine."""

    # Sliding window: timestamps of admitted alarms in the current window.
    timestamps: list[float] = field(default_factory=list)
    dropped_in_window: int = 0
    window_started_at: float = 0.0
    dropped_by_alid: Dict[str, int] = field(default_factory=dict)


class AlarmRateLimiter:
    """Caps how many alarms each machine may publish per window.

    One chattering tool must not starve the other 21 machines, so admission
    is counted per machine rather than globally. Drops are tallied per ALID
    so the shed summary can name the offending alarm.
    """

    def __init__(self, max_per_window: int = 50, window_sec: float = 1.0):
        self._max = max(1, int(max_per_window))
        self._window = max(0.001, float(window_sec))
        self._buckets: Dict[str, _MachineBucket] = {}
        self._lock = threading.Lock()

    def admit(
        self,
        machine_id: str,
        now: Optional[float] = None,
        *,
        alarm: Optional[Mapping[str, Any]] = None,
        max_per_window: Optional[int] = None,
    ) -> bool:
        """Return True if this alarm should be processed, False if dropped."""
        if alarm is not None:
            is_set = bool(alarm.get("is_set", True))
            try:
                alarm_class = int(alarm.get("alcd", alarm.get("ALCD", 0))) & 0x7F
            except (TypeError, ValueError):
                alarm_class = 0
            # Clears and SEMI E5 personal/equipment-safety alarms are state
            # critical and must never be shed.
            if not is_set or alarm_class in {1, 2}:
                return True
        limit = self._max if max_per_window is None else max(1, int(max_per_window))
        ts = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(machine_id)
            if bucket is None:
                bucket = _MachineBucket(window_started_at=ts)
                self._buckets[machine_id] = bucket
            # Drop expired timestamps from the head of the window.
            cutoff = ts - self._window
            while bucket.timestamps and bucket.timestamps[0] < cutoff:
                bucket.timestamps.pop(0)
            if len(bucket.timestamps) < limit:
                bucket.timestamps.append(ts)
                return True
            bucket.dropped_in_window += 1
            if alarm is not None:
                alid = str(alarm.get("alid", alarm.get("ALID", "unknown")))
                bucket.dropped_by_alid[alid] = bucket.dropped_by_alid.get(alid, 0) + 1
            return False

    def drain_drops(self) -> Dict[str, int]:
        """Return {machine_id: dropped_count} for any machine that has
        dropped alarms since the last drain. Called by the service on a
        periodic timer to emit a summary event."""
        with self._lock:
            out: Dict[str, int] = {}
            for machine_id, bucket in self._buckets.items():
                if bucket.dropped_in_window:
                    out[machine_id] = bucket.dropped_in_window
                    bucket.dropped_in_window = 0
                    # Same reset as drain_drop_details, or the ALIDs of an
                    # already-drained window are reported again later.
                    bucket.dropped_by_alid.clear()
            return out

    def drain_drop_details(self) -> Dict[str, Dict[str, Any]]:
        """Drain counts with the affected ALIDs for operational summaries."""
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for machine_id, bucket in self._buckets.items():
                if not bucket.dropped_in_window:
                    continue
                out[machine_id] = {
                    "count": bucket.dropped_in_window,
                    "alids": dict(bucket.dropped_by_alid),
                }
                bucket.dropped_in_window = 0
                bucket.dropped_by_alid.clear()
            return out
