from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulator.cli import main
from simulator.config import SimulatorConfigError, load_simulator_config


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "davinci.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _valid_config(
    tmp_path: Path, mode: str = "active", address: str = "127.0.0.1"
) -> Path:
    return _write_config(
        tmp_path,
        f"""
connection:
  mode: {mode}
  address: {address}
  allow_external_bind: {str(address != "127.0.0.1").lower()}
  port: 5050
  device_id: 0
simulation:
  tool_id: DAV_TEST
  wafer_count: 2
  event_interval_sec: 0.05
  repeat_lots: false
  emit_alarm: true
recovery:
  initial_retry_sec: 1
  maximum_retry_sec: 5
  maximum_restart_attempts: 3
logging:
  level: DEBUG
  directory: logs
  maximum_size_mb: 2
  backup_count: 2
""",
    )


def test_load_active_configuration_and_resolve_relative_log_path(tmp_path):
    path = _valid_config(tmp_path)
    config = load_simulator_config(path)

    assert config.connection.mode == "active"
    assert config.connection.address == "127.0.0.1"
    assert config.connection.port == 5050
    assert config.simulation.tool_id == "DAV_TEST"
    assert config.simulation.wafer_count == 2
    assert config.log_directory == tmp_path / "logs"


def test_load_passive_configuration_accepts_explicit_wildcard_bind(
    tmp_path, caplog
):
    config = load_simulator_config(
        _valid_config(tmp_path, "passive", "0.0.0.0")
    )
    assert config.connection.mode == "passive"
    assert config.summary()["listener"] == "0.0.0.0:5050"
    assert "LAB EXTERNAL BIND" in caplog.text


def test_passive_external_bind_requires_explicit_opt_in(tmp_path):
    path = _valid_config(tmp_path, "passive", "0.0.0.0")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  allow_external_bind: true\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(SimulatorConfigError, match="allow_external_bind"):
        load_simulator_config(path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("mode: sideways", "connection.mode"),
        ("address: 0.0.0.0", "cannot be 0.0.0.0"),
        ("port: 70000", "connection.port"),
        ("device_id: -1", "connection.device_id"),
        ("event_interval_sec: 0", "event_interval_sec"),
        ("maximum_restart_attempts: -1", "maximum_restart_attempts"),
        ("level: VERBOSE", "logging.level"),
    ],
)
def test_invalid_values_are_rejected(tmp_path, replacement, message):
    path = _valid_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    key = replacement.split(":", 1)[0]
    lines = [
        (
            f"{line[: len(line) - len(line.lstrip())]}{replacement}"
            if line.strip().startswith(f"{key}:")
            else line
        )
        for line in text.splitlines()
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(SimulatorConfigError, match=message):
        load_simulator_config(path)


def test_unknown_keys_are_rejected(tmp_path):
    path = _valid_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8") + "unexpected_section: true\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SimulatorConfigError, match="unknown key.*unexpected_section"
    ):
        load_simulator_config(path)


def test_boolean_strings_are_not_silently_accepted(tmp_path):
    path = _valid_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "repeat_lots: false", 'repeat_lots: "false"'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(
        SimulatorConfigError, match="repeat_lots must be true or false"
    ):
        load_simulator_config(path)


def test_check_config_command_outputs_machine_readable_summary(
    tmp_path, capsys
):
    path = _valid_config(tmp_path)
    assert main(["check-config", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["gem_role"] == "equipment"
    assert output["hsms_mode"] == "active"
    assert output["remote"] == "127.0.0.1:5050"


def test_check_config_command_returns_two_for_bad_file(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"
    assert main(["check-config", "--config", str(missing)]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "SecsGemSimulator 1.0.0"


@pytest.mark.parametrize(
    ("filename", "mode"),
    [("davinci-active.yaml", "active"), ("davinci-passive.yaml", "passive")],
)
def test_distributed_configuration_templates_are_valid(filename, mode):
    root = Path(__file__).resolve().parent.parent
    path = root / "packaging" / "secsgem_simulator" / filename
    assert load_simulator_config(path).connection.mode == mode


# ----- HSMS protocol timers -----
#
# The simulator stands in for the tool, and the tool is the side the
# middleware's timers have to match. Whichever side has the shorter value
# declares a communications failure first, and the link then drops with
# nothing in either log to explain it - so a rig pinned to the library
# defaults could never reproduce the one fault it most needs to.

def _config_with_timers(timers) -> dict:
    return {
        "connection": {
            "role": "equipment",
            "mode": "passive",
            "address": "0.0.0.0",
            "allow_external_bind": True,
            "port": 5051,
            "device_id": 0,
            "hsms_timers": timers,
        },
        "simulation": {"profile": "spts_fxp_omega", "tool_id": "SIM_01"},
    }


def test_hsms_timers_are_parsed_when_present():
    from simulator.config import simulator_config_from_dict

    spts = {"t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6}
    config = simulator_config_from_dict(_config_with_timers(spts))
    assert config.connection.hsms_timers == spts


def test_hsms_timers_default_to_empty_meaning_shipped_defaults():
    from simulator.config import simulator_config_from_dict

    raw = _config_with_timers({})
    del raw["connection"]["hsms_timers"]
    assert simulator_config_from_dict(raw).connection.hsms_timers == {}


@pytest.mark.parametrize(
    ("timers", "message"),
    [
        ({"t4": 10}, "not an HSMS timer"),
        ({"t3": 0}, "between 1 and 120"),
        ({"t3": 121}, "between 1 and 120"),
        ({"t3": "30"}, "whole number of seconds"),
        ({"t3": True}, "whole number of seconds"),
    ],
)
def test_hsms_timers_are_validated(timers, message):
    from simulator.config import simulator_config_from_dict

    with pytest.raises(SimulatorConfigError, match=message):
        simulator_config_from_dict(_config_with_timers(timers))


def test_hsms_timers_must_be_a_mapping():
    from simulator.config import simulator_config_from_dict

    with pytest.raises(SimulatorConfigError, match="must be a mapping"):
        simulator_config_from_dict(_config_with_timers([30, 5, 10, 5, 6]))
