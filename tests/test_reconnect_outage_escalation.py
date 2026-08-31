"""The reconnect watchdog has to explain an outage, not just repeat itself.

On the NexGen MG rig the watchdog logged the identical line

    Reconnect watchdog: TOOL_04 is disconnected, restarting session.

roughly every 30-60 s for forty minutes. Nothing in the log said how long the
tool had been down, how many attempts had been made, or - the part that
actually decides what to go and fix - whether the transport was failing or the
tool was answering TCP and refusing to establish communications. secsgem
reports connect failures at DEBUG only, so at INFO there was nothing at all.

The two states need opposite responses:

  * transport down  -> address/port/firewall, or something already holds the
                       tool's single HSMS peer slot;
  * WAIT_CRA        -> the tool is reachable and not answering S1F13 with
                       S1F14, so it is OFF-LINE or the device ID disagrees.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from secsgem.gem.communication_state_machine import CommunicationState

from eap_middleware.models import (
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.service import EapMiddlewareService


def _service(tmp_path) -> EapMiddlewareService:
    machine = MachineConfig(
        endpoint_id="TOOL_04",
        display_name="NEXGEN_MG_01",
        machine_profile="nexgen_mg_series",
        host="192.168.102.129",
        port=5051,
        hsms_mode="active",
    )
    cfg = ServiceConfig(
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
    return EapMiddlewareService(cfg)


def _host(state, tcp_connected):
    return SimpleNamespace(
        communication_state=SimpleNamespace(current=state),
        protocol=SimpleNamespace(
            _connection=SimpleNamespace(_connected=tcp_connected)
        ),
    )


def _escalate(service, host, failures, caplog, now=1_000.0):
    machine = service.config.machines[0]
    service._outage_since.setdefault(machine.endpoint_id, now - 600)
    with caplog.at_level(logging.ERROR, logger="eap_middleware.service"):
        service._escalate_outage(
            machine.endpoint_id, machine, host, failures, now
        )
    return "\n".join(record.getMessage() for record in caplog.records)


def test_no_escalation_before_the_threshold(tmp_path, caplog):
    """A single missed poll during a tool reboot is normal. Escalating on it
    would make the loud message meaningless."""
    service = _service(tmp_path)
    host = _host(CommunicationState.NOT_COMMUNICATING, False)
    assert _escalate(service, host, failures=1, caplog=caplog) == ""
    assert "TOOL_04" not in service._outage_escalated


def test_transport_failure_names_the_occupied_slot(tmp_path, caplog):
    """This is the rig's case: nothing on the wire from our host at all."""
    service = _service(tmp_path)
    host = _host(CommunicationState.NOT_COMMUNICATING, False)
    text = _escalate(service, host, failures=3, caplog=caplog)

    assert "TOOL_04 has not connected for 600s over 3 attempts" in text
    assert "192.168.102.129:5051" in text
    assert "hsms_mode=active" in text
    assert "gem_state=NOT_COMMUNICATING" in text
    assert "already holds the tool's HSMS connection" in text


def test_wait_cra_is_reported_as_a_refusal_not_a_network_fault(tmp_path, caplog):
    """TCP is up and Select succeeded but no S1F14 came back. Telling an
    operator to check the firewall here sends them to the wrong place."""
    service = _service(tmp_path)
    host = _host(CommunicationState.WAIT_CRA, True)
    text = _escalate(service, host, failures=5, caplog=caplog)

    assert "gem_state=WAIT_CRA" in text
    assert "tcp_connected=True" in text
    assert "never answered S1F13 with S1F14" in text
    assert "firewall" not in text


def test_escalation_happens_once_per_outage(tmp_path, caplog):
    """Otherwise it is the same repeated line, just louder."""
    service = _service(tmp_path)
    host = _host(CommunicationState.NOT_COMMUNICATING, False)
    assert _escalate(service, host, failures=3, caplog=caplog) != ""
    caplog.clear()
    assert _escalate(service, host, failures=9, caplog=caplog) == ""


def test_a_reconnect_clears_the_outage_and_says_how_long_it_lasted(tmp_path, caplog):
    service = _service(tmp_path)
    machine = service.config.machines[0]
    host = _host(CommunicationState.NOT_COMMUNICATING, False)
    _escalate(service, host, failures=3, caplog=caplog)
    assert machine.endpoint_id in service._outage_escalated

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="eap_middleware.service"):
        service._on_connect(machine)

    assert machine.endpoint_id not in service._outage_escalated
    assert machine.endpoint_id not in service._outage_since
    assert service._reconnect_failures[machine.endpoint_id] == 0
    assert any("reconnected after" in r.getMessage() for r in caplog.records)


def test_escalation_publishes_a_health_event(tmp_path, caplog):
    """The log is not enough on its own: an unattended rig needs the outage on
    the telemetry route where the dashboard can see it."""
    service = _service(tmp_path)
    published = []
    service._publish_health = lambda machine, state, details="": published.append(
        (machine.endpoint_id, state, details)
    )
    _escalate(service, _host(CommunicationState.NOT_COMMUNICATING, False), 3, caplog)

    assert len(published) == 1
    endpoint, state, details = published[0]
    assert (endpoint, state) == ("TOOL_04", "reconnect_failing")
    assert "has not connected for" in details
