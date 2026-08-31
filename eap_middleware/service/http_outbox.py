"""Per-machine Linkstuffs HTTP outbox wiring."""


from __future__ import annotations


from pathlib import Path


from typing import (
    Optional,
)


from ..models import (
    CanonicalEvent,
    LinkstuffsHttpConfig,
    MachineConfig,
    MachineLinkstuffsHttpConfig,
)

from ..outbox import SQLiteOutbox

from ..profiles import (
    MachineProfile,
)


from ..linkstuffs_http import LinkstuffsHttpPublisher

from .constants import (
    logger,
)
from .helpers import (
    machine_http_outbox_path,
)
from .state import ServiceState


class HttpOutboxMixin(ServiceState):
    """Per-machine Linkstuffs HTTP outbox wiring."""


    @staticmethod
    def _sanitized_endpoint(endpoint_id: str) -> str:
        return "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in endpoint_id
        )


    def _machine_http_outbox_path(self, endpoint_id: str) -> Path:
        return machine_http_outbox_path(
            Path(self.config.paths.http_outbox_db), endpoint_id
        )


    def _migrate_legacy_http_outbox(self, endpoint_id: str, target: Path) -> None:
        """Adopt the pre-digest queue file, when it unambiguously belongs here.

        Older builds named the file from the sanitised endpoint id alone. That
        file may hold undelivered telemetry, so it is moved rather than
        abandoned - but only when exactly one configured machine could have
        written it. If two machines share the legacy name, its contents cannot
        be attributed and it is left in place for an operator to look at.
        """
        base = Path(self.config.paths.http_outbox_db)
        suffix = base.suffix or ".sqlite3"
        safe = self._sanitized_endpoint(endpoint_id)
        legacy = base.with_name(f"{base.stem}.{safe}{suffix}")
        if target.exists() or not legacy.exists():
            return
        claimants = [
            candidate.endpoint_id
            for candidate in self.config.machines
            if self._sanitized_endpoint(candidate.endpoint_id) == safe
        ]
        if len(claimants) != 1:
            logger.error(
                "Legacy HTTPS queue %s could have been written by any of %s; "
                "leaving it untouched rather than guessing whose telemetry it "
                "holds.", legacy, sorted(claimants),
            )
            return
        try:
            legacy.replace(target)
            for extra in ("-wal", "-shm"):
                sidecar = legacy.with_name(legacy.name + extra)
                if sidecar.exists():
                    sidecar.replace(target.with_name(target.name + extra))
        except OSError as exc:
            logger.error("Could not adopt legacy HTTPS queue %s: %s", legacy, exc)
            return
        logger.info("Adopted legacy HTTPS queue %s as %s", legacy, target)


    def _start_machine_http(self, machine: MachineConfig) -> None:
        route = self._effective_machine_http(machine)

        if not route.enabled:
            return
        outbox_path = self._machine_http_outbox_path(machine.endpoint_id)
        self._migrate_legacy_http_outbox(machine.endpoint_id, outbox_path)
        outbox = SQLiteOutbox(
            outbox_path,
            retention_days=self.config.outbox_retention_days,
        )
        publisher = LinkstuffsHttpPublisher(
            LinkstuffsHttpConfig(
                enabled=True,
                base_url=route.base_url,
                device_tokens={machine.display_name: route.device_token},
                timeout_sec=route.timeout_sec,
                retry_count=route.retry_count,
                retry_delay_sec=route.retry_delay_sec,
                verify_tls=route.verify_tls,
                allow_insecure=route.allow_insecure,
            ),
            outbox,
            owner_display_name=machine.display_name,
        )
        self._http_outboxes[machine.endpoint_id] = outbox
        self._http_publishers[machine.endpoint_id] = publisher
        outbox.start_maintenance()
        publisher.start()


    def _effective_machine_http(
        self, machine: MachineConfig
    ) -> MachineLinkstuffsHttpConfig:
        route = machine.linkstuffs_http
        if not (route.enabled or route.base_url or route.device_token):
            global_route = self.config.linkstuffs_http
            route = type(route)(
                enabled=global_route.enabled,
                base_url=global_route.base_url,
                device_token=global_route.device_tokens.get(machine.display_name, ""),
                verify_tls=global_route.verify_tls,
                allow_insecure=global_route.allow_insecure,
                timeout_sec=global_route.timeout_sec,
                retry_count=global_route.retry_count,
                retry_delay_sec=global_route.retry_delay_sec,
            )
        return route


    def _stop_machine_http(
        self, endpoint_id: str, deadline: Optional[float] = None
    ) -> None:
        publisher = self._http_publishers.pop(endpoint_id, None)
        if publisher is not None:
            publisher.stop(self._budget(deadline, 10.0))
        outbox = self._http_outboxes.pop(endpoint_id, None)
        if outbox is not None:
            outbox.stop_maintenance(self._budget(deadline, 5.0))


    def _queue_http_event(self, event: CanonicalEvent) -> None:
        publisher = self._http_publishers.get(event.endpoint_id)
        if publisher is not None:
            publisher.queue_event(event)
        else:
            # Compatibility seam for direct callback tests and disabled HTTP.
            self.http_publisher.queue_event(event)


    def _queue_http_attributes(
        self, machine: MachineConfig, profile: MachineProfile
    ) -> None:
        publisher = self._http_publishers.get(machine.endpoint_id)
        if publisher is not None:
            publisher.queue_machine_attributes(machine, profile)
        else:
            self.http_publisher.queue_machine_attributes(machine, profile)
