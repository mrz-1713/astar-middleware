"""Real HSMS loopback in the REVERSE direction:

    middleware (PASSIVE, listening) <===HSMS===> simulator (ACTIVE, dialing in)

This proves the new hsms_mode='passive' code path works against a real
SECS/GEM peer, mirroring test_secs_simulator_loopback.py which exercises
the default active direction. Without this test, the passive code path is
configuration only - no production confidence.

Skipped automatically if secsgem isn't installed.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

pytest.importorskip("secsgem")
pytest.importorskip("paho.mqtt.client")

import secsgem.hsms
from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.linkstuffs import LINKSTUFFS_TOPIC_TELEMETRY
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.secs_runtime import SecsMachineSession

from tests.test_mqtt_loopback import _LoopbackPublisher, _wait_for_publish_count
from simulator.equipment import EquipmentSimulator


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


class _ActiveSimulator(EquipmentSimulator):
    """Simulator wired ACTIVE so it dials in to the middleware (which is
    listening PASSIVE). Fires CEID 851 (SPTS CassetteStarted) once
    communication is up."""

    CEID_OVERRIDE = 851

    def _event_loop(self) -> None:
        from secsgem.gem.communication_state_machine import CommunicationState
        while self._running:
            if self.communication_state.current == CommunicationState.COMMUNICATING:
                try:
                    self.send_event(self.CEID_OVERRIDE)
                except Exception:
                    pass
                time.sleep(self.event_interval)
            else:
                time.sleep(0.1)


def test_passive_middleware_accepts_inbound_from_active_simulator(tmp_path):
    """Middleware listens on a port; simulator dials in. End-to-end through
    mapper + fake MQTT proves the inbound direction works."""
    port = _free_port()
    display = "SPTS_fxP_OMEGA_PASSIVE_01"

    # --- middleware side: PASSIVE listener ---
    machine = MachineConfig(
        endpoint_id="TOOL_PASSIVE",
        display_name=display,
        machine_profile="spts_fxp_omega",
        host="127.0.0.1",  # documentation only in passive mode
        port=port,
        secs_device_id=0,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
        hsms_mode="passive",
        hsms_bind_address="127.0.0.1",
    )
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    csv_writer = PerLotCsvWriter()
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    publisher = _LoopbackPublisher(
        config=LinkstuffsConfig(
            enabled=True, host="127.0.0.1", port=1883,
            access_token="fake", client_id="passive-loopback",
        ),
        outbox=outbox,
    )

    received = []

    def on_event(_machine, ceid, data):
        received.append((ceid, data))
        ev = mapper.from_secs_event(_machine, ceid, data)
        csv_writer.append(_machine, profile, ev)
        publisher.queue_event(ev)

    session = SecsMachineSession(
        machine=machine,
        event_callback=on_event,
        alarm_callback=lambda *a, **k: None,
        connect_callback=lambda *a, **k: None,
        disconnect_callback=lambda *a, **k: None,
    )

    # --- simulator side: ACTIVE dial-in ---
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE,
        session_id=0,
    )
    simulator = _ActiveSimulator(
        settings=sim_settings, tool_id="SIM_ACTIVE_01", event_interval=0.5,
    )

    try:
        publisher.start()
        # Middleware listens FIRST so the simulator's connect succeeds.
        session.start()
        time.sleep(0.3)
        simulator.enable()
        simulator.start_events()

        # Wait up to 25s for the handshake + first S6F11 to land
        deadline = time.time() + 25.0
        while time.time() < deadline and not received:
            time.sleep(0.2)
        assert received, (
            "No S6F11 arrived inbound from the active simulator within 25s - "
            "passive listener may have failed to bind or accept the connection"
        )
        _wait_for_publish_count(publisher.fake_client, expected=1, timeout=10.0)

    finally:
        try:
            session.stop()
        except Exception:
            pass
        try:
            simulator.disable()
        except Exception:
            pass
        publisher.stop()

    ceid_seen = [c for c, _ in received if c > 0]
    assert _ActiveSimulator.CEID_OVERRIDE in ceid_seen, ceid_seen

    telemetry = [
        p for t, p in publisher.fake_client.publishes
        if t == LINKSTUFFS_TOPIC_TELEMETRY
    ]
    assert telemetry
    first = telemetry[0]
    assert display in first
    assert first[display][0]["values"]["event_type"] == "lot_start"
