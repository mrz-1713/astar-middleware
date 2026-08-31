"""Runtime adapter around the existing secsgem host implementation."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .models import MachineConfig

logger = logging.getLogger(__name__)

# A tool that refuses or times out S2F33/35/37 at connect time is usually just
# busy. Giving up after one attempt left it connected and subscribed to
# nothing, silently, until somebody restarted the machine by hand.
SUBSCRIBE_MAX_ATTEMPTS = 5
SUBSCRIBE_RETRY_DELAY_SEC = 5.0
SUBSCRIBE_RETRY_MAX_DELAY_SEC = 60.0


class SecsRuntimeUnavailable(RuntimeError):
    """Raised when the secsgem runtime cannot be used."""

    pass


class SecsMachineSession:
    """One isolated SECS/GEM session for a configured machine."""

    def __init__(
        self,
        machine: MachineConfig,
        event_callback: Callable[[MachineConfig, int, Dict[str, Any]], None],
        alarm_callback: Callable[[MachineConfig, Dict[str, Any]], None],
        connect_callback: Callable[[MachineConfig], None],
        disconnect_callback: Callable[[MachineConfig], None],
        subscription_path: Optional[str] = None,
        dv_name_by_id: Optional[Dict[int, str]] = None,
        events_enabled_svid: Optional[int] = None,
    ):
        self.machine = machine
        self.event_callback = event_callback
        self.alarm_callback = alarm_callback
        self.connect_callback = connect_callback
        self.disconnect_callback = disconnect_callback
        self.subscription_path = subscription_path
        # DVID -> name map (for labelling E40 S16F9 VID/V pairs). From the profile.
        self.dv_name_by_id = dv_name_by_id or {}
        # SVID holding the tool's own enabled-collection-event list, read back
        # after subscribing so the acknowledgement isn't the only evidence.
        self.events_enabled_svid = events_enabled_svid
        self.host: Any = None
        # Connection generation. Every start() opens a new one and every stop()
        # closes the current one, so work that was kicked off by a previous
        # connection can tell that it has been superseded and stand down
        # instead of provisioning a session that no longer exists.
        self._epoch = 0
        self._stopped = True
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._provision_threads: List[threading.Thread] = []
        # Serializes S1F3/S1F1 status round-trips (SVID admin thread vs the
        # watchdog's liveness thread); secsgem is not safe for concurrent
        # send_and_waitfor_response on one connection.
        self._s1f3_lock = threading.Lock()

    def _is_current(self, epoch: int) -> bool:
        with self._lock:
            if epoch != self._epoch or self._stopped:
                return False
        return bool(getattr(self.host, "is_connected", False))

    def start(self) -> None:
        try:
            from gateway.host import GatewayHost, create_host_settings
        except Exception as exc:
            raise SecsRuntimeUnavailable(
                "secsgem runtime is unavailable. Install requirements.txt and "
                "verify the gateway.host module on the Windows server."
            ) from exc

        with self._lock:
            self._epoch += 1
            epoch = self._epoch
            self._stopped = False
        self._wake.clear()

        settings = create_host_settings(
            host=self.machine.host,
            port=self.machine.port,
            device_id=self.machine.secs_device_id,
            mode=self.machine.hsms_mode,
            bind_address=self.machine.hsms_bind_address,
            timers=getattr(self.machine, "hsms_timers", None),
        )
        self.host = GatewayHost(
            settings=settings,
            tool_id=self.machine.endpoint_id,
            on_event=lambda _tool, ceid, data: self.event_callback(self.machine, ceid, data),
            on_connect=lambda _tool: self._on_connect(epoch),
            on_disconnect=lambda _tool: self.disconnect_callback(self.machine),
            dv_name_by_id=self.dv_name_by_id,
        )
        def on_alarm(_tool: str, alarm: Dict[str, Any]) -> None:
            self.alarm_callback(self.machine, alarm)

        self.host.set_alarm_callback(on_alarm)
        self.host.enable()

    def stop(self, timeout: float = 10.0) -> None:
        # Close the generation before touching the host, so a provisioning
        # worker that is mid-handshake sees it is finished and stops rather
        # than continuing to configure a session being torn down.
        with self._lock:
            self._epoch += 1
            self._stopped = True
            threads = list(self._provision_threads)
            self._provision_threads.clear()
        self._wake.set()
        host = self.host
        if host is not None:
            # retire(), not disable(): this host is being replaced, and a
            # replaced host that keeps its socket goes on acknowledging
            # S6F11/S5F1 into the pipeline while occupying the tool's only
            # HSMS peer slot, so the next host can never connect. retire()
            # detaches the callbacks and guarantees the socket is closed even
            # when secsgem's own disable() path fails. Failure to retire is
            # raised, not swallowed: the caller has to know the old
            # connection may still be live before it starts a new one.
            host.retire()
        for thread in threads:
            if thread is not threading.current_thread():
                # Joined rather than abandoned: an unjoined worker can still be
                # inside a blocking SECS round-trip and issue subscription
                # messages after the next session has already started. The
                # epoch bumped above is what makes an expired join safe - the
                # worker checks it and stands down.
                thread.join(timeout=max(0.05, timeout))
                if thread.is_alive():
                    logger.warning(
                        "Provisioning worker for %s did not finish within "
                        "%.1fs; its generation is retired so it will stand "
                        "down at its next checkpoint",
                        self.machine.endpoint_id, timeout,
                    )

    def request_svids(self, svids: List[int]) -> Dict[int, Any]:
        if self.host is None or not getattr(self.host, "is_connected", False):
            return {}
        # Serialize S1F3 round-trips: the SVID admin thread and the watchdog's
        # liveness thread can both poll this session, and secsgem's pending-
        # message registry is not safe for concurrent send_and_waitfor_response.
        with self._s1f3_lock:
            return self.host.request_status(svids)

    def _on_connect(self, epoch: int) -> None:
        self.connect_callback(self.machine)
        # The connect callback fires from inside secsgem's communication
        # state-machine thread. subscribe_to_events / enable_all_alarms each
        # issue blocking send_and_waitfor_response calls (up to T3=45s), so
        # running them inline would stall that thread during setup. Do the
        # SECS round-trips on a short-lived worker thread instead.
        worker = threading.Thread(
            target=self._provision_after_connect,
            args=(epoch,),
            name=f"Provision-{self.machine.endpoint_id}",
            daemon=True,
        )
        with self._lock:
            if epoch != self._epoch or self._stopped:
                return  # superseded between connect and here
            self._provision_threads = [
                thread for thread in self._provision_threads if thread.is_alive()
            ]
            self._provision_threads.append(worker)
            # Started INSIDE the lock, on purpose. stop() snapshots this list
            # under the same lock and joins what it finds. With start() outside
            # it, stop() could take the snapshot in the window between the
            # append and the start - and then either join a thread that has not
            # begun (RuntimeError) or, worse, return while the worker starts
            # immediately afterwards and outlives the session it belongs to,
            # issuing SECS round-trips against a connection that has been torn
            # down. The new thread's first act is to take this same lock, so it
            # simply waits until this block exits; there is no deadlock.
            worker.start()

    def _subscribe_with_retry(self, epoch: int, host: Any) -> bool:
        """Set up event reporting, retrying while the connection holds.

        A refused or timed-out S2F33/35/37 was previously logged once and left
        alone, so a tool that was merely busy at connect time stayed connected
        and subscribed to nothing - indistinguishable, from the outside, from a
        tool with nothing to report.

        `host` is the GatewayHost captured when this worker started: a worker
        superseded by a stop/start must keep talking to its own connection and
        never interleave round-trips on the new generation's host.
        """
        delay = SUBSCRIBE_RETRY_DELAY_SEC
        for attempt in range(1, SUBSCRIBE_MAX_ATTEMPTS + 1):
            if not self._is_current(epoch) or self.host is not host:
                return False
            if host.subscribe_to_events(
                self.subscription_path,
                events_enabled_svid=self.events_enabled_svid,
                # Only on the first attempt of this connection. A retry means
                # the tool refused or timed out our own define/link/enable, and
                # re-running delete-all would discard any band it did accept.
                reset_first=(
                    attempt == 1
                    and bool(getattr(
                        self.machine, "reset_subscription_on_connect", False
                    ))
                ),
                # Checked between bands so a stop() lands promptly instead of
                # waiting out every remaining define/link/enable round trip.
                should_continue=lambda: (
                    self._is_current(epoch) and self.host is host
                ),
            ):
                if attempt > 1:
                    logger.info(
                        "Event subscription for %s succeeded on attempt %d",
                        self.machine.endpoint_id, attempt,
                    )
                return True
            if attempt == SUBSCRIBE_MAX_ATTEMPTS:
                break
            logger.warning(
                "Event subscription attempt %d/%d failed for %s; retrying in "
                "%.0fs", attempt, SUBSCRIBE_MAX_ATTEMPTS,
                self.machine.endpoint_id, delay,
            )
            if self._wake.wait(delay):
                return False
            delay = min(delay * 2, SUBSCRIBE_RETRY_MAX_DELAY_SEC)
        logger.error(
            "Event subscription for %s failed %d times; the tool is connected "
            "but will report no collection events",
            self.machine.endpoint_id, SUBSCRIBE_MAX_ATTEMPTS,
        )
        return False

    def _provision_after_connect(self, epoch: int) -> None:
        # Request ON-LINE first (opt-in). A DaVinci in OFF-LINE control state
        # ignores S2F33/35/37 + S1F3 + S5F3 and sends no events, so the
        # subscription below would silently define nothing. S1F17 does not take
        # REMOTE control - it only lifts the tool out of OFF-LINE so data can
        # flow. Gated by MachineConfig.request_online (default False).
        if not self._is_current(epoch):
            return
        # Capture the connection this worker owns up front. Everything below
        # uses `host`, never `self.host`: a stop/start during provisioning
        # rebinds self.host to the next generation's GatewayHost, and a
        # superseded worker must keep talking to its own connection rather than
        # interleaving round-trips on the new one (see _subscribe_with_retry).
        host = self.host
        if host is not None and getattr(self.machine, "request_online", False):
            if host.request_online():
                logger.info("Requested ON-LINE for %s", self.machine.endpoint_id)
            else:
                logger.warning(
                    "Request ON-LINE failed/denied for %s (tool may stay "
                    "OFF-LINE and report no events)", self.machine.endpoint_id
                )
        if (
            self.subscription_path
            and host is not None
            and self.machine.event_subscription_enabled
        ):
            self._subscribe_with_retry(epoch, host)
            # `host`, not `self.host`: a stop/start between the subscribe and
            # here rebinds self.host to the next generation's GatewayHost,
            # whose band results belong to a different connection.
            refused = [
                band for band, accepted
                in getattr(host, "subscription_band_results", {}).items()
                if not accepted
            ]
            if refused:
                logger.warning(
                    "Subscription bands refused for %s: %s (the accepted bands "
                    "keep reporting)", self.machine.endpoint_id, refused,
                )
        # Recover any messages the tool spooled while the host was disconnected
        # (opt-in S6F23). Done after subscribing so the re-sent S6F11/S5F1 land
        # on the freshly-defined reports.
        if not self._is_current(epoch) or self.host is not host:
            return
        if host is not None and getattr(self.machine, "drain_spool_on_connect", False):
            host.drain_spool()
        # Enable alarm reporting (S5F3) so S5F1 alarm reports are guaranteed to
        # arrive. This is opt-IN per machine via `enable_alarms: true`, and it
        # is OFF by default everywhere except the MG (config.py derives the
        # default from `nexgen_safeguards`, and MachineConfig.enable_alarms is
        # False). Leaving it off means the middleware collects whatever alarms
        # the tool already has enabled and never writes to the tool's alarm
        # configuration - S5F3 with a zero-length ALID list enables *every*
        # alarm and that setting persists on the equipment, so switching it on
        # is a change to a shared tool, not a host-local one. It is on for the
        # MG because that profile also ships the alarm rate limiter (50/window)
        # that absorbs the resulting volume. If a tool ships with its alarms
        # disabled and this stays off, no S5F1 will ever arrive.
        if not self._is_current(epoch) or self.host is not host:
            return
        if host is not None and getattr(self.machine, "enable_alarms", False):
            if not host.enable_all_alarms():
                logger.warning(
                    "Enable-all-alarms failed for %s", self.machine.endpoint_id
                )
