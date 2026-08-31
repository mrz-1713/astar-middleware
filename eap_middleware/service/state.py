"""Shared state for the service.

``ServiceState`` owns every instance attribute and declares the methods that
cross concern boundaries, so each mixin in this package type-checks on its
own.  The declarations here are the service's internal contract; the real
implementations live in the mixins and win by MRO.
"""


from __future__ import annotations


import threading


from pathlib import Path


from typing import (
    TYPE_CHECKING, Dict, Optional, Tuple,
)


from ..alarms import AlarmRateLimiter

from ..control import file_revision


from ..csv_store import PerLotCsvWriter

from ..job_tracker import JobTracker

from ..journal import (
    IngressJournal,
)

from ..legacy_api import LegacyApiPublisher

from ..logging_setup import MachineLogManager

from ..mapper import CanonicalMapper

from ..models import (
    CanonicalEvent,
    MachineConfig,
    MachineLinkstuffsHttpConfig,
    ServiceConfig,
)

from ..outbox import SQLiteOutbox

from ..profiles import (
    MachineProfile,
    ProfileRegistry,
)

from ..secs_runtime import SecsMachineSession

from ..single_instance import SingleInstanceLock
from ..storage_safety import StorageSafetyMonitor


from ..linkstuffs import LinkstuffsGatewayPublisher

from ..linkstuffs_http import LinkstuffsHttpPublisher

# The simulator is a SEPARATE deliverable, shipped as its own standalone exe,
# and the middleware package does not contain it. So `simulator` must never be
# imported at module scope: doing so makes the whole service unimportable on a
# production install, where only the middleware is present.
#
# `from __future__ import annotations` above makes the annotations below strings,
# so TYPE_CHECKING is enough for type checkers and costs nothing at runtime.
# The real imports live in `_start_simulator`, which only runs for a machine
# with `runtime_mode: simulated`.
if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from simulator.runner import SimulatorRunner

from .helpers import (
    resolve_data_path,
)
from .session import SessionGuard


class ServiceState:
    """Instance state shared by every concern mixin in this package.

    ``__init__`` owns all instance attributes, and the declarations below
    are the calls that cross concern boundaries. Keeping both here is what
    lets each mixin type-check on its own; the real implementations live in
    the mixins and win by MRO.
    """

    def __init__(
        self,
        config: ServiceConfig,
        profiles: Optional[ProfileRegistry] = None,
        config_path: Optional[str | Path] = None,
    ):
        self.config = config
        self.config_path = Path(config_path) if config_path else None
        self.profiles = profiles or ProfileRegistry()
        self.outbox = SQLiteOutbox(
            config.paths.outbox_db,
            retention_days=config.outbox_retention_days,
        )
        self.publisher = LinkstuffsGatewayPublisher(config.linkstuffs, self.outbox)
        self.legacy_api_outbox = SQLiteOutbox(
            config.paths.legacy_api_outbox_db,
            retention_days=config.outbox_retention_days,
        )
        self.legacy_api = LegacyApiPublisher(config.legacy_api, self.legacy_api_outbox)
        # HTTP REST upstream (Cloudflare-friendly path). Independent outbox
        # so it can run alongside or instead of the MQTT publisher above.
        self.http_outbox = SQLiteOutbox(
            config.paths.http_outbox_db,
            retention_days=config.outbox_retention_days,
        )
        self.http_publisher = LinkstuffsHttpPublisher(
            config.linkstuffs_http, self.http_outbox,
        )
        self._http_publishers: Dict[str, LinkstuffsHttpPublisher] = {}
        self._http_outboxes: Dict[str, SQLiteOutbox] = {}
        # Written before any acknowledgement goes back to a tool. Everything
        # below is a derived view that can be rebuilt from it.
        self.journal = IngressJournal(
            resolve_data_path(
                config.paths.ingress_journal_db,
                config.paths.install_dir,
                "data",
                "ingress_journal.sqlite3",
            ),
            retention_days=config.outbox_retention_days,
            cross_generation_window_sec=(
                config.cross_generation_retransmit_window_sec
            ),
        )
        self.csv_writer = PerLotCsvWriter(journal=self.journal)
        self.machine_logs = MachineLogManager(config.logging)
        self.job_tracker = JobTracker()
        # v2 Track A: shared alarm rate limiter across all machines. Defaults
        # are conservative; ops can adjust by replacing self.alarm_limiter
        # before start() or by setting per-machine overrides on MachineConfig.
        self.alarm_limiter = AlarmRateLimiter(max_per_window=50, window_sec=1.0)
        self._recent_alarms: Dict[tuple[str, str, bool], float] = {}
        self._alarm_lock = threading.Lock()
        # v2 Track B: PID lockfile in install_dir prevents two middleware
        # processes from silently fighting over the same HSMS connections.
        self.instance_lock = SingleInstanceLock(
            Path(config.paths.install_dir) / "middleware.lock"
        )
        self._sessions: Dict[str, SecsMachineSession] = {}
        self._session_guards: Dict[str, SessionGuard] = {}
        self._simulators: Dict[str, Tuple[SimulatorRunner, threading.Thread]] = {}
        self._simulator_exit_codes: Dict[str, int] = {}
        self._svid_threads: Dict[str, threading.Thread] = {}
        self._svid_stop_events: Dict[str, threading.Event] = {}
        self._machines_by_endpoint: Dict[str, MachineConfig] = {}
        self._profiles_by_endpoint: Dict[str, MachineProfile] = {}
        self._reconnect_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._mirror_thread: Optional[threading.Thread] = None
        self._mirror_wake = threading.Event()
        # The writer sets this the moment a mirror is queued, so moving
        # the copy off the S6F11 acknowledgement path costs latency
        # rather than a whole poll interval.
        self.csv_writer.set_mirror_wake_event(self._mirror_wake)
        self._reconcile_lock = threading.RLock()
        # Serializes every journal-entry dispatch. The live gateway-callback
        # path and the supervisor's replay path both check-then-dispatch; a
        # replay pass running between journal.append and the sinks would
        # otherwise dispatch the same entry twice (duplicate CSV rows), and
        # the refcounts in PerLotCsvWriter would race. Everything under this
        # lock re-reads the entry's fresh journal state, so only one thread
        # ever applies an entry's sinks.
        self._dispatch_lock = threading.Lock()
        self._generations: Dict[str, int] = {}
        self._runtime_states: Dict[str, Dict[str, object]] = {}
        self._command_results: Dict[str, Dict[str, object]] = {}
        self._revision = file_revision(self.config_path) if self.config_path else "initial"
        self._last_invalid_config = ""
        self._last_reconnect_attempt: Dict[str, float] = {}
        self._reconnect_failures: Dict[str, int] = {}
        self._reconnect_inflight: set[str] = set()
        self._liveness_inflight: set[str] = set()
        # endpoint_id -> when the current outage started, and whether it has
        # been escalated yet. Without this the watchdog repeats one identical
        # WARNING for hours and never says why the tool will not come back.
        self._outage_since: Dict[str, float] = {}
        self._outage_escalated: set[str] = set()
        # CSV-sink failure log throttle: a full traceback the first time a
        # machine's CSV sink fails, then a count of suppressed repeats until
        # the sink recovers or the interval elapses. Without this, a broken
        # sink (disk full, permissions) produces one traceback per collection
        # event - hundreds per lot - which floods the log while saying the
        # same thing every time.
        self._csv_fail_last_log: Dict[str, float] = {}
        self._csv_fail_suppressed: Dict[str, int] = {}
        # Per-endpoint event-liveness state for the E40/silent-subscription
        # detector: {"connect_ts": float, "baseline": LastEventID-at-connect,
        # "alarmed": bool}. Reset on every (re)connect.
        self._event_liveness: Dict[str, Dict[str, object]] = {}
        self._running = False
        self.storage_monitor = StorageSafetyMonitor(
            config.storage_safety,
            self._storage_paths,
            self._on_storage_transition,
            integrity_check=self._storage_integrity_check,
        )

    def _storage_paths(self) -> list[Path]:
        paths = self.config.paths
        result = [
            Path(paths.install_dir), Path(paths.log_dir), Path(paths.data_dir),
            Path(paths.control_dir), Path(paths.archive_dir),
            Path(paths.outbox_db), Path(paths.legacy_api_outbox_db),
            Path(paths.http_outbox_db), Path(paths.ingress_journal_db),
        ]
        for machine in self.config.machines:
            result.extend((machine.csv_local_dir, machine.admin_dir))
            network = machine.csv_network_dir
            if network is not None:
                result.append(network)
        return result

    def _storage_integrity_check(self) -> bool:
        stores = [
            self.journal, self.outbox, self.legacy_api_outbox,
            self.http_outbox, *self._http_outboxes.values(),
        ]
        try:
            return all(store.integrity_check() for store in stores)
        except Exception:
            return False

    def _on_storage_transition(
        self, previous: str, current: str, details: object
    ) -> bool | None:
        raise NotImplementedError  # implemented by LifecycleMixin

    def _advance_generation(self, endpoint_id: str) -> int:
        raise NotImplementedError  # implemented by LifecycleMixin

    def _dispatch_alarm(
        self, machine: MachineConfig, alarm: Dict[str, object], seq: Optional[int]
    ) -> None:
        raise NotImplementedError  # implemented by AlarmsMixin

    def _drain_alarm_summary(self) -> None:
        raise NotImplementedError  # implemented by AlarmsMixin

    def _on_alarm(self, machine: MachineConfig, alarm: Dict[str, object]) -> None:
        raise NotImplementedError  # implemented by AlarmsMixin

    def _publish_alarm_state_unknown(self, machine: MachineConfig) -> None:
        raise NotImplementedError  # implemented by AlarmsMixin

    def _start_supervisor(self) -> None:
        raise NotImplementedError  # implemented by ControlMixin

    def _write_status(self) -> None:
        raise NotImplementedError  # implemented by ControlMixin

    def _on_secs_event(
        self, machine: MachineConfig, ceid: int, data: Dict[str, object]
    ) -> None:
        raise NotImplementedError  # implemented by DispatchMixin

    def _replay_journal(self, limit: int = 500) -> int:
        raise NotImplementedError  # implemented by DispatchMixin

    def _on_connect(self, machine: MachineConfig) -> None:
        raise NotImplementedError  # implemented by HealthMixin

    def _on_disconnect(self, machine: MachineConfig) -> None:
        raise NotImplementedError  # implemented by HealthMixin

    def _publish_health(self, machine: MachineConfig, state: str, details: str = "") -> None:
        raise NotImplementedError  # implemented by HealthMixin

    def _start_reconnect_watchdog(self) -> None:
        raise NotImplementedError  # implemented by HealthMixin

    def _start_svid_thread(
        self,
        machine: MachineConfig,
        profile: MachineProfile,
        session: SecsMachineSession,
    ) -> None:
        raise NotImplementedError  # implemented by HealthMixin

    def _effective_machine_http(
        self, machine: MachineConfig
    ) -> MachineLinkstuffsHttpConfig:
        raise NotImplementedError  # implemented by HttpOutboxMixin

    def _queue_http_attributes(
        self, machine: MachineConfig, profile: MachineProfile
    ) -> None:
        raise NotImplementedError  # implemented by HttpOutboxMixin

    def _queue_http_event(self, event: CanonicalEvent) -> None:
        raise NotImplementedError  # implemented by HttpOutboxMixin

    def _start_machine_http(self, machine: MachineConfig) -> None:
        raise NotImplementedError  # implemented by HttpOutboxMixin

    def _stop_machine_http(
        self, endpoint_id: str, deadline: Optional[float] = None
    ) -> None:
        raise NotImplementedError  # implemented by HttpOutboxMixin

    @staticmethod
    def _budget(deadline: Optional[float], cap: float) -> float:
        raise NotImplementedError  # implemented by LifecycleMixin

    def _join_within(
        self, thread: Optional[threading.Thread], deadline: Optional[float],
        cap: float, what: str,
    ) -> None:
        raise NotImplementedError  # implemented by LifecycleMixin

    def _start_machine(self, machine: MachineConfig) -> None:
        raise NotImplementedError  # implemented by LifecycleMixin

    def _stop_machine(
        self, endpoint_id: str, reason: str,
        deadline: Optional[float] = None,
    ) -> None:
        raise NotImplementedError  # implemented by LifecycleMixin

    def reconcile(
        self,
        config: ServiceConfig,
        revision: Optional[str] = None,
    ) -> Dict[str, str]:
        raise NotImplementedError  # implemented by LifecycleMixin

    @staticmethod
    def _runtime_machine(machine: MachineConfig) -> MachineConfig:
        raise NotImplementedError  # implemented by SimulatorMixin

    def _set_runtime_state(
        self, endpoint_id: str, state: str, error: str = ""
    ) -> None:
        raise NotImplementedError  # implemented by SimulatorMixin

    def _simulator_status(self, endpoint_id: str) -> str:
        raise NotImplementedError  # implemented by SimulatorMixin

    def _start_simulator(
        self, machine: MachineConfig, subscription_path: Optional[str] = None
    ) -> None:
        # Imported here, not at module scope: the simulator is a separate
        # deliverable and is absent from a middleware-only install. A missing
        # simulator must degrade to one clear message on one machine, not make
        # the whole service unimportable.
        raise NotImplementedError  # implemented by SimulatorMixin

    def _stop_simulator(
        self, endpoint_id: str, deadline: Optional[float] = None
    ) -> None:
        raise NotImplementedError  # implemented by SimulatorMixin

    def _admin_dir(self, machine: MachineConfig) -> Path:
        raise NotImplementedError  # implemented by WiringMixin

    def _mapper(self, machine: MachineConfig) -> CanonicalMapper:
        raise NotImplementedError  # implemented by WiringMixin

    def _prepare_machine(self, machine: MachineConfig, profile: MachineProfile) -> None:
        raise NotImplementedError  # implemented by WiringMixin

    def _profile_for(self, machine: MachineConfig) -> MachineProfile:
        raise NotImplementedError  # implemented by WiringMixin

    def _subscription_path_for(self, machine: MachineConfig) -> Optional[str]:
        raise NotImplementedError  # implemented by WiringMixin
