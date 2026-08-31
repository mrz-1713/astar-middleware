"""Regression tests for the secsgem-0.3.x real-hardware bugs found in the
deep audit. Each of these passed the rest of the suite while being broken on a
real DaVinci, because the simulator emits S6F11/S5F1 regardless of whether the
host successfully subscribed, and the other e2e test never sets a
subscription_path.

Bugs pinned here (all caused by reading the raw secsgem ``Message.data`` bytes
instead of decoding the reply with the stream-function codec):

  A. Event subscription (S2F33/35/37) ack was read from the raw header byte, so
     DRACK/LRACK/ERACK always looked non-zero -> subscribe_to_events() returned
     False -> a real tool was never told to enable any collection event.
  B. request_status() (S1F3 -> S1F4) zipped SVIDs against raw payload bytes,
     publishing garbage SVID telemetry.
  C. Connection loss was never detected: secsgem 0.3.x reports disconnects on
     the protocol event bus, not via handler.on_connection_closed, so
     is_connected stayed True forever and the reconnect watchdog never fired.

Skipped automatically if secsgem isn't installed.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

pytest.importorskip("secsgem")

import secsgem.hsms

from gateway.host import GatewayHost, create_host_settings
from simulator.secsgem_equipment import SecsGemEquipment


def _free_port() -> int:
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
            continue
    raise RuntimeError("Could not find a free, immediately-rebindable port")


def _make_pair():
    """Return (simulator, host, connected_flag, disconnected_flag) wired over a
    fresh loopback port. Caller must enable both and tear them down."""
    port = _free_port()
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    simulator = SecsGemEquipment(
        settings=sim_settings,
        tool_id="DAV_SIM_REG",
        wafer_count=1,
        step_interval_sec=0.05,
        fire_alarm=False,
        loop_lots=False,
    )
    connected: list[int] = []
    disconnected: list[int] = []
    host_settings = create_host_settings(
        host="127.0.0.1", port=port, device_id=0, mode="active"
    )
    # Fail fast if a reply never comes instead of waiting the 45s default.
    host_settings.timeouts.t3 = 5
    host_settings.timeouts.t6 = 3
    host = GatewayHost(
        settings=host_settings,
        tool_id="DAV_REG",
        on_connect=lambda _tool: connected.append(1),
        on_disconnect=lambda _tool: disconnected.append(1),
    )
    return simulator, host, connected, disconnected


def _wait(pred, timeout=15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_subscription_succeeds_against_real_secs_stack():
    """Bug A: subscribe_to_events must return True; the equipment accepts the
    full generated subscription (DRACK/LRACK/ERACK = 0)."""
    simulator, host, connected, _ = _make_pair()
    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected)), "host never reached COMMUNICATING"

        ok = host.subscribe_to_events(
            "output/davinci200_mc4_hc1/EventSubscription.json"
        )
        assert ok is True, (
            "subscribe_to_events returned False - S2F33/35/37 ack was misread "
            "(regression of the raw-bytes _extract_ack bug)"
        )
    finally:
        host.disable()
        simulator.disable()


def test_davinci_s1f2_identity_matches_manual_over_real_secs_stack():
    simulator, host, connected, _ = _make_pair()
    try:
        simulator.enable()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected))
        response = host.are_you_there()
        assert response is not None
        assert host.settings.streams_functions.decode(response).get() == [
            "DaVinci200",
            "DaVinci200 Version 4.9.3",
        ]
    finally:
        host.disable()
        simulator.disable()


def test_request_status_returns_decoded_values():
    """Bug B: request_status must return the decoded S1F4 values, not raw bytes."""
    simulator, host, connected, _ = _make_pair()
    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected)), "host never reached COMMUNICATING"

        # ControlState=1010001 (sim returns 5), ProcessState=1050001 (sim 2),
        # PM1/RecipeName=1060007 (sim returns a recipe string).
        values = host.request_status([1010001, 1050001, 1060007])
        assert values.get(1010001) == 5, f"ControlState wrong: {values!r}"
        assert values.get(1050001) == 2, f"ProcessState wrong: {values!r}"
        recipe = values.get(1060007)
        assert isinstance(recipe, str) and recipe, (
            f"PM1/RecipeName should decode to a non-empty string, got {recipe!r} "
            "(regression of the raw-bytes request_status bug)"
        )
    finally:
        host.disable()
        simulator.disable()


def test_telemetry_transform_is_clean_and_correct():
    """End-to-end transform check: a real S6F11 from the tool is parsed by the
    host, named by the mapper from the per-CEID DV layout, and serialized for
    ThingsBoard. The payload must carry the correct lot/wafer/recipe and must
    NOT carry the legacy positional mislabels (LOT_ID/LOAD_PORT/CHAMBER/...)
    that would otherwise leak in as wrong raw_* fields."""
    from eap_middleware.job_tracker import JobTracker
    from eap_middleware.mapper import CanonicalMapper
    from eap_middleware.models import MachineConfig
    from eap_middleware.profiles import ProfileRegistry

    port = _free_port()
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1", port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE, session_id=0,
    )
    simulator = SecsGemEquipment(
        settings=sim_settings, tool_id="DAV_SIM_TX",
        wafer_count=1, step_interval_sec=0.05, fire_alarm=False, loop_lots=False,
    )

    events: list = []
    connected: list = []
    host_settings = create_host_settings(
        host="127.0.0.1", port=port, device_id=0, mode="active"
    )
    host_settings.timeouts.t3 = 5
    host_settings.timeouts.t6 = 3
    host = GatewayHost(
        settings=host_settings, tool_id="DAV_TX",
        on_event=lambda _t, ceid, data: events.append((ceid, data)),
        on_connect=lambda _t: connected.append(1),
    )

    machine = MachineConfig(
        endpoint_id="TOOL_TX", display_name="DAVINCI200_MC4_HC1_01",
        machine_profile="davinci_200_mc4_hc1", host="127.0.0.1", port=port,
    )
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=JobTracker())

    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected)), "host never reached COMMUNICATING"
        assert host.subscribe_to_events(
            "output/davinci200_mc4_hc1/EventSubscription.json"
        ) is True
        # Let the scripted lot run so we capture ProcessingFinished (3140003).
        assert _wait(lambda: any(c == 3140003 for c, _ in events), timeout=15.0), (
            "never received PM1/ProcessingFinished (3140003)"
        )
    finally:
        host.disable()
        simulator.disable()

    raw = next(data for ceid, data in events if ceid == 3140003)
    ev = mapper.from_secs_event(machine, 3140003, raw)
    values = ev.telemetry_values()

    # Correctly transformed lifecycle fields
    assert ev.event_type == "process_end"
    assert values["lot_id"].startswith("LOT_SIM_"), values["lot_id"]
    assert values["wafer_id"], "wafer_id missing"
    assert values["recipe"] == "Recipe_Overlay_v3", values["recipe"]
    assert values["chamber"] == "NA"  # honest default, not a mislabeled V[] value

    # The measurement payload survives, correctly named from the DV layout.
    assert "raw_ResultFile" in values
    assert "raw_TestResults" in values

    # No legacy positional mislabels leaked into the ThingsBoard payload.
    for bad in ("raw_LOT_ID", "raw_LOAD_PORT", "raw_CHAMBER", "raw_TOOL_EVENT",
                "raw_EAP_TOOLNAME", "raw_WAFER_ID", "raw_RECIPE",
                "raw_DATETIME", "raw_LOT_START_TIME", "raw_WAFER_QTY"):
        assert bad not in values, f"misleading positional field leaked: {bad}={values.get(bad)!r}"

    # And the raw V[] marker never ships.
    assert "raw__v_raw" not in values and "_v_raw" not in values


def test_enable_all_alarms_is_acknowledged():
    """R2: the host enables alarm reporting via S5F3 and the tool ACKs (ACKC5=0)
    so S5F1 alarm reports are guaranteed regardless of the tool's default."""
    simulator, host, connected, _ = _make_pair()
    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected)), "host never reached COMMUNICATING"

        assert host.enable_all_alarms() is True, (
            "enable_all_alarms() should send S5F3 and get ACKC5=0"
        )
    finally:
        host.disable()
        simulator.disable()


def test_disconnect_is_detected_and_flips_is_connected():
    """Bug C: when the tool drops, is_connected must flip to False and the
    disconnect callback must fire so the reconnect watchdog can act."""
    simulator, host, connected, disconnected = _make_pair()
    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        assert _wait(lambda: bool(connected)), "host never reached COMMUNICATING"
        assert host.is_connected is True

        # Simulate a tool drop (reboot / network loss).
        simulator.disable()

        assert _wait(lambda: host.is_connected is False, timeout=15.0), (
            "is_connected stayed True after peer drop - reconnect watchdog "
            "would never fire (regression of the dead on_connection_closed bug)"
        )
        assert _wait(lambda: bool(disconnected), timeout=5.0), (
            "on_disconnect callback never fired after peer drop"
        )
    finally:
        host.disable()
        try:
            simulator.disable()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2026-06-30 audit: data-completeness + deploy hardening regressions
# ---------------------------------------------------------------------------

def test_every_subscribed_davinci_ceid_has_a_dv_layout():
    """Every collection event in the deployed DaVinci subscription whose report
    carries DVIDs MUST have a DAVINCI_CEID_DV_LAYOUT entry, otherwise the host
    decodes V[] into _v_raw and the mapper never names it -> the payload
    (AlarmID/AlarmText, ProcessingResult WaferID/LotID, CarrierID,
    CtrlJobID/PRJobID, ...) is silently dropped before the ThingsBoard POST.

    This pins the 14-event gap found in the 2026-06-30 audit so a future
    subscription edit can't re-introduce a payload-less event unnoticed.
    """
    import json
    from pathlib import Path

    from eap_middleware.profiles import DAVINCI_CEID_DV_LAYOUT

    sub_path = (
        Path(__file__).resolve().parent.parent
        / "output" / "davinci200_mc4_hc1" / "EventSubscription.json"
    )
    sub = json.loads(sub_path.read_text())
    reports = {r["rptid"]: r["dvids"] for r in sub["reports"]}

    missing = []
    for event in sub["events"]:
        ceid = event["ceid"]
        dvids = []
        for rid in event["rptids"]:
            dvids += reports.get(rid, [])
        if dvids and ceid not in DAVINCI_CEID_DV_LAYOUT:
            missing.append(ceid)

    assert not missing, (
        f"Subscribed CEIDs with DVID payloads but no DAVINCI_CEID_DV_LAYOUT "
        f"entry (payload would be dropped): {sorted(missing)}"
    )


def test_subscribed_layout_length_covers_report_dvids():
    """For every subscribed event that has a layout, the layout must name at
    least as many positions as the report sends DVIDs - a short layout would
    truncate the trailing values."""
    import json
    from pathlib import Path

    from eap_middleware.profiles import DAVINCI_CEID_DV_LAYOUT

    sub_path = (
        Path(__file__).resolve().parent.parent
        / "output" / "davinci200_mc4_hc1" / "EventSubscription.json"
    )
    sub = json.loads(sub_path.read_text())
    reports = {r["rptid"]: r["dvids"] for r in sub["reports"]}

    short = []
    for event in sub["events"]:
        ceid = event["ceid"]
        layout = DAVINCI_CEID_DV_LAYOUT.get(ceid)
        if not layout:
            continue
        dvids = []
        for rid in event["rptids"]:
            dvids += reports.get(rid, [])
        if len(layout) < len(dvids):
            short.append((ceid, len(layout), len(dvids)))
    assert not short, f"Layouts shorter than report DVID count: {short}"


def test_hsms_timers_match_manual():
    """create_host_settings pins T3/T5/T6/T7/T8 to the DaVinci manual values."""
    s = create_host_settings(host="10.0.0.1", port=5000, device_id=0, mode="active")
    assert (s.timeouts.t3, s.timeouts.t5, s.timeouts.t6, s.timeouts.t7, s.timeouts.t8) == (45, 10, 5, 10, 5)


def test_http_url_redaction_hides_token():
    """The device token embedded in the telemetry URL must be masked in logs."""
    from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher

    url = "https://astar-monitoring.linkstuffs.com/api/v1/SECRETTOKEN123/telemetry"
    redacted = LinkstuffsHttpPublisher._redact(url)
    assert "SECRETTOKEN123" not in redacted
    assert redacted == "https://astar-monitoring.linkstuffs.com/api/v1/***/telemetry"
