"""Every reconnect must say the alarm picture is unknown - when it truly is.

The NexGen MG documents AlarmsSet as "Not Supported", so the currently-active
alarm set cannot be queried, and documents spooling as unsupported, so alarms
raised during an outage are never redelivered. The manual also warns that
irrecoverable errors and attention flags may never send a clearing message, so
there is no natural resynchronisation. The middleware therefore emits an
explicit marker on connect rather than letting a stale picture look current.

Profiles whose tools CAN report their active alarms must not emit it - the
signal is only meaningful where the gap is real.
"""

from __future__ import annotations

from typing import List

from eap_middleware.models import (
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.service import EapMiddlewareService


def _service(tmp_path, profile_id: str, display: str) -> EapMiddlewareService:
    machine = MachineConfig(
        endpoint_id="TOOL_04",
        display_name=display,
        machine_profile=profile_id,
        host="10.0.0.4",
        port=5000,
    )
    config = ServiceConfig(
        machines=[machine],
        linkstuffs=LinkstuffsConfig(enabled=False),
        linkstuffs_http=LinkstuffsHttpConfig(enabled=False),
        legacy_api=LegacyApiConfig(enabled=False),
        paths=MiddlewarePaths(
            install_dir=str(tmp_path / "install"),
            outbox_db=str(tmp_path / "o.sqlite3"),
            legacy_api_outbox_db=str(tmp_path / "l.sqlite3"),
            http_outbox_db=str(tmp_path / "h.sqlite3"),
        ),
    )
    return EapMiddlewareService(config)


def _published(service) -> List:
    captured = []
    service.publisher.queue_event = captured.append  # type: ignore[assignment]
    service.http_publisher.queue_event = captured.append  # type: ignore[assignment]
    return captured


def test_mg_connect_emits_an_alarm_state_unknown_marker(tmp_path):
    service = _service(tmp_path, "nexgen_mg_series", "NEXGEN_MG_01")
    machine = service.config.machines[0]
    captured = _published(service)

    service._on_connect(machine)

    alarms = [e for e in captured if e.event_type == "alarm"]
    assert len(alarms) == 2, "one per upstream (MQTT + HTTP)"
    marker = alarms[0]
    assert marker.raw_event_name == "AlarmCleared"
    assert marker.raw_payload["_alarm_state_unknown"] is True
    assert "unknown" in marker.secs_raw_event.lower()
    assert "lost" in marker.secs_raw_event.lower()


def test_every_reconnect_re_emits_it(tmp_path):
    service = _service(tmp_path, "nexgen_mg_series", "NEXGEN_MG_01")
    machine = service.config.machines[0]
    captured = _published(service)

    service._on_connect(machine)
    service._on_connect(machine)

    markers = [
        e for e in captured
        if e.raw_payload.get("_alarm_state_unknown")
    ]
    assert len(markers) == 4, "two connects x two upstreams"


def test_tools_that_can_report_their_alarms_stay_quiet(tmp_path):
    """DaVinci exposes AlarmsSet, so its alarm picture is recoverable."""
    service = _service(tmp_path, "davinci_200_mc4_hc1", "DAVINCI200_MC4_HC1_01")
    machine = service.config.machines[0]
    captured = _published(service)

    service._on_connect(machine)

    assert not [e for e in captured if e.event_type == "alarm"]


def test_the_gate_is_the_absence_of_an_alarms_set_variable():
    """Documented as "Not Supported" in the MG manual, so it is simply absent
    from the profile - that absence is the signal, not a separate flag."""
    registry = ProfileRegistry()
    assert registry.get("nexgen_mg_series").resolve_svid_name("AlarmsSet") is None
    for profile_id in ("spts_fxp_omega", "davinci_200_mc4_hc1", "ptiq_secsgem"):
        assert registry.get(profile_id).resolve_svid_name("AlarmsSet") is not None


def test_alarm_cleared_is_recognised_on_every_vendor(tmp_path):
    """`is_set` must come from the profile, not from the DaVinci's own CEID.

    It used to be `ceid != 3020002` - the DaVinci's AlarmNCleared. On any other
    tool that comparison is always true, so every alarm the machine cleared was
    published upstream as still set and the alarm never went away.
    """
    for profile_id, display, set_ceid, clear_ceid in (
        ("davinci_200_mc4_hc1", "DAVINCI200_MC4_HC1_01", 3020001, 3020002),
        ("nexgen_mg_series", "NEXGEN_MG_01", 8, 9),
    ):
        service = _service(tmp_path, profile_id, display)
        machine = service.config.machines[0]
        seen: List[dict] = []
        service._on_alarm = lambda _m, alarm: seen.append(alarm)  # type: ignore

        service._on_secs_event(machine, set_ceid, {"AlarmID": 42, "ALTX": "hot"})
        service._on_secs_event(machine, clear_ceid, {"AlarmID": 42, "ALTX": "hot"})

        assert len(seen) == 2, f"{profile_id} did not route both alarms"
        assert seen[0]["is_set"] is True, f"{profile_id} set was not set"
        assert seen[1]["is_set"] is False, (
            f"{profile_id} reported a cleared alarm as still set"
        )
