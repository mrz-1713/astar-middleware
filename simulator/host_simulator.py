"""Host-side simulator: the SECS/GEM HOST half of the link.

Every other simulator in this package pretends to be a tool. This one
pretends to be the EAP, so an operator can point it at a real (or
simulated) piece of equipment and prove the equipment's side of the link
works without installing the middleware at all.

It performs the same opening sequence the production middleware performs
(see eap_middleware.secs_runtime.SecsMachineSession) - optional S1F17
ON-LINE request, S2F33/35/37 subscription, S5F3 alarm enable - and then
logs every S6F11 report and S5F1 alarm instead of forwarding them to
MQTT/HTTPS. What arrives in the log is exactly what the middleware would
have had to work with.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import secsgem.hsms

from eap_middleware.profiles import ProfileRegistry
from gateway.host import GatewayHost

logger = logging.getLogger(__name__)

# How much of a report's V[] array to render on one log line. The full
# payload is still available at DEBUG through gateway.host.
_MAX_LOGGED_VALUES = 12


class HostSimulator:
    """Drive the host half of a link and report everything that arrives.

    Deliberately quacks like the equipment simulators (``enable``,
    ``disable``, ``start_events``, ``communication_state``, ``protocol``,
    ``events``, ``settings``) so SimulatorRunner can supervise either role
    with one retry/backoff loop.
    """

    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        tool_id: str = "HOST_SIM_01",
        profile_id: str = "davinci_200_mc4_hc1",
        subscription_path: Optional[str] = None,
        request_online: bool = True,
        enable_alarms: bool = True,
        drain_spool: bool = False,
        read_identity: bool = True,
    ) -> None:
        self.tool_id = tool_id
        self.profile_id = profile_id
        self._request_online = request_online
        self._enable_alarms = enable_alarms
        self._drain_spool = drain_spool
        self._read_identity = read_identity

        registry = ProfileRegistry()
        # An unknown id is a configuration bug, not a runtime condition:
        # config.py validates it, so anything reaching here is a caller
        # bypassing the loader and should see the KeyError.
        self._profile = registry.get(profile_id)
        # Fall back to whatever the profile documents, so a host started
        # with nothing but a profile id still subscribes to real events.
        self._subscription_path = (
            subscription_path or self._profile.event_subscription_path
        )

        # Observable counters - the GUI and the smoke tests read these to
        # tell "connected but silent" apart from "receiving".
        self.events_received = 0
        self.alarms_received = 0
        self.last_event_ceid: Optional[int] = None
        self.last_event_name: str = ""
        self.last_event_at: Optional[datetime] = None
        self.subscription_ok: Optional[bool] = None

        self._host = GatewayHost(
            settings=settings,
            tool_id=tool_id,
            on_event=self._on_event,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            dv_name_by_id={
                value: key for key, value in self._profile.dvs_by_name.items()
            },
        )
        self._host.set_alarm_callback(self._on_alarm)

    # -- equipment-simulator shaped surface -----------------------------

    @property
    def host(self) -> GatewayHost:
        return self._host

    @property
    def settings(self) -> Any:
        return self._host.settings

    @property
    def protocol(self) -> Any:
        return self._host.protocol

    @property
    def events(self) -> Any:
        return self._host.events

    @property
    def communication_state(self) -> Any:
        return self._host.communication_state

    def enable(self) -> None:
        self._host.enable()

    def disable(self) -> None:
        self._host.disable()

    def start_events(self) -> None:
        """No-op: a host receives events, it does not produce them.

        Present only so the runner can drive both roles identically.
        """

    # -- link callbacks --------------------------------------------------

    def _on_connect(self, _tool_id: str) -> None:
        logger.info(
            "[%s] HSMS selected; provisioning the equipment as a host would",
            self.tool_id,
        )
        # Fires on secsgem's communication state-machine thread, where each
        # of the calls below would block for up to T3 (45s). Provision on a
        # worker so the state machine keeps servicing the link, exactly as
        # the middleware does.
        threading.Thread(
            target=self._provision,
            name=f"HostSimProvision-{self.tool_id}",
            daemon=True,
        ).start()

    def _on_disconnect(self, _tool_id: str) -> None:
        logger.warning("[%s] equipment disconnected", self.tool_id)

    def _provision(self) -> None:
        try:
            self._run_opening_sequence()
        except Exception:
            # Never let a provisioning failure kill the worker silently -
            # the operator needs to see which step failed.
            logger.exception("[%s] provisioning failed", self.tool_id)

    def _run_opening_sequence(self) -> None:
        if self._request_online:
            if self._host.request_online():
                logger.info("[%s] equipment is ON-LINE", self.tool_id)
            else:
                logger.warning(
                    "[%s] S1F17 ON-LINE request refused; a tool left "
                    "OFF-LINE ignores subscriptions and sends no events",
                    self.tool_id,
                )

        if self._subscription_path:
            self.subscription_ok = self._host.subscribe_to_events(
                self._subscription_path,
                events_enabled_svid=(
                    self._profile.health_events_enabled_svid
                ),
            )
            refused = [
                band
                for band, accepted in
                self._host.subscription_band_results.items()
                if not accepted
            ]
            if refused:
                logger.warning(
                    "[%s] equipment refused subscription band(s): %s",
                    self.tool_id,
                    ", ".join(refused),
                )
        else:
            self.subscription_ok = None
            logger.warning(
                "[%s] profile %s documents no EventSubscription.json and "
                "none was configured; no events will be linked",
                self.tool_id,
                self.profile_id,
            )

        if self._drain_spool:
            self._host.drain_spool()

        if self._enable_alarms:
            if not self._host.enable_all_alarms():
                logger.warning(
                    "[%s] S5F3 alarm enable refused; S5F1 may never arrive",
                    self.tool_id,
                )

        if self._read_identity:
            self._log_identity()

    def _log_identity(self) -> None:
        """Read the profile's identity SVs, proving S1F3 works both ways."""
        svids = [
            self._profile.svids_by_name[name]
            for name in self._profile.identity_svid_names
            if name in self._profile.svids_by_name
        ]
        if not svids:
            return
        values = self._host.request_status(svids)
        if not values:
            logger.warning(
                "[%s] S1F3 identity read returned nothing", self.tool_id
            )
            return
        by_id = {
            value: key for key, value in self._profile.svids_by_name.items()
        }
        rendered = ", ".join(
            f"{by_id.get(svid, svid)}={value!r}"
            for svid, value in values.items()
        )
        logger.info("[%s] equipment identity: %s", self.tool_id, rendered)

    def _on_event(
        self, _tool_id: str, ceid: int, data: Dict[str, Any]
    ) -> None:
        self.events_received += 1
        self.last_event_ceid = ceid
        self.last_event_at = datetime.now()
        mapping = self._profile.resolve_event(ceid=ceid)
        self.last_event_name = getattr(mapping, "event_type", "") or ""
        logger.info(
            "[%s] event #%s CEID=%s (%s) %s",
            self.tool_id,
            self.events_received,
            ceid,
            self.last_event_name or "unmapped",
            self._render_values(data),
        )

    def _on_alarm(self, _tool_id: str, alarm: Dict[str, Any]) -> None:
        self.alarms_received += 1
        logger.info(
            "[%s] alarm %s ALID=%s %r",
            self.tool_id,
            "SET" if alarm.get("is_set") else "CLEARED",
            alarm.get("alid"),
            alarm.get("altx", ""),
        )

    def _render_values(self, data: Dict[str, Any]) -> str:
        values: List[Any] = list(data.get("_v_raw") or [])
        if not values:
            return "no report payload"
        shown = values[:_MAX_LOGGED_VALUES]
        hidden = len(values) - len(shown)
        suffix = f" (+{hidden} more)" if hidden else ""
        return f"RPTID={data.get('_rptid')} V={shown}{suffix}"
