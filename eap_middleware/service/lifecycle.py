"""Service and machine lifecycle: start, stop, and reconcile."""


from __future__ import annotations


import random

import signal


import threading

import time


from concurrent.futures import ThreadPoolExecutor, as_completed


from types import FrameType

from typing import (
    Callable, Dict, FrozenSet, Optional, Tuple,
)


from ..models import (
    MachineConfig,
    ServiceConfig,
)


from ..secs_runtime import SecsMachineSession


from .constants import (
    STOP_TIMEOUT_SEC,
    logger,
)
from .errors import StaleSessionError
from .helpers import (
    reconnect_delay,
)
from .session import SessionGuard
from .state import ServiceState
from ..storage_safety import CRITICAL, NORMAL, RECOVERING


class LifecycleMixin(ServiceState):
    """Service and machine lifecycle: start, stop, and reconcile."""


    def start(self) -> None:
        if self._running:
            return
        # v2 Track B: take the single-instance lock BEFORE any side effects.
        # If another process holds it, fail loudly without disturbing its
        # MQTT sessions or outbox.
        self.instance_lock.acquire()
        self._running = True
        # Everything below has a side effect that stop() is responsible for
        # undoing: worker threads, a bound legacy-API port, live HSMS sessions,
        # and the lockfile itself. If any step raises - an unreachable broker,
        # a port already in use, a machine entry reconcile rejects - the caller
        # sees the original exception but the service is left half-started:
        # `_running` stays True, so a second start() returns immediately at the
        # guard above and does nothing, and the lockfile stays held with no
        # owner, so every later attempt fails on the lock instead of the real
        # cause. Unwind here so the failure is reported once and cleanly.
        try:
            self.storage_monitor.start()
            # v2 Track B: outbox purge runs in its own daemon thread so
            # retention is enforced even when MQTT (and the publish loop that
            # used to run purge) is wedged on an unreachable broker.
            self.outbox.start_maintenance()
            self.legacy_api_outbox.start_maintenance()
            self.http_outbox.start_maintenance()
            self.publisher.start()
            self.legacy_api.start()
            self.reconcile(self.config, revision=self._revision)
            # After reconcile, so the machines the entries belong to are
            # configured and their profiles are loaded. Anything acknowledged
            # to a tool but not yet written when the last process died is
            # finished here.
            try:
                self._replay_journal()
            except Exception:
                logger.exception("Ingress journal replay failed at startup")
            self._start_reconnect_watchdog()
            self._start_supervisor()
        except BaseException:
            logger.exception("Service start failed; unwinding")
            try:
                # stop() is idempotent over the parts that never started, and
                # it is what flushes open CSV lot buffers - a machine that
                # connected before the failing step may already have buffered
                # rows that would otherwise be lost.
                self.stop()
            except Exception:
                logger.exception("Cleanup after a failed start also failed")
                # stop() releases the lockfile last, so a failure inside it can
                # leave the lock held by a process that is about to keep
                # running. Releasing twice is a no-op.
                self.instance_lock.release()
            self._running = False
            raise


    @staticmethod
    def _budget(deadline: Optional[float], cap: float) -> float:
        """Seconds a join may wait: what's left of the shared budget, capped.

        Never negative and never zero - a 0-second join is a no-op that skips
        even an already-finished thread, which loses the ordering guarantee
        the join is there for.
        """
        if deadline is None:
            return cap
        return max(0.05, min(cap, deadline - time.monotonic()))


    def _join_within(
        self, thread: Optional[threading.Thread], deadline: Optional[float],
        cap: float, what: str,
    ) -> None:
        """Join a worker inside the shared budget, and say so if it outlives it.

        A thread still alive here is not abandoned quietly: it is a daemon, so
        it cannot keep the process alive, but under the control panel the
        process does keep running, and an operator restarting the service
        deserves to know which worker never stood down.
        """
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=self._budget(deadline, cap))
        if thread.is_alive():
            logger.warning(
                "%s did not stop within the shutdown budget; it is a daemon "
                "thread and will be terminated when the process exits",
                what,
            )


    def stop(self, timeout: float = STOP_TIMEOUT_SEC) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self._running = False
        self.storage_monitor.stop(self._budget(deadline, 5.0))
        self._mirror_wake.set()
        self._join_within(
            self._supervisor_thread, deadline, 2.0, "Configuration supervisor"
        )
        self._supervisor_thread = None
        self._join_within(
            self._mirror_thread, deadline, 2.0, "CSV mirror worker"
        )
        self._mirror_thread = None
        for endpoint_id in list(self._sessions):
            self._stop_machine(
                endpoint_id, reason="service_stop", deadline=deadline
            )
        # Deliberately outside the budget. This is the last chance to turn an
        # open lot buffer into a file on disk; a deadline that cut it short
        # would trade a slow stop for a lost lot.
        self.csv_writer.flush_all(reason="service_stop")
        self.legacy_api.stop(self._budget(deadline, 10.0))
        self.publisher.stop(self._budget(deadline, 10.0))
        self.http_publisher.stop(self._budget(deadline, 10.0))
        # v2 Track B: stop the purge threads before releasing the lockfile so
        # no maintenance happens after the next process takes the lock.
        try:
            for outbox in (
                self.outbox, self.legacy_api_outbox, self.http_outbox,
            ):
                outbox.stop_maintenance(self._budget(deadline, 5.0))
        except Exception:
            logger.debug("Outbox maintenance stop failed", exc_info=True)
        # Release the lockfile last so a crashed publisher.stop() still leaves
        # a stale lock that gets reclaimed on next boot.
        self.instance_lock.release()
        self._write_status()


    @staticmethod
    def _restart_signature(machine: MachineConfig) -> Tuple[object, ...]:
        return (
            machine.host,
            machine.port,
            machine.secs_device_id,
            machine.hsms_mode,
            machine.hsms_bind_address,
            machine.machine_profile,
            machine.event_subscription_path,
            machine.event_subscription_enabled,
            machine.svid_collection_enabled,
            machine.enable_alarms,
            machine.request_online,
            machine.drain_spool_on_connect,
            machine.reset_subscription_on_connect,
            machine.alarm_rate_limit,
            machine.runtime_mode,
            machine.hsms_timers,
        )


    # Machine settings that deliberately do NOT restart the session when they
    # change. Everything else in MachineConfig must appear in
    # _restart_signature, or editing it while the service runs is accepted,
    # written back to production.yaml, and silently never applied - the failure
    # `hsms_timers` had before it was added. tests/test_unified_control.py pins
    # this, so adding a field to MachineConfig forces the decision.
    _NO_RESTART_FIELDS: FrozenSet[str] = frozenset({
        # Identity of the row itself, not of the connection.
        "endpoint_id",
        # A label; reconcile() handles enable/disable without this.
        "display_name", "enabled",
        # Sinks and simulator settings: applied by their own owners without
        # tearing down the HSMS session.
        "storage", "linkstuffs_http", "simulator",
        "local_csv_path", "network_csv_path", "admin_config_path",
        # Chooses whether an upstream exists at all, handled by the publisher
        # wiring rather than the session.
        "offline_test_mode",
    })


    def _reconcile_endpoint(
        self,
        endpoint_id: str,
        machine: Optional[MachineConfig],
        start_delay: float = 0.0,
    ) -> Optional[str]:
        current = self._machines_by_endpoint.get(endpoint_id)
        try:
            if machine is None:
                self._stop_machine(endpoint_id, reason="disabled_or_removed")
                return "stopped"
            if current is None:
                if start_delay:
                    time.sleep(start_delay)
                self._start_machine(machine)
                return "started"
            if self._restart_signature(current) != self._restart_signature(machine):
                self._stop_machine(endpoint_id, reason="config_change")
                self._start_machine(machine)
                return "restarted"

            self.machine_logs.apply(
                endpoint_id,
                machine.log_dir,
                machine.simulator_log_dir,
                display_name=machine.display_name,
            )
            if current.linkstuffs_http != machine.linkstuffs_http:
                self._stop_machine_http(endpoint_id)
                self._start_machine_http(machine)
            action: Optional[str] = None
            if current.simulator != machine.simulator and machine.is_simulated:
                self._profiles_by_endpoint.pop(endpoint_id, None)
                self._stop_simulator(endpoint_id)
                self._start_simulator(machine)
                action = "simulator_restarted"
            self._machines_by_endpoint[endpoint_id] = machine
            return action
        except Exception as exc:
            logger.exception("Runtime reconcile failed for %s", endpoint_id)
            if current is None:
                self._stop_machine(endpoint_id, reason="start_failed")
            self._set_runtime_state(endpoint_id, "Error", str(exc))
            return "error"


    def reconcile(
        self,
        config: ServiceConfig,
        revision: Optional[str] = None,
    ) -> Dict[str, str]:
        """Apply one valid config without disturbing unchanged sessions."""
        actions: Dict[str, str] = {}
        with self._reconcile_lock:
            self.config = config
            desired = {m.endpoint_id: m for m in config.machines if m.enabled}
            stagger = max(0.0, float(config.startup_stagger_sec))
            work: list[tuple[str, Optional[MachineConfig], float]] = [
                (endpoint_id, None, 0.0)
                for endpoint_id in list(self._sessions)
                if endpoint_id not in desired
            ]
            new_index = 0
            for endpoint_id, machine in desired.items():
                delay = 0.0
                if endpoint_id not in self._machines_by_endpoint:
                    delay = new_index * stagger
                    new_index += 1
                work.append((endpoint_id, machine, delay))

            if work:
                with ThreadPoolExecutor(
                    max_workers=len(work), thread_name_prefix="Reconcile"
                ) as executor:
                    futures = {
                        executor.submit(
                            self._reconcile_endpoint, endpoint_id, machine, delay
                        ): endpoint_id
                        for endpoint_id, machine, delay in work
                    }
                    for future in as_completed(futures):
                        endpoint_id = futures[future]
                        action = future.result()
                        if action is not None:
                            actions[endpoint_id] = action
            if revision is not None:
                self._revision = revision
            self._write_status()
        return actions


    def _start_machine(self, machine: MachineConfig) -> None:
        self._machines_by_endpoint[machine.endpoint_id] = machine
        self.machine_logs.apply(
            machine.endpoint_id,
            machine.log_dir,
            machine.simulator_log_dir,
            display_name=machine.display_name,
        )
        logger.info("Starting machine %s", machine.endpoint_id)
        self._start_machine_http(machine)
        profile = self._profile_for(machine)
        logger.info(
            "Profile provenance for %s/%s: %s",
            machine.endpoint_id,
            machine.machine_profile,
            profile.notes,
        )
        self._prepare_machine(machine, profile)
        subscription_path = self._subscription_path_for(machine)
        runtime_machine = self._runtime_machine(machine)
        generation = max(
            self._generations.get(machine.endpoint_id, 0),
            self.journal.latest_generation(machine.endpoint_id),
        ) + 1
        guard = SessionGuard(generation)
        self._session_guards[machine.endpoint_id] = guard
        session = SecsMachineSession(
            machine=runtime_machine,
            event_callback=lambda m, ceid, data: self._guarded_event(
                guard, m, ceid, data
            ),
            alarm_callback=lambda m, alarm: self._guarded_alarm(guard, m, alarm),
            connect_callback=lambda m: self._guarded_lifecycle(
                guard, self._on_connect, m
            ),
            disconnect_callback=lambda m: self._guarded_lifecycle(
                guard, self._on_disconnect, m
            ),
            subscription_path=subscription_path,
            dv_name_by_id={v: k for k, v in profile.dvs_by_name.items()},
            events_enabled_svid=profile.health_events_enabled_svid,
        )
        self._sessions[machine.endpoint_id] = session
        interval = max(1.0, float(self.config.reconnect_interval_sec))
        self._last_reconnect_attempt[machine.endpoint_id] = (
            time.time() + reconnect_delay(interval, 1, random.random())
        )
        self._reconnect_failures[machine.endpoint_id] = 0
        self._generations[machine.endpoint_id] = generation
        self._set_runtime_state(machine.endpoint_id, "Starting")
        try:
            if machine.is_simulated:
                self._start_simulator(machine, subscription_path)
            if self.storage_monitor.accepting_ingress:
                session.start()
                self._set_runtime_state(machine.endpoint_id, "Connecting")
            else:
                self._set_runtime_state(
                    machine.endpoint_id,
                    "StorageBackpressure",
                    "local durable reserve is below the critical threshold",
                )
        except Exception as exc:
            logger.error("Failed to start %s: %s", machine.endpoint_id, exc)
            self._set_runtime_state(machine.endpoint_id, "Error", str(exc))
            self._publish_health(machine, "runtime_error", str(exc))
        self._start_svid_thread(machine, profile, session)

    def _on_storage_transition(
        self, previous: str, current: str, details: object
    ) -> bool | None:
        """Quiesce equipment at critical and reconnect only after integrity."""
        if current in (CRITICAL, RECOVERING):
            for endpoint_id, session in list(self._sessions.items()):
                self._set_runtime_state(endpoint_id, "StorageBackpressure")
                try:
                    session.stop()
                except Exception:
                    logger.exception(
                        "Could not quiesce %s during storage backpressure",
                        endpoint_id,
                    )
        elif current == NORMAL and previous in (CRITICAL, RECOVERING):
            for endpoint_id, session in list(self._sessions.items()):
                machine = self._machines_by_endpoint.get(endpoint_id)
                if machine is None or not machine.enabled:
                    continue
                try:
                    self._advance_generation(endpoint_id)
                    session.start()
                    self._set_runtime_state(endpoint_id, "Connecting")
                except Exception as exc:
                    logger.error(
                        "Storage recovered but %s could not restart: %s",
                        endpoint_id,
                        exc,
                    )
        self._write_status()
        return True

    def _advance_generation(self, endpoint_id: str) -> int:
        generation = max(
            self._generations.get(endpoint_id, 0),
            self.journal.latest_generation(endpoint_id),
        ) + 1
        self._generations[endpoint_id] = generation
        guard = self._session_guards.get(endpoint_id)
        if guard is not None:
            guard.generation = generation
        return generation


    def _guarded_event(
        self,
        guard: SessionGuard,
        machine: MachineConfig,
        ceid: int,
        data: Dict[str, object],
    ) -> None:
        if not guard.active:
            # Refuse rather than drop. Returning normally would have the gateway
            # answer ACKC6=0, telling the tool we hold an event that no live
            # session is going to journal - and the tool is then free to forget
            # it. A refusal makes it resend to whichever session is current.
            raise StaleSessionError(
                f"{machine.endpoint_id}: session generation {guard.generation} "
                "has been retired; refusing the event so the tool retains it"
            )
        self._on_secs_event(machine, ceid, data)


    def _guarded_alarm(
        self,
        guard: SessionGuard,
        machine: MachineConfig,
        alarm: Dict[str, object],
    ) -> None:
        if not guard.active:
            raise StaleSessionError(
                f"{machine.endpoint_id}: session generation {guard.generation} "
                "has been retired; refusing the alarm so the tool retains it"
            )
        self._on_alarm(machine, alarm)


    def _guarded_lifecycle(
        self,
        guard: SessionGuard,
        callback: Callable[[MachineConfig], None],
        machine: MachineConfig,
    ) -> None:
        """Connect/disconnect notices from a retired session are simply ignored.

        Unlike equipment data these carry nothing to lose, and acting on them
        would let a dying session overwrite the health and liveness state that
        its replacement has already published.
        """
        if not guard.active:
            logger.debug(
                "Ignoring lifecycle callback from retired session %s/%d",
                machine.endpoint_id, guard.generation,
            )
            return
        callback(machine)


    def _stop_machine(
        self, endpoint_id: str, reason: str,
        deadline: Optional[float] = None,
    ) -> None:
        self._set_runtime_state(endpoint_id, "Stopping")
        logger.info("Stopping machine %s (%s)", endpoint_id, reason)
        # Retire the generation first, so anything still in flight on the old
        # session is refused instead of landing after the teardown.
        guard = self._session_guards.pop(endpoint_id, None)
        if guard is not None:
            guard.active = False
        stop_event = self._svid_stop_events.pop(endpoint_id, None)
        if stop_event is not None:
            stop_event.set()
        session = self._sessions.pop(endpoint_id, None)
        if session is not None:
            try:
                session.stop(self._budget(deadline, 10.0))
            except Exception:
                # Not debug: a session that fails to stop may still hold the
                # tool's only HSMS peer slot, in which case every later
                # connect attempt is refused and the machine looks
                # permanently disconnected for a reason found nowhere in the
                # log. This is the one line that names the cause.
                logger.exception(
                    "Session teardown for %s failed; its HSMS connection may "
                    "still be open and could block the next connect",
                    endpoint_id,
                )
        self._stop_simulator(endpoint_id, deadline)
        self._stop_machine_http(endpoint_id, deadline)
        self.csv_writer.flush_machine(endpoint_id, reason=reason)
        # Joined, not just dropped: an unjoined poller can be mid-S1F3 and
        # publish a sample under the old profile after the machine has been
        # restarted onto a new one. The session guard is what makes that safe
        # if the join does expire - the retired generation's sample is dropped.
        self._join_within(
            self._svid_threads.pop(endpoint_id, None), deadline, 10.0,
            f"SVID poller for {endpoint_id}",
        )
        self._machines_by_endpoint.pop(endpoint_id, None)
        self._profiles_by_endpoint.pop(endpoint_id, None)
        self._event_liveness.pop(endpoint_id, None)
        self._last_reconnect_attempt.pop(endpoint_id, None)
        self._reconnect_failures.pop(endpoint_id, None)
        # Outage bookkeeping is per-connected-episode. Leaving it behind would
        # let a disabled-then-re-enabled machine inherit a stale escalation
        # flag (suppressing the next outage's escalation) and a stale
        # _outage_since that makes the next "reconnected after Ns down" log
        # measure from the previous outage, possibly days ago.
        self._outage_since.pop(endpoint_id, None)
        self._outage_escalated.discard(endpoint_id)
        self.machine_logs.remove(endpoint_id)
        self._set_runtime_state(endpoint_id, "Stopped")


    def machine_states(self) -> Dict[str, str]:
        """endpoint_id -> 'connected'/'disconnected' for status displays.

        Read-only view over the live sessions so callers (the GUI) don't have
        to reach into _sessions.
        """
        return {
            endpoint_id: (
                "connected"
                if getattr(session.host, "is_connected", False)
                else "disconnected"
            )
            # Snapshot the dict so a concurrent stop/start cannot raise
            # "dictionary changed size during iteration" into this call.
            for endpoint_id, session in list(self._sessions.items())
        }


    def run_forever(self) -> None:
        self.start()
        stop_event = threading.Event()

        def handle_signal(signum: int, frame: Optional[FrameType]) -> None:
            _ = (signum, frame)
            stop_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        while not stop_event.is_set():
            time.sleep(1)
        self.stop()
