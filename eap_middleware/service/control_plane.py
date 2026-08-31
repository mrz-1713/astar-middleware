"""Status file, supervisor loop, and the GUI command channel."""


from __future__ import annotations


import ssl

import threading

import time

import urllib.error

import urllib.request


from pathlib import Path


from typing import (
    Dict,
)


from ..control import consume_commands, file_revision, write_status

from ..config import ConfigError, load_service_config


from ..models import (
    MachineConfig,
)


from .constants import (
    MIRROR_POLL_INTERVAL_SEC,
    logger,
)
from .helpers import (
    resolve_data_path,
)
from .state import ServiceState


class ControlMixin(ServiceState):
    """Status file, supervisor loop, and the GUI command channel."""


    def _write_status(self) -> None:
        machines: Dict[str, Dict[str, object]] = {}
        try:
            journal_stats: Dict[str, int] = self.journal.stats()
        except Exception:
            logger.debug("Journal stats unavailable", exc_info=True)
            journal_stats = {}
        configured = {m.endpoint_id: m for m in self.config.machines}
        for endpoint_id, machine in configured.items():
            session = self._sessions.get(endpoint_id)
            runtime = dict(self._runtime_states.get(endpoint_id, {}))
            connected = bool(
                session is not None
                and getattr(getattr(session, "host", None), "is_connected", False)
            )
            host = getattr(session, "host", None) if session is not None else None
            http_publisher = self._http_publishers.get(endpoint_id)
            profile = self._profile_for(machine)
            runtime.update(
                {
                    "endpoint_id": endpoint_id,
                    "display_name": machine.display_name,
                    "machine_profile": machine.machine_profile,
                    "profile_provenance": profile.notes,
                    "runtime_mode": machine.runtime_mode,
                    "enabled": machine.enabled,
                    "hsms_state": "Selected" if connected else "Disconnected",
                    "gem_state": "Communicating" if connected else "Not communicating",
                    "generation": self._generations.get(endpoint_id, 0),
                    "retry_count": self._reconnect_failures.get(endpoint_id, 0),
                    "subscription_bands": dict(
                        getattr(host, "subscription_band_results", {}) or {}
                    ),
                    "missing_enabled_events": list(
                        getattr(host, "missing_enabled_events", []) or []
                    ),
                    "simulator_state": self._simulator_status(endpoint_id),
                    "https_queue": (
                        self._http_outboxes[endpoint_id].stats()
                        if endpoint_id in self._http_outboxes
                        else {"pending": 0, "sent": 0, "dead": 0}
                    ),
                    "last_http_status": getattr(
                        http_publisher, "last_http_status", None
                    ),
                    "upstream_routes": [
                        route
                        for route, enabled in (
                            ("https", machine.linkstuffs_http.enabled),
                            ("mqtt", self.config.linkstuffs.enabled),
                        )
                        if enabled and not machine.offline_test_mode
                    ],
                }
            )
            machines[endpoint_id] = runtime
        write_status(
            self._control_data_dir(),
            {
                "configuration_revision": self._revision,
                "last_invalid_config": self._last_invalid_config,
                # Backlog of accepted-but-not-yet-written traffic. Anything
                # persistently non-zero here means a sink is falling behind.
                "ingress_journal": journal_stats,
                "cross_generation_retransmit_window_sec": (
                    self.config.cross_generation_retransmit_window_sec
                ),
                "storage": {
                    **self.storage_monitor.status(),
                    "database_sizes": {
                        "ingress_journal": self.journal.database_size_bytes(),
                        "mqtt_outbox": self.outbox.database_size_bytes(),
                        "https_outbox": self.http_outbox.database_size_bytes(),
                        "legacy_outbox": self.legacy_api_outbox.database_size_bytes(),
                    },
                    "queue_depths": {
                        "mqtt": self.outbox.stats(),
                        "https": self.http_outbox.stats(),
                        "legacy": self.legacy_api_outbox.stats(),
                    },
                },
                "active_release": self._active_release_identity(),
                "machines": machines,
                "command_results": self._command_results,
                "updated_at": time.time(),
            },
        )

    def _active_release_identity(self) -> str:
        marker = Path(self.config.paths.install_dir) / "ACTIVE_RELEASE"
        try:
            return marker.read_text(encoding="ascii").strip()
        except OSError:
            return "development-or-legacy-install"


    def _control_data_dir(self) -> Path:
        return resolve_data_path(
            self.config.paths.control_dir,
            self.config.paths.install_dir,
            "control",
        )


    def _start_supervisor(self) -> None:
        if self._supervisor_thread is not None:
            return

        def loop() -> None:
            next_status = 0.0
            next_replay = 0.0
            next_purge = time.monotonic() + 3600.0
            while self._running:
                changed = False
                if self.config_path is not None:
                    revision = file_revision(self.config_path)
                    if revision and revision != self._revision:
                        try:
                            candidate = load_service_config(
                                self.config_path, profiles=self.profiles
                            )
                            self.reconcile(candidate, revision=revision)
                            self._last_invalid_config = ""
                            changed = True
                        except (ConfigError, OSError) as exc:
                            message = str(exc)
                            if message != self._last_invalid_config:
                                logger.error(
                                    "Rejected config revision %s: %s",
                                    revision[:12],
                                    message,
                                )
                                changed = True
                            self._last_invalid_config = message
                            # Remember neither the bad revision nor candidate:
                            # a corrected atomic replacement is retried.
                try:
                    for command in consume_commands(self._control_data_dir()):
                        self._process_command(command)
                        changed = True
                except Exception:
                    # Must never kill this thread: it also owns hot config
                    # reload, journal replay and journal purge (below). A
                    # transient OSError here (e.g. Windows AV/backup briefly
                    # holding a command file) would otherwise stop all four
                    # silently, with nothing but a stale runtime_status.json
                    # to notice by.
                    logger.exception("Command processing failed")
                now = time.monotonic()
                if now >= next_replay:
                    # Catches up a sink that was briefly broken - a full disk,
                    # a locked CSV directory - without waiting for a restart.
                    try:
                        self._replay_journal()
                    except Exception:
                        logger.exception("Ingress journal replay failed")
                    next_replay = now + 5.0
                if now >= next_purge:
                    try:
                        purged = self.journal.purge_old()
                        if purged:
                            logger.info(
                                "Ingress journal purged %d entries past "
                                "retention", purged,
                            )
                    except Exception:
                        logger.exception("Ingress journal purge failed")
                    next_purge = now + 3600.0
                if changed or now >= next_status:
                    try:
                        self._write_status()
                    except Exception:
                        # A status-write fault must never kill the supervisor:
                        # it is the thread that watches the configuration,
                        # drains the command inbox, replays the journal and
                        # retries mirrors for every machine.
                        logger.exception("Status snapshot write failed")
                    next_status = now + 1.0
                time.sleep(0.25)

        self._supervisor_thread = threading.Thread(
            target=loop, name="ConfigurationSupervisor", daemon=True
        )
        self._supervisor_thread.start()
        self._start_mirror_worker()


    def _start_mirror_worker(self) -> None:
        """Drain the network-CSV mirror queue on its own thread.

        This used to run inline in the supervisor loop. A copy to an
        unreachable SMB share blocks for the OS timeout, so one dead share
        stalled the thread that reloads configuration, drains the control
        command inbox, replays the ingress journal and writes
        runtime_status.json - and a status file older than
        SERVICE_STALE_AFTER_SEC makes the control panel report the service
        dead. Nothing else in the supervisor's job depends on a mirror, so it
        does not belong on that thread.
        """
        if self._mirror_thread is not None:
            return

        def loop() -> None:
            while self._running:
                try:
                    self.csv_writer.retry_mirrors()
                except Exception:
                    logger.exception("CSV mirror retry failed")
                # The queue itself carries the per-task backoff; this interval
                # only decides how often we look for work that has come due.
                if self._mirror_wake.wait(MIRROR_POLL_INTERVAL_SEC):
                    self._mirror_wake.clear()

        self._mirror_thread = threading.Thread(
            target=loop, name="CsvMirrorWorker", daemon=True
        )
        self._mirror_thread.start()


    def _process_command(self, command: Dict[str, object]) -> None:
        request_id = str(command.get("request_id", ""))
        action = str(command.get("action", ""))
        endpoint_id = str(command.get("endpoint_id", ""))
        result: Dict[str, object] = {
            "request_id": request_id,
            "action": action,
            "endpoint_id": endpoint_id,
            "completed_at": time.time(),
        }
        try:
            if action == "restart":
                machine = self._machines_by_endpoint.get(endpoint_id)
                if machine is None:
                    raise ValueError(f"{endpoint_id} is not running")
                # Serialize with reconcile so a stale reconnect thread cannot
                # observe _sessions[endpoint_id] mid-swap and start() the old
                # session after the new one is live (duplicate GatewayHost).
                with self._reconcile_lock:
                    self._stop_machine(endpoint_id, reason="stopped")
                    self._start_machine(machine)
                result["status"] = "ok"
            elif action == "test_connection":
                session = self._sessions.get(endpoint_id)
                if session is not None:
                    connected = bool(getattr(session.host, "is_connected", False))
                    identity: object = None
                    if connected and hasattr(session.host, "are_you_there"):
                        response = session.host.are_you_there()
                        if response is not None:
                            identity = session.host.settings.streams_functions.decode(
                                response
                            ).get()
                    result.update(
                        {
                            "status": "ok",
                            "connected": connected,
                            "identity": identity,
                            "detail": "current running session",
                        }
                    )
                else:
                    machine = next(
                        (m for m in self.config.machines if m.endpoint_id == endpoint_id),
                        None,
                    )
                    if machine is None:
                        raise ValueError(f"Unknown endpoint_id: {endpoint_id}")
                    result.update(self._test_stopped_machine(machine))
            elif action == "test_linkstuffs":
                machine = next(
                    (m for m in self.config.machines if m.endpoint_id == endpoint_id),
                    None,
                )
                if machine is None:
                    raise ValueError(f"Unknown endpoint_id: {endpoint_id}")
                result.update(self._test_linkstuffs(machine))
            else:
                raise ValueError(f"Unsupported action: {action}")
        except Exception as exc:
            result.update({"status": "error", "error": str(exc)})
        if request_id:
            self._command_results[request_id] = result
            if len(self._command_results) > 100:
                self._command_results.pop(next(iter(self._command_results)))


    @staticmethod
    def _test_stopped_machine(machine: MachineConfig) -> Dict[str, object]:
        if machine.is_simulated:
            return {
                "status": "error",
                "error": "Start the simulated machine to test its loopback pair",
            }
        from gateway.host import GatewayHost, create_host_settings

        host = GatewayHost(
            settings=create_host_settings(
                host=machine.host,
                port=machine.port,
                device_id=machine.secs_device_id,
                mode=machine.hsms_mode,
                bind_address=machine.hsms_bind_address,
                timers=machine.hsms_timers,
            ),
            tool_id=machine.endpoint_id,
        )
        try:
            host.enable()
            if not host.waitfor_communicating(timeout=5.0):
                raise TimeoutError("HSMS did not reach COMMUNICATING")
            response = host.are_you_there()
            if response is None:
                raise TimeoutError("S1F1 identity request received no S1F2")
            return {
                "status": "ok",
                "connected": True,
                "identity": host.settings.streams_functions.decode(response).get(),
            }
        finally:
            host.disable()


    def _test_linkstuffs(self, machine: MachineConfig) -> Dict[str, object]:
        route = self._effective_machine_http(machine)
        if not route.enabled or not route.base_url or not route.device_token:
            return {"status": "error", "error": "HTTPS route is incomplete"}
        context = None
        if not route.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        url = (
            f"{route.base_url.rstrip('/')}/api/v1/{route.device_token}/"
            "attributes?clientKeys=endpoint_id"
        )
        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "astar-eap-middleware/1.0"}
        )
        try:
            # Route URL is normalized and scheme-validated during config load.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=route.timeout_sec, context=context
            ) as response:
                return {"status": "ok", "http_status": response.status}
        except urllib.error.HTTPError as exc:
            return {"status": "error", "error": f"HTTP {exc.code}"}
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            return {"status": "error", "error": type(exc).__name__}
