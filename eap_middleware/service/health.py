"""Connection health: reconnect watchdog, outage escalation, liveness."""


from __future__ import annotations


import random


import threading

import time


from typing import (
    Any,
)


from ..mapper import CanonicalMapper

from ..models import (
    MachineConfig,
)


from ..profiles import (
    MachineProfile,
)

from ..secs_runtime import SecsMachineSession


from ..svid_admin import SvidAdminConfig, SvidAdminError


from .constants import (
    logger,
)
from .helpers import (
    event_liveness_decision,
    reconnect_delay,
)
from .state import ServiceState


class HealthMixin(ServiceState):
    """Connection health: reconnect watchdog, outage escalation, liveness."""


    def _on_connect(self, machine: MachineConfig) -> None:
        machine = self._machines_by_endpoint.get(machine.endpoint_id, machine)
        self._reconnect_failures[machine.endpoint_id] = 0
        outage_start = self._outage_since.pop(machine.endpoint_id, None)
        if machine.endpoint_id in self._outage_escalated:
            self._outage_escalated.discard(machine.endpoint_id)
            logger.info(
                "%s reconnected after %.0fs down",
                machine.endpoint_id,
                time.time() - (outage_start or time.time()),
            )
        # Reset the event-liveness baseline on every (re)connect: a fresh
        # GatewayHost is created so host.last_event_time is None again, and the
        # tool's LastEventID baseline must be re-sampled after this connect.
        self._event_liveness[machine.endpoint_id] = {
            "connect_ts": time.time(),
            "baseline": None,
            "alarmed": False,
            "offline_alarmed": False,
            "spool_alarmed": False,
        }
        self._publish_health(machine, "connected")
        self._publish_alarm_state_unknown(machine)
        self._set_runtime_state(machine.endpoint_id, "Running")
        self._write_status()


    def _on_disconnect(self, machine: MachineConfig) -> None:
        machine = self._machines_by_endpoint.get(machine.endpoint_id, machine)
        self._publish_health(machine, "disconnected")
        if self._running and machine.endpoint_id in self._sessions:
            self._set_runtime_state(machine.endpoint_id, "Retrying")
            self._write_status()


    def _publish_health(self, machine: MachineConfig, state: str, details: str = "") -> None:
        event = self._mapper(machine).connection_event(machine, state, details)
        self.publisher.queue_event(event)
        self._queue_http_event(event)


    def _start_reconnect_watchdog(self) -> None:
        """Fix #7: poll each session's host and restart it if the connection
        has been down for longer than reconnect_interval_sec. Throttled to
        one attempt per machine per interval so we don't busy-loop against
        an unreachable tool."""
        interval = max(1.0, float(self.config.reconnect_interval_sec))

        def loop() -> None:
            while self._running:
                if not self.storage_monitor.accepting_ingress:
                    time.sleep(interval)
                    continue
                now = time.time()
                # v2 Track A: surface any alarm-storm drops accumulated since
                # the last loop iteration so operators see a single summary.
                try:
                    self._drain_alarm_summary()
                except Exception:
                    logger.debug("Alarm summary drain failed", exc_info=True)
                for endpoint_id, session in list(self._sessions.items()):
                    machine = self._machines_by_endpoint.get(endpoint_id)
                    if machine is None:
                        continue
                    host = getattr(session, "host", None)
                    connected = bool(getattr(host, "is_connected", False)) if host else False
                    if connected:
                        # Connected: run the event-liveness check (detects an
                        # acked-but-silent subscription, e.g. E40 event style)
                        # then skip the reconnect logic below.
                        self._maybe_start_liveness_check(endpoint_id, machine, session)
                        continue
                    last = self._last_reconnect_attempt.get(endpoint_id, 0.0)
                    if now < last:
                        continue
                    if endpoint_id in self._reconnect_inflight:
                        continue
                    failures = self._reconnect_failures.get(endpoint_id, 0) + 1
                    self._reconnect_failures[endpoint_id] = failures
                    self._last_reconnect_attempt[endpoint_id] = now + reconnect_delay(
                        interval, failures + 1, random.random()
                    )
                    self._reconnect_inflight.add(endpoint_id)
                    self._outage_since.setdefault(endpoint_id, now)
                    logger.warning(
                        "Reconnect watchdog: %s is disconnected, restarting session.",
                        endpoint_id,
                    )
                    self._escalate_outage(endpoint_id, machine, host, failures, now)
                    threading.Thread(
                        target=self._restart_disconnected,
                        args=(endpoint_id, machine, session),
                        name=f"Reconnect-{endpoint_id}",
                        daemon=True,
                    ).start()
                time.sleep(interval)

        self._reconnect_thread = threading.Thread(
            target=loop, name="ReconnectWatchdog", daemon=True
        )
        self._reconnect_thread.start()


    # Consecutive failed reconnects before the watchdog stops repeating one
    # WARNING and says, once, what it actually knows.
    OUTAGE_ESCALATE_AFTER = 3


    def _escalate_outage(
        self,
        endpoint_id: str,
        machine: MachineConfig,
        host: Any,
        failures: int,
        now: float,
    ) -> None:
        """Explain a persistent outage once, instead of repeating a WARNING.

        A rig was found restarting the same session every 30 s for 40 minutes
        with nothing in the log but the identical "is disconnected" line. The
        two failure modes behind that look the same from outside and need
        completely different fixes, so name which one it is:

          NOT_COMMUNICATING/DISABLED - the TCP connect itself is not
            completing (wrong address, port closed, firewall, or the tool's
            single HSMS peer slot is already taken - including by an orphaned
            connection this service left behind).
          WAIT_CRA/WAIT_DELAY - TCP is up and Select succeeded, but the tool
            never answered S1F13 with S1F14, so it is reachable and refusing
            to establish communications (typically OFF-LINE, or a device-ID
            mismatch).

        secsgem reports connect failures only at DEBUG, so without this an
        operator has nothing to go on.
        """
        if failures < self.OUTAGE_ESCALATE_AFTER:
            return
        if endpoint_id in self._outage_escalated:
            return
        self._outage_escalated.add(endpoint_id)
        down_for = now - self._outage_since.get(endpoint_id, now)
        state = "unknown"
        transport = "unknown"
        try:
            state = getattr(host.communication_state.current, "name", "unknown")
        except Exception:
            logger.debug("Could not read communication state", exc_info=True)
        try:
            connection = getattr(host.protocol, "_connection", None)
            transport = str(bool(getattr(connection, "_connected", False)))
        except Exception:
            logger.debug("Could not read transport state", exc_info=True)
        detail = (
            f"{endpoint_id} has not connected for {down_for:.0f}s over "
            f"{failures} attempts. target={machine.host}:{machine.port} "
            f"hsms_mode={machine.hsms_mode} device_id={machine.secs_device_id} "
            f"gem_state={state} tcp_connected={transport}. "
            + (
                "TCP is up but the tool never answered S1F13 with S1F14 - it "
                "is reachable and declining to establish communications "
                "(check the tool is ON-LINE and that the device/session ID "
                "matches)."
                if state in ("WAIT_CRA", "WAIT_DELAY")
                else "The transport is not up - check the address, the port, "
                "the firewall, and that no other client already holds the "
                "tool's HSMS connection."
            )
        )
        logger.error("Reconnect watchdog: %s", detail)
        self._publish_health(machine, "reconnect_failing", detail)


    def _restart_disconnected(
        self,
        endpoint_id: str,
        machine: MachineConfig,
        session: SecsMachineSession,
    ) -> None:
        try:
            if not self._running:
                return
            try:
                session.stop()
            except Exception:
                logger.exception(
                    "Stopping %s before reconnect failed; the previous HSMS "
                    "connection may still be open", endpoint_id,
                )
            # Identity check and start are atomic under the reconcile lock so
            # a concurrent restart command cannot swap _sessions[endpoint_id]
            # between them and leave this thread starting a dead session.
            with self._reconcile_lock:
                if not self._running or self._sessions.get(endpoint_id) is not session:
                    return
                self._advance_generation(endpoint_id)
                session.start()
            self._publish_health(machine, "reconnect_attempted")
        except Exception as exc:
            logger.error("Reconnect failed for %s: %s", endpoint_id, exc)
            self._publish_health(machine, "reconnect_failed", str(exc))
        finally:
            self._reconnect_inflight.discard(endpoint_id)


    def _maybe_start_liveness_check(
        self,
        endpoint_id: str,
        machine: MachineConfig,
        session: SecsMachineSession,
    ) -> None:
        """Spawn an event-liveness check for a connected machine, if one is
        due and none is already outstanding.

        Guarded by `_liveness_inflight` so a tool that stays
        connected-but-silent for longer than T3 (the common OFF-LINE case,
        since `offline_alarmed` suppresses `alarmed` here) gets one
        outstanding check, not a new thread every watchdog tick.
        """
        profile = self._profile_for(machine)
        sv = profile.health_last_event_svid
        if not sv or not machine.event_subscription_enabled:
            return
        host_obj = getattr(session, "host", None)
        delivered = bool(getattr(host_obj, "last_event_time", None))
        state = self._event_liveness.get(endpoint_id, {})
        if delivered or state.get("alarmed") or endpoint_id in self._liveness_inflight:
            return
        # The liveness S1F3 round-trip can block for up to T3=45s against a
        # wedged tool (OFF-LINE DaVinci, dead NIC). Run it on its own thread
        # so one unresponsive machine can never stall the reconnect
        # scheduling of the other 21.
        self._liveness_inflight.add(endpoint_id)
        threading.Thread(
            target=self._guarded_liveness,
            args=(endpoint_id, machine, session),
            name=f"Liveness-{endpoint_id}",
            daemon=True,
        ).start()


    def _guarded_liveness(
        self,
        endpoint_id: str,
        machine: MachineConfig,
        session: SecsMachineSession,
    ) -> None:
        """Liveness check on its own thread so its S1F3 round-trip (up to
        T3) can never delay the watchdog for other machines."""
        try:
            self._check_event_liveness(machine, session)
        except Exception:
            logger.debug(
                "Event-liveness check failed for %s", endpoint_id, exc_info=True,
            )
        finally:
            self._liveness_inflight.discard(endpoint_id)


    def _check_event_liveness(
        self,
        machine: MachineConfig,
        session: SecsMachineSession,
    ) -> None:
        """Detect a connected tool whose subscription was acknowledged but is
        delivering no S6F11 reports (the silent-failure symptom of the DaVinci
        being in E40 event-report style, or of reports being spooled).

        Strategy: poll the tool's LastEventID status variable. It advances on
        every collection event the equipment fires internally, independent of
        report delivery. If it moves past its post-connect baseline while
        host.last_event_time is still None, no reports are reaching us -> alarm.
        Polling stops once the first S6F11 arrives (or once alarmed), so this
        adds at most one tiny S1F3 per watchdog tick during the silent window.
        """
        profile = self._profile_for(machine)
        sv = profile.health_last_event_svid
        if not sv or not machine.event_subscription_enabled:
            return  # feature not applicable to this profile/machine

        host = getattr(session, "host", None)
        if host is None:
            return
        state = self._event_liveness.setdefault(
            machine.endpoint_id,
            {"connect_ts": time.time(), "baseline": None, "alarmed": False,
             "offline_alarmed": False, "spool_alarmed": False},
        )
        delivered = getattr(host, "last_event_time", None) is not None

        # Fast paths that avoid an SVID poll entirely.
        if delivered:
            if state.get("alarmed"):
                state["alarmed"] = False
                self._publish_health(
                    machine, "event_reports_ok",
                    "S6F11 collection-event reports are now flowing.",
                )
            state["offline_alarmed"] = False
            return
        if state.get("alarmed"):
            return  # already raised once; don't repoll or spam

        ee = profile.health_events_enabled_svid
        spool_sv = profile.health_spool_count_svid
        svids = [sv] + ([ee] if ee else []) + ([spool_sv] if spool_sv else [])
        values = session.request_svids(svids)
        current = values.get(sv)

        connect_ts = state.get("connect_ts")
        seconds = (
            time.time() - float(connect_ts)
            if isinstance(connect_ts, (int, float))
            else 0.0
        )

        # OFF-LINE / unresponsive detection: a connected DaVinci that is in
        # OFF-LINE control state answers establish-comm but ignores S1F3, so the
        # status poll returns nothing. After the grace window that is a reliable
        # "tool is OFF-LINE (or comms wedged) and will report no events" signal.
        if current is None:
            if seconds >= float(self.config.event_liveness_grace_sec) and not state.get("offline_alarmed"):
                state["offline_alarmed"] = True
                self._publish_health(
                    machine, "no_status_response",
                    "Connected but the tool is not answering S1F3 status polls "
                    "(LastEventID). It is most likely in OFF-LINE control state, "
                    "so it ignores the subscription/status/alarm requests and "
                    "sends no events. Put the tool ON-LINE at the console, or set "
                    "request_online: true for this machine in production.yaml.",
                )
            return
        # Got a status response -> tool is ON-LINE and responsive again.
        if state.get("offline_alarmed"):
            state["offline_alarmed"] = False

        # Spool detection: the tool buffered messages during a host outage that
        # we do not auto-drain (no S6F23). Surface so they aren't silently lost.
        if spool_sv is not None:
            try:
                spool = int(values.get(spool_sv) or 0)
            except (TypeError, ValueError):
                spool = 0
            if spool > 0 and not state.get("spool_alarmed"):
                state["spool_alarmed"] = True
                # Do NOT tell the operator to disable tool-side spooling: the
                # spool is what preserved these events across the outage, and
                # the middleware *can* collect them. `drain_spool_on_connect`
                # sends the S6F23 that empties it, after the subscription is
                # rebuilt so the re-sent S6F11/S5F1 land on live reports
                # (secs_runtime._provision_after_connect). It is off by
                # default, which is the only reason a backlog can sit here.
                self._publish_health(
                    machine, "spooled_messages_pending",
                    f"Tool reports SpoolCountActual={spool}: it spooled messages "
                    "during a host outage, and they are still on the equipment. "
                    "Set `drain_spool_on_connect: true` for this machine in "
                    "production.yaml - the middleware then sends S6F23 on each "
                    "connect and the tool retransmits the backlog through the "
                    "normal event path. Until then these events will not reach "
                    "the CSV files or the dashboard.",
                )
            elif spool == 0 and state.get("spool_alarmed"):
                state["spool_alarmed"] = False

        if state.get("baseline") is None:
            state["baseline"] = current  # first reading after connect

        decision = event_liveness_decision(
            baseline=state.get("baseline"),
            current=current,
            delivered=delivered,
            seconds_since_connect=seconds,
            grace=float(self.config.event_liveness_grace_sec),
            alarmed=bool(state.get("alarmed")),
        )
        if decision == "alarm":
            state["alarmed"] = True
            events_enabled = values.get(ee) if ee else "?"
            detail = (
                "Subscription acknowledged but NO S6F11 reports received while "
                f"the tool's LastEventID advanced ({state.get('baseline')} -> "
                f"{current}); EventsEnabled={events_enabled}. The DaVinci "
                "HostInterface is likely configured for E40 event-report style "
                "(collection events are sent on Stream 16, not S6F11) or reports "
                "are being spooled. No telemetry will flow until the tool is "
                "switched to E30 / S6F11 reporting (see docs/OPERATIONS.md)."
            )
            logger.error("[%s] %s", machine.endpoint_id, detail)
            self._publish_health(machine, "no_event_reports", detail)


    def _start_svid_thread(
        self,
        machine: MachineConfig,
        profile: MachineProfile,
        session: SecsMachineSession,
    ) -> None:
        if not machine.svid_collection_enabled:
            return
        stop_event = threading.Event()
        self._svid_stop_events[machine.endpoint_id] = stop_event
        guard = self._session_guards.get(machine.endpoint_id)

        def loop() -> None:
            admin = SvidAdminConfig(self._admin_dir(machine), profile)
            mapper = CanonicalMapper(profile)
            while self._running and not stop_event.is_set():
                try:
                    state = admin.load()
                    if state.invalid_entries:
                        logger.warning(
                            "%s ignored invalid SVID entries: %s",
                            machine.endpoint_id,
                            state.invalid_entries,
                        )
                    if state.enabled and state.svids:
                        values = session.request_svids([item.svid for item in state.svids])
                        # v2 Track A: some hosts return {svid: None} for partial
                        # failures - filter those out so we don't publish noise.
                        clean = {k: v for k, v in values.items() if v is not None}
                        # The poll can block for up to T3, so the machine may
                        # have been stopped or repointed while it was out. The
                        # sample belongs to the profile that asked for it, and
                        # publishing it now would attribute it to whatever
                        # replaced that profile.
                        if clean and self._session_guards.get(
                            machine.endpoint_id
                        ) is guard:
                            ev = mapper.svid_event(machine, clean)
                            self.publisher.queue_event(ev)
                            self._queue_http_event(ev)
                    stop_event.wait(state.interval_sec)
                except SvidAdminError as exc:
                    logger.error("SVID admin config error for %s: %s", machine.endpoint_id, exc)
                    stop_event.wait(5)
                except Exception as exc:
                    logger.exception("SVID loop error for %s: %s", machine.endpoint_id, exc)
                    stop_event.wait(5)

        thread = threading.Thread(
            target=loop,
            name=f"SVID-{machine.endpoint_id}",
            daemon=True,
        )
        self._svid_threads[machine.endpoint_id] = thread
        thread.start()
