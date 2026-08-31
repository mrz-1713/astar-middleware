"""Resolves a machine's profile, subscription file, and mapper."""


from __future__ import annotations


import json


import os


from pathlib import Path


from typing import (
    Optional,
)


from ..config import ConfigError


from ..mapper import CanonicalMapper

from ..models import (
    MachineConfig,
)


from ..profiles import (
    MachineProfile,
    profile_with_ceid_overrides,
    profile_with_subscription_file,
)


from ..svid_admin import SvidAdminConfig


from .state import ServiceState


class WiringMixin(ServiceState):
    """Resolves a machine's profile, subscription file, and mapper."""


    def _prepare_machine(self, machine: MachineConfig, profile: MachineProfile) -> None:
        SvidAdminConfig(self._admin_dir(machine), profile).ensure_default_files()
        self.publisher.queue_machine_connect(machine)
        self.publisher.queue_machine_attributes(machine, profile)
        # HTTP REST upstream gets the same attributes (no connect/disconnect
        # topic - HTTP API is implicit per-device).
        self._queue_http_attributes(machine, profile)


    def _admin_dir(self, machine: MachineConfig) -> Path:
        configured = machine.storage.admin_config_path or machine.admin_config_path
        if configured:
            return Path(configured)
        return Path(self.config.paths.install_dir) / "machines" / machine.display_name / "config"


    def _subscription_path_for(self, machine: MachineConfig) -> Optional[str]:
        """Subscription definition, extended with simulator-only CEID aliases."""
        profile = self.profiles.get(machine.machine_profile)
        configured = machine.event_subscription_path or profile.event_subscription_path
        overrides = machine.simulator.ceid_overrides
        inline = machine.simulator.event_definitions
        if not machine.is_simulated or (not overrides and not inline):
            return configured
        source: Optional[Path] = None
        data: object = None
        if configured:
            source = Path(configured)
            if not source.is_file() and not source.is_absolute():
                source = Path(__file__).resolve().parent.parent / source
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError) as exc:
                raise ConfigError(
                    f"Machine {machine.endpoint_id}: cannot extend simulator "
                    f"subscription {configured}: {exc}"
                ) from exc
        if data is None:
            data = {"reports": [], "events": [], "dvid_names": {}}
        if inline:
            inline_copy = json.loads(json.dumps(inline))
            if inline_copy.get("events"):
                data = inline_copy
                source = None
            elif isinstance(data, dict):
                data.update(inline_copy)
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ConfigError(
                f"Machine {machine.endpoint_id}: simulator subscription must "
                "contain an events list"
            )
        effective = (
            profile_with_subscription_file(profile, str(source))
            if source is not None
            else profile
        )
        events = [dict(event) for event in data["events"] if isinstance(event, dict)]
        by_ceid = {
            int(event["ceid"]): event
            for event in events
            if "ceid" in event
        }
        for event_type, target_ceid in overrides.items():
            if target_ceid in by_ceid:
                continue
            source_ceid = next(
                (
                    ceid
                    for ceid in sorted(effective.ceid_aliases)
                    if effective.resolve_event(ceid=ceid).event_type == event_type
                ),
                None,
            )
            source_event = by_ceid.get(source_ceid) if source_ceid is not None else None
            clone = dict(source_event or {})
            clone.update({"ceid": target_ceid, "name": event_type})
            clone.setdefault("rptids", [])
            events.append(clone)
        data["events"] = events

        target = self._admin_dir(machine) / "simulator" / "EventSubscription.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return str(target)


    def _profile_for(self, machine: MachineConfig) -> MachineProfile:
        """The machine's profile with its own EventSubscription.json layered on.

        Cached per endpoint: the overlay reads a file, and this is called on
        every inbound event. A changed file is picked up on restart, same as
        every other production.yaml value.
        """
        cached = self._profiles_by_endpoint.get(machine.endpoint_id)
        if cached is not None:
            return cached
        base = self.profiles.get(machine.machine_profile)
        subscription_path = (
            self._subscription_path_for(machine)
            if machine.is_simulated
            else machine.event_subscription_path or base.event_subscription_path
        )
        profile = profile_with_subscription_file(
            base,
            subscription_path,
        )
        if machine.is_simulated:
            profile = profile_with_ceid_overrides(
                profile, machine.simulator.ceid_overrides
            )
        self._profiles_by_endpoint[machine.endpoint_id] = profile
        return profile


    def _mapper(self, machine: MachineConfig) -> CanonicalMapper:
        return CanonicalMapper(
            self._profile_for(machine),
            tracker=self.job_tracker,
        )
