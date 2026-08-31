"""Per-machine HSMS active/passive mode.

The middleware previously assumed every tool is HSMS-PASSIVE and the
middleware dials out (ACTIVE). Real fabs are mixed: some tools are configured
HSMS-ACTIVE and EXPECT the middleware to listen for them inbound. This test
file pins:

  - Config parsing accepts and validates hsms_mode
  - Per-machine selection produces the right HsmsSettings
  - Two passive machines on the same port are rejected at validate-config
  - A mixed deployment (active + passive) loads cleanly
"""

from __future__ import annotations

import pytest

from eap_middleware.config import ConfigError, service_config_from_dict


def _base_yaml(machines):
    return {
        "linkstuffs": {
            "enabled": True, "host": "127.0.0.1", "port": 1883,
            "access_token": "x", "client_id": "test",
        },
        # These tests exercise socket-role validation, not production routing.
        "machines": [
            dict(machine, offline_test_mode=True) for machine in machines
        ],
    }


def test_default_hsms_mode_is_active():
    cfg = service_config_from_dict(_base_yaml([{
        "endpoint_id": "TOOL_01",
        "display_name": "SPTS_fxP_OMEGA_01",
        "machine_profile": "spts_fxp_omega",
        "host": "192.0.2.31",
    }]))
    machine = cfg.machines[0]
    assert machine.hsms_mode == "active"
    assert machine.is_passive is False
    assert machine.hsms_bind_address == "0.0.0.0"


def test_explicit_passive_mode_round_trips():
    cfg = service_config_from_dict(_base_yaml([{
        "endpoint_id": "TOOL_X",
        "display_name": "DAVINCI200_MC4_HC1_X",
        "machine_profile": "davinci_200_mc4_hc1",
        "host": "10.10.20.99",
        "port": 5001,
        "hsms_mode": "passive",
        "hsms_bind_address": "192.168.1.10",
    }]))
    machine = cfg.machines[0]
    assert machine.hsms_mode == "passive"
    assert machine.is_passive is True
    assert machine.hsms_bind_address == "192.168.1.10"


def test_invalid_hsms_mode_rejected():
    with pytest.raises(ConfigError, match="hsms_mode"):
        service_config_from_dict(_base_yaml([{
            "endpoint_id": "TOOL_X",
            "display_name": "X",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.99",
            "hsms_mode": "client",  # nonsense value
        }]))


def test_two_passive_machines_same_port_rejected():
    """Two passive machines cannot both bind 0.0.0.0:5000."""
    with pytest.raises(ConfigError, match="passive machines cannot share"):
        service_config_from_dict(_base_yaml([
            {
                "endpoint_id": "TOOL_A",
                "display_name": "DAV_A",
                "machine_profile": "davinci_200_mc4_hc1",
                "host": "10.10.20.32",
                "port": 5000,
                "hsms_mode": "passive",
            },
            {
                "endpoint_id": "TOOL_B",
                "display_name": "DAV_B",
                "machine_profile": "davinci_200_mc4_hc1",
                "host": "192.0.2.33",
                "port": 5000,    # COLLIDES with TOOL_A
                "hsms_mode": "passive",
            },
        ]))


@pytest.mark.parametrize("wildcard_first", [True, False])
def test_passive_wildcard_conflicts_with_specific_address(wildcard_first):
    binds = ["0.0.0.0", "192.0.2.10"]
    if not wildcard_first:
        binds.reverse()
    machines = [
        {
            "endpoint_id": f"TOOL_{index}",
            "display_name": f"DAV_{index}",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": f"192.0.2.{20 + index}",
            "port": 5000,
            "hsms_mode": "passive",
            "hsms_bind_address": bind_address,
        }
        for index, bind_address in enumerate(binds, start=1)
    ]

    with pytest.raises(ConfigError, match="0.0.0.0 wildcard overlaps"):
        service_config_from_dict(_base_yaml(machines))


def test_two_passive_machines_different_ports_ok():
    cfg = service_config_from_dict(_base_yaml([
        {
            "endpoint_id": "TOOL_A",
            "display_name": "DAV_A",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.32",
            "port": 5000,
            "hsms_mode": "passive",
        },
        {
            "endpoint_id": "TOOL_B",
            "display_name": "DAV_B",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "192.0.2.33",
            "port": 5001,
            "hsms_mode": "passive",
        },
    ]))
    assert len(cfg.machines) == 2


def test_two_passive_machines_distinct_bind_addresses_ok():
    """Multi-NIC server: same port is OK if each binds a different NIC."""
    cfg = service_config_from_dict(_base_yaml([
        {
            "endpoint_id": "TOOL_A",
            "display_name": "DAV_A",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.32",
            "port": 5000,
            "hsms_mode": "passive",
            "hsms_bind_address": "192.168.1.10",
        },
        {
            "endpoint_id": "TOOL_B",
            "display_name": "DAV_B",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "192.0.2.33",
            "port": 5000,
            "hsms_mode": "passive",
            "hsms_bind_address": "192.168.2.10",
        },
    ]))
    assert len(cfg.machines) == 2


def test_two_active_machines_same_port_ok():
    """ACTIVE-mode machines dial out, no bind, so port collision doesn't apply."""
    cfg = service_config_from_dict(_base_yaml([
        {
            "endpoint_id": "TOOL_A", "display_name": "A",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.32", "port": 5000, "hsms_mode": "active",
        },
        {
            "endpoint_id": "TOOL_B", "display_name": "B",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "192.0.2.33", "port": 5000, "hsms_mode": "active",
        },
    ]))
    assert len(cfg.machines) == 2


def test_disabled_passive_machine_doesnt_block_a_live_one_on_same_port():
    """A disabled passive entry shouldn't reserve its bind slot."""
    cfg = service_config_from_dict(_base_yaml([
        {
            "endpoint_id": "TOOL_A", "display_name": "A",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.32", "port": 5000, "hsms_mode": "passive",
            "enabled": False,
        },
        {
            "endpoint_id": "TOOL_B", "display_name": "B",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "192.0.2.33", "port": 5000, "hsms_mode": "passive",
            "enabled": True,
        },
    ]))
    assert len(cfg.machines) == 2


def test_mixed_active_and_passive_loads_cleanly():
    """The fab the customer described: DaVinci-01 active, DaVinci-02 passive."""
    cfg = service_config_from_dict(_base_yaml([
        {
            "endpoint_id": "TOOL_01", "display_name": "DAVINCI_01",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.10.20.32", "port": 5000, "hsms_mode": "active",
        },
        {
            "endpoint_id": "TOOL_02", "display_name": "DAVINCI_02",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "192.0.2.33", "port": 5001, "hsms_mode": "passive",
        },
        {
            "endpoint_id": "TOOL_03", "display_name": "SPTS_01",
            "machine_profile": "spts_fxp_omega",
            "host": "10.10.20.34", "port": 5000, "hsms_mode": "active",
        },
    ]))
    modes = {m.endpoint_id: m.hsms_mode for m in cfg.machines}
    assert modes == {"TOOL_01": "active", "TOOL_02": "passive", "TOOL_03": "active"}


# ----- HsmsSettings construction -----

def test_create_host_settings_active_mode():
    """Verify active mode produces ACTIVE-direction HsmsSettings."""
    pytest.importorskip("secsgem")
    import secsgem.hsms
    from gateway.host import create_host_settings

    settings = create_host_settings(
        host="10.10.20.32", port=5000, device_id=0, mode="active",
    )
    assert settings.connect_mode == secsgem.hsms.HsmsConnectMode.ACTIVE
    assert settings.address == "10.10.20.32"
    assert settings.port == 5000


def test_create_host_settings_passive_mode_uses_bind_address():
    """In passive mode the settings address should be the bind address, not
    the documentation-only host field."""
    pytest.importorskip("secsgem")
    import secsgem.hsms
    from gateway.host import create_host_settings

    settings = create_host_settings(
        host="10.10.20.32",  # informational
        port=5000,
        device_id=0,
        mode="passive",
        bind_address="0.0.0.0",
    )
    assert settings.connect_mode == secsgem.hsms.HsmsConnectMode.PASSIVE
    assert settings.address == "0.0.0.0"
    assert settings.port == 5000


# ----- HSMS-SS protocol timers follow the tool, not the host -----

def test_profile_timers_come_from_each_vendor_manual():
    """The host's timers must match the tool's.

    Both manuals that state protocol timers give different values, and the
    side with the shorter timer declares a communications failure while the
    other still considers the transaction open - an intermittent link drop
    with nothing in either log to point at.
    """
    from eap_middleware.profiles import ProfileRegistry

    registry = ProfileRegistry()
    # Omega manual section 4.4, Table 3 "Protocol Parameters".
    assert registry.get("spts_fxp_omega").hsms_timers == {
        "t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6
    }
    # DaVinci Host Interface Manual section 4.3.1.2.
    assert registry.get("davinci_200_mc4_hc1").hsms_timers == {
        "t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5
    }
    # Neither the NexGen MG manual nor the PTIQ spec states a protocol timer.
    # They therefore carry the shipped default WRITTEN OUT rather than left
    # empty: empty also resolves to these numbers, but silently, and "this
    # tool is running the DaVinci's timers" is a fact an operator should be
    # able to read off the profile instead of having to infer it from an
    # absent field. Correcting them from the tool's own SECS/GEM screen stays
    # a configuration change.
    shipped_default = {"t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5}
    assert registry.get("nexgen_mg_series").hsms_timers == shipped_default
    assert registry.get("ptiq_secsgem").hsms_timers == shipped_default
    assert (
        registry.get("davinci_200_mc4_hc1").hsms_timers == shipped_default
    ), "the shipped default is the DaVinci's own documented set"


def test_settings_apply_profile_timers_and_keep_the_default_otherwise():
    from gateway.host import create_host_settings

    default = create_host_settings(host="10.0.0.1", port=5000, mode="active")
    assert (
        default.timeouts.t3, default.timeouts.t5, default.timeouts.t6,
        default.timeouts.t7, default.timeouts.t8,
    ) == (45, 10, 5, 10, 5)

    omega = create_host_settings(
        host="10.0.0.1", port=5000, mode="active",
        timers={"t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6},
    )
    assert (
        omega.timeouts.t3, omega.timeouts.t5, omega.timeouts.t6,
        omega.timeouts.t7, omega.timeouts.t8,
    ) == (30, 5, 10, 5, 6)

    # A partial override leaves the rest at the default.
    partial = create_host_settings(host="10.0.0.1", port=5000, timers={"t3": 20})
    assert (partial.timeouts.t3, partial.timeouts.t5) == (20, 10)


def test_out_of_range_and_unknown_timers_are_refused():
    """SEMI E37 allows 1..120s. Anything else is a config error, not tuning."""
    import pytest as _pytest
    from gateway.host import create_host_settings

    for bad in ({"t9": 5}, {"t3": 0}, {"t3": 121}):
        with _pytest.raises(ValueError):
            create_host_settings(host="10.0.0.1", port=5000, timers=bad)


def test_a_machine_may_override_its_profile_timers():
    """A tool retuned on site has to be followed, so the override wins."""
    from pathlib import Path as _Path

    import yaml as _yaml
    from eap_middleware.config import service_config_from_dict

    root = _Path(__file__).resolve().parents[1]
    raw = _yaml.safe_load(
        (root / "config" / "production.yaml").read_text(encoding="utf-8")
    )
    spts = next(
        m for m in raw["machines"] if m["machine_profile"] == "spts_fxp_omega"
    )
    spts["hsms_timers"] = {"t3": 20}
    machine = next(
        m for m in service_config_from_dict(raw).machines
        if m.machine_profile == "spts_fxp_omega"
    )
    # Overridden value replaces, the rest still come from the manual.
    assert machine.hsms_timers["t3"] == 20
    assert machine.hsms_timers["t6"] == 10
