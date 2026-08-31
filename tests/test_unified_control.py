from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import json
import threading
import time

import pytest
import yaml

from eap_middleware.config import (
    ConfigError,
    load_service_config,
    service_config_from_dict,
)
from eap_middleware.control import (
    StaleConfigError,
    consume_commands,
    file_revision,
    load_status,
    save_config_atomic,
    submit_command,
)
from eap_middleware.service import EapMiddlewareService
from eap_middleware.service import reconnect_delay
from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.models import CanonicalEvent, MachineConfig, MachineStorageConfig
from eap_middleware.profiles import ProfileRegistry


def _machine(endpoint_id: str = "TOOL_01", port: int = 5000):
    return {
        "endpoint_id": endpoint_id,
        "display_name": endpoint_id,
        "machine_profile": "nexgen_mg_series",
        "host": "127.0.0.1",
        "port": port,
        "enabled": True,
        "offline_test_mode": True,
    }


def test_machine_scoped_sections_override_legacy_defaults(tmp_path):
    raw = {
        "linkstuffs_http": {
            "enabled": True,
            "base_url": "https://global.example",
            "device_tokens": {"TOOL_01": "global-token"},
        },
        "machines": [
            {
                **_machine(),
                "runtime_mode": "simulated",
                "local_csv_path": "legacy-csv",
                "storage": {
                    "log_dir": str(tmp_path / "logs"),
                    "simulator_log_dir": str(tmp_path / "sim-logs"),
                    "local_csv_path": str(tmp_path / "csv"),
                    "network_csv_path": str(tmp_path / "mirror"),
                    "admin_config_path": str(tmp_path / "admin"),
                },
                "linkstuffs_http": {
                    "base_url": "https://machine.example",
                    "device_token": "machine-token",
                    "retry_count": 7,
                },
                "simulator": {
                    "implementation": "profile",
                    "mdln": "MG22-300",
                    "softrev": "1.1.18",
                    "alarm_id": 42,
                    "alarm_text": "simulated alarm",
                    "wafer_count": 5,
                    "event_interval_sec": 0.25,
                    "repeat_lots": False,
                    "emit_alarm": False,
                    "ceid_overrides": {"lot_start": 7001},
                    "svid_values": {"100": "READY"},
                    "event_definitions": {
                        "dvid_names": {"10": "LotID"},
                        "dvid_types": {"10": "A"},
                        "dvid_values": {"10": "LOT_INLINE"},
                        "reports": [
                            {"rptid": 1, "name": "Lot", "dvids": [10]}
                        ],
                        "events": [
                            {"ceid": 7101, "name": "lot_start", "rptids": [1]},
                            {"ceid": 7102, "name": "wafer_start", "rptids": [1]},
                            {"ceid": 7103, "name": "wafer_end", "rptids": [1]},
                            {"ceid": 7104, "name": "lot_end", "rptids": [1]},
                        ],
                        "svids": [
                            {"svid": 101, "name": "Mode", "type": "A", "value": "AUTO"}
                        ],
                    },
                },
            }
        ],
    }

    machine = service_config_from_dict(raw).machines[0]

    assert machine.runtime_mode == "simulated"
    assert machine.csv_local_dir == tmp_path / "csv"
    assert machine.log_dir == tmp_path / "logs"
    assert machine.simulator_log_dir == tmp_path / "sim-logs"
    assert machine.linkstuffs_http.base_url == "https://machine.example"
    assert machine.linkstuffs_http.device_token == "machine-token"
    assert machine.linkstuffs_http.verify_tls is True
    assert machine.linkstuffs_http.retry_count == 7
    assert machine.simulator.mdln == "MG22-300"
    assert machine.simulator.alarm_id == 42
    assert machine.simulator.alarm_text == "simulated alarm"
    assert machine.simulator.wafer_count == 5
    assert machine.simulator.ceid_overrides == {"lot_start": 7001}
    assert machine.simulator.svid_values == {"100": "READY", "101": "AUTO"}
    assert machine.simulator.event_definitions["reports"][0]["rptid"] == 1


@pytest.mark.parametrize(
    ("definitions", "message"),
    [
        (
            {
                "reports": [{"rptid": 1, "name": "R", "dvids": [10]}],
                "events": [{"ceid": 1, "name": "lot_start", "rptids": [2]}],
                "dvid_names": {"10": "LotID"},
                "dvid_types": {"10": "A"},
            },
            "unknown report",
        ),
        (
            {
                "reports": [],
                "events": [
                    {"ceid": 1, "name": "lot_start", "rptids": []},
                    {"ceid": 1, "name": "lot_end", "rptids": []},
                ],
            },
            "duplicate CEID",
        ),
        (
            {
                "reports": [],
                "events": [],
                "svids": [
                    {"svid": 1, "name": "State", "type": "U1", "value": "bad"}
                ],
            },
            "type U1",
        ),
    ],
)
def test_simulator_event_definitions_are_strict(definitions, message):
    raw = {
        **_machine(),
        "runtime_mode": "simulated",
        "simulator": {"event_definitions": definitions},
    }

    with pytest.raises(ConfigError, match=message):
        service_config_from_dict({"machines": [raw]})


def test_advanced_simulator_must_match_its_profile():
    raw = {
        **_machine(),
        "runtime_mode": "simulated",
        "simulator": {"implementation": "davinci_advanced"},
    }

    with pytest.raises(ConfigError, match="davinci_advanced"):
        service_config_from_dict({"machines": [raw]})


@pytest.mark.parametrize("runtime_mode", ["simulation", "hardware", ""])
def test_runtime_mode_is_explicit(runtime_mode):
    raw = {"machines": [{**_machine(), "runtime_mode": runtime_mode}]}
    with pytest.raises(ConfigError, match="runtime_mode"):
        service_config_from_dict(raw)


def test_simulated_machines_cannot_share_a_local_socket():
    first = {**_machine("TOOL_01"), "runtime_mode": "simulated"}
    second = {**_machine("TOOL_02"), "runtime_mode": "simulated"}
    raw = {"machines": [first, second]}

    with pytest.raises(ConfigError, match="simulator endpoint"):
        service_config_from_dict(raw)


def test_legacy_flat_storage_and_global_http_still_work():
    raw = {
        "linkstuffs_http": {
            "enabled": True,
            "base_url": "https://global.example",
            "device_tokens": {"TOOL_01": "global-token"},
        },
        "machines": [
            {
                **_machine(),
                "offline_test_mode": False,
                "local_csv_path": "legacy-csv",
            }
        ],
    }

    machine = service_config_from_dict(raw).machines[0]

    assert machine.csv_local_dir == Path("legacy-csv")
    assert machine.linkstuffs_http.base_url == "https://global.example"
    assert machine.linkstuffs_http.device_token == "global-token"


def test_enabled_real_ptiq_rejects_a_missing_installation_definition(tmp_path):
    machine = {
        **_machine(),
        "machine_profile": "ptiq_secsgem",
        "event_subscription_path": str(tmp_path / "missing.json"),
    }
    with pytest.raises(ConfigError, match="PTIQ.*event_subscription_path"):
        service_config_from_dict({"machines": [machine]})

    machine["runtime_mode"] = "simulated"
    assert service_config_from_dict({"machines": [machine]}).machines[0].is_simulated


def test_enabled_real_ptiq_rejects_an_incomplete_installation_definition(tmp_path):
    definition = tmp_path / "EventSubscription.json"
    definition.write_text(
        json.dumps(
            {
                "events": [
                    {"ceid": 4001, "name": "SCH1.LotStarted", "rptids": []}
                ]
            }
        ),
        encoding="utf-8",
    )
    machine = {
        **_machine(),
        "machine_profile": "ptiq_secsgem",
        "event_subscription_path": str(definition),
    }

    with pytest.raises(ConfigError, match="complete.*PTIQ|PTIQ.*complete"):
        service_config_from_dict({"machines": [machine]})


def test_nexgen_safeguards_default_on_when_not_explicitly_configured():
    machine = service_config_from_dict({"machines": [_machine()]}).machines[0]

    assert machine.request_online is True
    assert machine.enable_alarms is True
    assert machine.alarm_rate_limit == 50


def test_simulator_ceid_overrides_must_be_unique():
    raw = {
        **_machine(),
        "runtime_mode": "simulated",
        "simulator": {
            "ceid_overrides": {"lot_start": 7777, "lot_end": 7777}
        },
    }

    with pytest.raises(ConfigError, match="unique"):
        service_config_from_dict({"machines": [raw]})


def test_simulator_override_cannot_reuse_machine_defined_ceid(tmp_path):
    definition = tmp_path / "EventSubscription.json"
    definition.write_text(
        json.dumps(
            {
                "events": [
                    {"ceid": 7777, "name": "LOT_END", "rptids": []}
                ],
                "reports": [],
            }
        ),
        encoding="utf-8",
    )
    raw = {
        **_machine(),
        "runtime_mode": "simulated",
        "event_subscription_path": str(definition),
        "simulator": {"ceid_overrides": {"lot_start": 7777}},
    }

    with pytest.raises(ConfigError, match="cannot reuse CEID 7777"):
        service_config_from_dict({"machines": [raw]})


def test_reconnect_delay_is_bounded_exponential():
    assert reconnect_delay(10, 1, jitter=0) == 10
    assert reconnect_delay(10, 2, jitter=0) == 20
    assert reconnect_delay(10, 6, jitter=0) == 160
    assert reconnect_delay(10, 20, jitter=0) == 160
    assert reconnect_delay(30, 20, jitter=0) == 300


def test_enabled_online_machine_accepts_mqtt_only_without_https_token():
    raw = {
        "linkstuffs": {"enabled": True, "access_token": "mqtt-token"},
        "machines": [
            {
                **_machine(),
                "offline_test_mode": False,
                "linkstuffs_http": {
                    "enabled": False,
                    "base_url": "https://machine.example",
                },
            }
        ],
    }

    config = service_config_from_dict(raw)
    assert config.linkstuffs.enabled is True
    assert config.machines[0].linkstuffs_http.enabled is False


def _service_config(tmp_path, machines):
    return service_config_from_dict(
        {
            "paths": {
                "install_dir": str(tmp_path / "install"),
                "data_dir": str(tmp_path / "data"),
                "outbox_db": str(tmp_path / "mqtt.sqlite3"),
                "http_outbox_db": str(tmp_path / "http.sqlite3"),
                "legacy_api_outbox_db": str(tmp_path / "legacy.sqlite3"),
            },
            "startup_stagger_sec": 0,
            "machines": machines,
        }
    )


class _FakeSession:
    def __init__(self, machine, **_kwargs):
        self.machine = machine
        self.host = SimpleNamespace(is_connected=True, last_event_time=None)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout: float = 10.0):
        # Mirrors the real signature: EapMiddlewareService.stop() shares
        # one shutdown deadline across every worker and passes each its
        # remaining slice.
        del timeout
        self.stopped = True

    def request_svids(self, _svids):
        return {}


class _FakeRunner:
    instances = []

    def __init__(self, config):
        self.config = config
        self.stopped = False
        self.instances.append(self)

    def run(self):
        return 0

    def request_stop(self):
        self.stopped = True


class _FactoryCapturingRunner(_FakeRunner):
    factories = []

    def __init__(self, config, simulator_factory=None):
        super().__init__(config)
        self.factories.append(simulator_factory)


class _FakeHttpPublisher:
    instances = []

    def __init__(self, config, outbox, owner_display_name=None):
        self.config = config
        self.outbox = outbox
        self.owner_display_name = owner_display_name
        self.started = False
        self.stopped = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout: float = 10.0):
        # Mirrors the real signature: EapMiddlewareService.stop() shares
        # one shutdown deadline across every worker and passes each its
        # remaining slice.
        del timeout
        self.stopped = True

    def queue_machine_attributes(self, *_args):
        pass

    def queue_event(self, *_args):
        pass


def test_reconcile_changes_only_the_affected_runtime(tmp_path):
    first = {**_machine("TOOL_01", 5101), "svid_collection_enabled": False}
    second = {**_machine("TOOL_02", 5102), "svid_collection_enabled": False}
    initial = _service_config(tmp_path, [first, second])

    with patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession):
        service = EapMiddlewareService(initial)
        try:
            service.start()
            sessions = dict(service._sessions)

            assert service.reconcile(initial) == {}
            assert service._sessions == sessions

            storage_only = {
                **second,
                "storage": {"local_csv_path": str(tmp_path / "new-csv")},
            }
            service.reconcile(_service_config(tmp_path, [first, storage_only]))
            assert service._sessions["TOOL_02"] is sessions["TOOL_02"]

            changed = {**first, "host": "127.0.0.2"}
            actions = service.reconcile(
                _service_config(tmp_path, [changed, storage_only])
            )
            assert actions == {"TOOL_01": "restarted"}
            assert sessions["TOOL_01"].stopped is True
            assert service._sessions["TOOL_01"] is not sessions["TOOL_01"]
            assert service._sessions["TOOL_02"] is sessions["TOOL_02"]

            disabled = {**changed, "enabled": False}
            actions = service.reconcile(
                _service_config(tmp_path, [disabled, storage_only])
            )
            assert actions == {"TOOL_01": "stopped"}
            assert "TOOL_01" not in service._sessions
            assert service._sessions["TOOL_02"] is sessions["TOOL_02"]
        finally:
            service.stop()


def test_service_owns_the_opposite_role_simulator(tmp_path):
    machine = {
        **_machine("TOOL_SIM", 5110),
        "runtime_mode": "simulated",
        "svid_collection_enabled": False,
        "simulator": {"mdln": "MG22", "softrev": "1.2", "wafer_count": 4},
    }
    config = _service_config(tmp_path, [machine])
    _FakeRunner.instances.clear()

    with (
        patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession),
        patch("simulator.runner.SimulatorRunner", _FakeRunner),
    ):
        service = EapMiddlewareService(config)
        try:
            service.start()
            simulator = _FakeRunner.instances[0].config
            assert simulator.connection.mode == "passive"
            # Embedded simulators are a local test double, never a LAN-facing
            # listener. Standalone passive simulators require an explicit
            # allow_external_bind opt-in for non-loopback addresses.
            assert simulator.connection.address == "127.0.0.1"
            assert simulator.simulation.profile == "nexgen_mg_series"
            assert simulator.simulation.mdln == "MG22"
            assert simulator.simulation.wafer_count == 4
            assert service._sessions["TOOL_SIM"].machine.host == "127.0.0.1"
        finally:
            service.stop()

    assert _FakeRunner.instances[0].stopped is True


def test_simulator_setting_change_restarts_only_the_simulator(tmp_path):
    machine = {
        **_machine("TOOL_SIM", 5111),
        "runtime_mode": "simulated",
        "svid_collection_enabled": False,
        "simulator": {"wafer_count": 2},
    }
    initial = _service_config(tmp_path, [machine])
    _FakeRunner.instances.clear()

    with (
        patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession),
        patch("simulator.runner.SimulatorRunner", _FakeRunner),
    ):
        service = EapMiddlewareService(initial)
        try:
            service.start()
            session = service._sessions["TOOL_SIM"]
            first_runner = _FakeRunner.instances[-1]
            changed = {
                **machine,
                "simulator": {"wafer_count": 7},
            }

            actions = service.reconcile(_service_config(tmp_path, [changed]))

            assert actions == {"TOOL_SIM": "simulator_restarted"}
            assert service._sessions["TOOL_SIM"] is session
            assert session.stopped is False
            assert first_runner.stopped is True
            assert _FakeRunner.instances[-1].config.simulation.wafer_count == 7
        finally:
            service.stop()


def test_simulator_ceid_override_maps_on_the_middleware_side(tmp_path):
    raw = {
        **_machine("TOOL_SIM", 5112),
        "runtime_mode": "simulated",
        "simulator": {"ceid_overrides": {"lot_start": 7777}},
    }
    config = _service_config(tmp_path, [raw])
    service = EapMiddlewareService(config)

    profile = service._profile_for(config.machines[0])
    subscription_path = service._subscription_path_for(config.machines[0])
    definition = json.loads(Path(subscription_path).read_text(encoding="utf-8"))

    assert profile.resolve_event(ceid=7777).event_type == "lot_start"
    assert any(event["ceid"] == 7777 for event in definition["events"])


def test_advanced_simulator_is_selected_through_service_control(tmp_path):
    machine = {
        **_machine("TOOL_DAV", 5115),
        "machine_profile": "davinci_200_mc4_hc1",
        "runtime_mode": "simulated",
        "svid_collection_enabled": False,
        "simulator": {"implementation": "davinci_advanced"},
    }
    config = _service_config(tmp_path, [machine])
    _FactoryCapturingRunner.factories.clear()

    with (
        patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession),
        patch("simulator.runner.SimulatorRunner", _FactoryCapturingRunner),
    ):
        service = EapMiddlewareService(config)
        try:
            service.start()
        finally:
            service.stop()

    assert _FactoryCapturingRunner.factories[-1].__name__ == "SecsGemEquipment"


def test_slow_endpoint_cleanup_does_not_delay_another_endpoint(tmp_path, monkeypatch):
    old = _service_config(tmp_path, [_machine("TOOL_OLD", 5113)])
    new = _service_config(tmp_path, [_machine("TOOL_NEW", 5114)])
    service = EapMiddlewareService(old)
    service._sessions["TOOL_OLD"] = object()
    service._machines_by_endpoint["TOOL_OLD"] = old.machines[0]
    stop_entered = threading.Event()
    release_stop = threading.Event()
    new_started = threading.Event()

    def slow_stop(endpoint_id, reason):
        _ = reason
        assert endpoint_id == "TOOL_OLD"
        stop_entered.set()
        release_stop.wait(2)
        service._sessions.pop(endpoint_id, None)
        service._machines_by_endpoint.pop(endpoint_id, None)

    def start(machine):
        service._sessions[machine.endpoint_id] = object()
        service._machines_by_endpoint[machine.endpoint_id] = machine
        new_started.set()

    monkeypatch.setattr(service, "_stop_machine", slow_stop)
    monkeypatch.setattr(service, "_start_machine", start)
    reconcile = threading.Thread(target=service.reconcile, args=(new,), daemon=True)
    reconcile.start()
    try:
        assert stop_entered.wait(1)
        assert new_started.wait(0.5), "TOOL_NEW was blocked by TOOL_OLD cleanup"
    finally:
        release_stop.set()
        reconcile.join(2)


def test_each_machine_gets_an_independent_http_route_and_outbox(tmp_path):
    def with_route(endpoint, port, token, base_url):
        return {
            **_machine(endpoint, port),
            "svid_collection_enabled": False,
            "linkstuffs_http": {
                "enabled": True,
                "base_url": base_url,
                "device_token": token,
            },
        }

    first = with_route("TOOL_01", 5121, "token-one", "https://one.example")
    second = with_route("TOOL_02", 5122, "token-two", "https://two.example")
    config = _service_config(tmp_path, [first, second])
    _FakeHttpPublisher.instances.clear()

    with (
        patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession),
        # The name is bound in two places since the service package split:
        # the service-wide publisher (state) and the per-machine one (http_outbox).
        patch("eap_middleware.service.state.LinkstuffsHttpPublisher", _FakeHttpPublisher),
        patch(
            "eap_middleware.service.http_outbox.LinkstuffsHttpPublisher",
            _FakeHttpPublisher,
        ),
    ):
        service = EapMiddlewareService(config)
        try:
            service.start()
            session = service._sessions["TOOL_02"]
            routes = [item for item in _FakeHttpPublisher.instances if item.config.enabled]
            assert {tuple(item.config.device_tokens.values())[0] for item in routes} == {
                "token-one", "token-two"
            }
            assert service._http_outboxes["TOOL_01"].db_path != service._http_outboxes["TOOL_02"].db_path

            changed = with_route(
                "TOOL_01", 5121, "rotated-token", "https://new-one.example"
            )
            service.reconcile(_service_config(tmp_path, [changed, second]))
            assert service._sessions["TOOL_02"] is session
            assert service._http_publishers["TOOL_01"].config.device_tokens == {
                "TOOL_01": "rotated-token"
            }
        finally:
            service.stop()


def test_atomic_config_save_rejects_a_stale_editor(tmp_path):
    path = tmp_path / "production.yaml"
    path.write_text("machines: []\n", encoding="utf-8")
    original = save_config_atomic(path, {"machines": []})
    path.write_text("machines:\n  - external: true\n", encoding="utf-8")

    with pytest.raises(StaleConfigError):
        save_config_atomic(path, {"machines": []}, expected_revision=original)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "machines": [{"external": True}]
    }


def test_command_inbox_keeps_rapid_repeated_commands(tmp_path):
    first = submit_command(tmp_path, "restart", "TOOL_01")
    second = submit_command(tmp_path, "restart", "TOOL_01")

    assert first != second
    commands = consume_commands(tmp_path)
    assert [item["request_id"] for item in commands] == [first, second]
    assert consume_commands(tmp_path) == []


def test_status_snapshot_never_contains_machine_tokens(tmp_path):
    config = _service_config(
        tmp_path,
        [
            {
                **_machine(),
                "linkstuffs_http": {
                    "enabled": True,
                    "base_url": "https://machine.example",
                    "device_token": "super-secret",
                },
            }
        ],
    )
    with patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession):
        service = EapMiddlewareService(config)
        try:
            service.start()
            status = load_status(Path(config.paths.control_dir))
        finally:
            service.stop()

    assert status["machines"]["TOOL_01"]["machine_profile"] == "nexgen_mg_series"
    assert "DOCUMENTATION-DERIVED" in status["machines"]["TOOL_01"]["profile_provenance"]
    assert "super-secret" not in repr(status)


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_supervisor_rejects_invalid_yaml_then_applies_the_fixed_revision(tmp_path):
    raw = {
        "paths": {
            "install_dir": str(tmp_path / "install"),
            "log_dir": str(tmp_path / "logs"),
            "data_dir": str(tmp_path / "data"),
            "outbox_db": str(tmp_path / "mqtt.sqlite3"),
            "http_outbox_db": str(tmp_path / "http.sqlite3"),
            "legacy_api_outbox_db": str(tmp_path / "legacy.sqlite3"),
        },
        "startup_stagger_sec": 0,
        "machines": [{**_machine("TOOL_01", 5201), "svid_collection_enabled": False}],
    }
    path = tmp_path / "production.yaml"
    save_config_atomic(path, raw)

    with patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession):
        service = EapMiddlewareService(load_service_config(path), config_path=path)
        try:
            service.start()
            first = service._sessions["TOOL_01"]
            path.write_text("machines: [", encoding="utf-8")
            assert _wait_for(
                lambda: bool(
                    load_status(tmp_path / "control").get("last_invalid_config")
                )
            )
            assert service._sessions["TOOL_01"] is first

            raw["machines"].append(
                {**_machine("TOOL_02", 5202), "svid_collection_enabled": False}
            )
            save_config_atomic(path, raw, expected_revision=file_revision(path))
            assert _wait_for(lambda: "TOOL_02" in service._sessions)
            assert service._sessions["TOOL_01"] is first
        finally:
            service.stop()


def test_supervisor_survives_a_command_inbox_read_failure(tmp_path, monkeypatch):
    """A transient OSError draining the command inbox (e.g. Windows AV/backup
    briefly holding a command file) must not kill the ConfigurationSupervisor
    thread - it also owns hot config reload, journal replay and journal
    purge. Regression: this call was the one I/O site in the loop with no
    try/except, unlike its neighbours (journal replay, journal purge, status
    write all catch and log)."""
    raw = {
        "paths": {
            "install_dir": str(tmp_path / "install"),
            "log_dir": str(tmp_path / "logs"),
            "data_dir": str(tmp_path / "data"),
            "outbox_db": str(tmp_path / "mqtt.sqlite3"),
            "http_outbox_db": str(tmp_path / "http.sqlite3"),
            "legacy_api_outbox_db": str(tmp_path / "legacy.sqlite3"),
        },
        "startup_stagger_sec": 0,
        "machines": [{**_machine("TOOL_01", 5203), "svid_collection_enabled": False}],
    }
    path = tmp_path / "production.yaml"
    save_config_atomic(path, raw)

    calls = {"n": 0}
    real_consume_commands = consume_commands

    def flaky_consume_commands(data_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("command file locked by another process")
        return real_consume_commands(data_dir)

    import eap_middleware.service as service_mod
    monkeypatch.setattr(
        service_mod.control_plane, "consume_commands", flaky_consume_commands
    )

    with patch("eap_middleware.service.lifecycle.SecsMachineSession", _FakeSession):
        service = EapMiddlewareService(load_service_config(path), config_path=path)
        try:
            service.start()
            assert _wait_for(lambda: calls["n"] >= 1)
            # The supervisor thread must still be alive and ticking after the
            # exception, not silently dead.
            assert service._supervisor_thread.is_alive()

            # Prove it is genuinely still doing its job, not just "not dead":
            # a config revision applied after the failing tick must still be
            # picked up.
            raw["machines"].append(
                {**_machine("TOOL_02", 5204), "svid_collection_enabled": False}
            )
            save_config_atomic(path, raw, expected_revision=file_revision(path))
            assert _wait_for(lambda: "TOOL_02" in service._sessions)
        finally:
            service.stop()


def test_csv_destination_is_captured_per_lot_and_stop_marks_partial(tmp_path):
    profile = ProfileRegistry().get("spts_fxp_omega")
    writer = PerLotCsvWriter()
    old = MachineConfig(
        endpoint_id="TOOL_01",
        display_name="SPTS_01",
        machine_profile="spts_fxp_omega",
        host="127.0.0.1",
        port=5000,
        storage=MachineStorageConfig(local_csv_path=str(tmp_path / "old")),
    )
    new = MachineConfig(
        **{
            **old.__dict__,
            "storage": MachineStorageConfig(local_csv_path=str(tmp_path / "new")),
        }
    )

    def event(event_type, lot_id):
        ceid = next(
            value
            for value in profile.ceid_aliases
            if profile.resolve_event(ceid=value).event_type == event_type
        )
        return CanonicalEvent(
            timestamp=datetime.now(timezone.utc),
            endpoint_id="TOOL_01",
            display_name="SPTS_01",
            machine_profile="spts_fxp_omega",
            vendor=profile.vendor,
            model=profile.model,
            event_type=event_type,
            ceid=ceid,
            lot_id=lot_id,
        )

    writer.append(old, profile, event("lot_start", "LOT_1"))
    first_files = writer.append(new, profile, event("unloaded", "LOT_1"))
    assert first_files[0].parent == tmp_path / "old"

    writer.append(new, profile, event("lot_start", "LOT_2"))
    partial = writer.flush_machine("TOOL_01", reason="stopped")
    assert partial[0].parent == tmp_path / "new"
    assert partial[0].name.endswith(".partial.csv")


# ----- hot reload: every connection-affecting setting must restart the session -----

def test_restart_signature_covers_every_connection_affecting_field():
    """A MachineConfig field missing from _restart_signature is accepted in
    production.yaml, saved, and then silently never applied to the running
    session. `hsms_timers` shipped that way once; this pins the whole set so
    the next field added has to make the choice explicitly.
    """
    import dataclasses

    from eap_middleware.models import MachineConfig
    from eap_middleware.service import EapMiddlewareService

    all_fields = {f.name for f in dataclasses.fields(MachineConfig)}
    must_restart = all_fields - EapMiddlewareService._NO_RESTART_FIELDS

    # Build two machines differing only in the field under test and check the
    # signature actually changes - reading the source would not prove it.
    base = MachineConfig(
        endpoint_id="TOOL_SIG",
        display_name="SIG",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
    )
    alternatives = {
        "machine_profile": "spts_fxp_omega",
        "host": "127.0.0.2",
        "port": 5001,
        "secs_device_id": 7,
        "runtime_mode": "simulated",
        "event_subscription_path": "somewhere/else.json",
        "event_subscription_enabled": False,
        "svid_collection_enabled": False,
        "enable_alarms": True,
        "hsms_timers": {"t3": 31},
        "request_online": True,
        "drain_spool_on_connect": True,
        "reset_subscription_on_connect": True,
        "hsms_mode": "passive",
        "hsms_bind_address": "10.0.0.1",
        "alarm_rate_limit": 99,
    }
    missing_case = must_restart - set(alternatives)
    assert not missing_case, (
        "this test needs a differing value for every restart-affecting field; "
        f"add one for: {sorted(missing_case)}"
    )

    signature = EapMiddlewareService._restart_signature
    ignored = []
    for name in sorted(must_restart):
        changed = dataclasses.replace(base, **{name: alternatives[name]})
        if signature(base) == signature(changed):
            ignored.append(name)
    assert not ignored, (
        "changing these settings does not restart the machine, so editing "
        f"them while the service runs has no effect: {ignored}"
    )
