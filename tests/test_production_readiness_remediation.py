from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import secsgem.hsms

from eap_middleware.config import ConfigError, service_config_from_dict
from eap_middleware.journal import IngressJournal, KIND_EVENT
from eap_middleware.models import StorageSafetyConfig
from eap_middleware.release_evidence import validate_evidence
from eap_middleware.restore import verify_restore
from eap_middleware.storage_safety import (
    CRITICAL,
    NORMAL,
    RECOVERING,
    WARNING,
    CapacitySample,
    StorageBackpressureError,
    StorageSafetyMonitor,
)
from simulator.data_generator import ProcessState
from simulator.equipment import EquipmentSimulator
from simulator.profile_simulator import ProfileSimulator


def test_storage_threshold_order_is_fail_closed() -> None:
    with pytest.raises(ConfigError, match="critical < warning < recovery"):
        service_config_from_dict(
            {
                "storage_safety": {
                    "critical_free_bytes": 20,
                    "warning_free_bytes": 10,
                    "recovery_free_bytes": 30,
                },
                "machines": [],
            }
        )


def test_storage_monitor_quiesces_and_recovers_after_integrity(tmp_path: Path) -> None:
    free = [50]
    transitions: list[tuple[str, str]] = []
    integrity = [False]
    config = StorageSafetyConfig(
        sample_interval_sec=60,
        debounce_samples=1,
        critical_free_bytes=100,
        warning_free_bytes=200,
        recovery_free_bytes=300,
        critical_free_percent=5,
        warning_free_percent=10,
        recovery_free_percent=15,
    )
    monitor = StorageSafetyMonitor(
        config,
        lambda: [tmp_path],
        lambda previous, current, details: transitions.append((previous, current)),
        probe=lambda path: CapacitySample(str(path), free[0], 1000),
        alert=lambda state, details: None,
        integrity_check=lambda: integrity[0],
    )
    assert monitor.sample(force=True) == CRITICAL
    with pytest.raises(StorageBackpressureError):
        monitor.require_ingress_capacity()

    free[0] = 400
    assert monitor.sample() == RECOVERING
    integrity[0] = True
    assert monitor.sample() == NORMAL
    monitor.require_ingress_capacity()
    assert transitions == [
        (NORMAL, CRITICAL),
        (CRITICAL, RECOVERING),
        (RECOVERING, NORMAL),
    ]


def test_storage_monitor_warning_keeps_ingress_open(tmp_path: Path) -> None:
    monitor = StorageSafetyMonitor(
        StorageSafetyConfig(
            debounce_samples=1,
            critical_free_bytes=100,
            warning_free_bytes=300,
            recovery_free_bytes=400,
            critical_free_percent=5,
            warning_free_percent=40,
            recovery_free_percent=50,
        ),
        lambda: [tmp_path],
        lambda previous, current, details: True,
        probe=lambda path: CapacitySample(str(path), 250, 1000),
        alert=lambda state, details: None,
    )
    assert monitor.sample() == WARNING
    monitor.require_ingress_capacity()


def test_cross_generation_retries_are_bounded(monkeypatch, tmp_path: Path) -> None:
    clock = [1000.0]
    monkeypatch.setattr("eap_middleware.journal.time.time", lambda: clock[0])
    journal = IngressJournal(
        tmp_path / "ingress.sqlite3", cross_generation_window_sec=10
    )
    kwargs = dict(
        endpoint_id="M1",
        kind=KIND_EVENT,
        stream=6,
        function=11,
        ceid=7,
        system_bytes=99,
        payload={"value": 1},
    )
    first, is_new = journal.append(**kwargs, generation=1)
    assert is_new
    clock[0] = 1005.0
    retry, is_new = journal.append(**kwargs, generation=2)
    assert not is_new and retry.seq == first.seq
    clock[0] = 1016.0
    repeat, is_new = journal.append(**kwargs, generation=3)
    assert is_new and repeat.seq != first.seq
    # Same-generation retransmission still collapses outside the time window.
    clock[0] = 2000.0
    same_generation, is_new = journal.append(**kwargs, generation=3)
    assert not is_new and same_generation.seq == repeat.seq


def _simulator() -> EquipmentSimulator:
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=5000,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    return EquipmentSimulator(settings)


def test_s2f13_decodes_real_wire_body_in_request_order() -> None:
    simulator = _simulator()
    request = simulator.stream_function(2, 13)([2, 1])
    response = simulator._handle_s2f13(None, SimpleNamespace(data=request.encode()))
    assert response.data.get() == [simulator.event_interval, simulator.tool_id]


def test_s2f41_decodes_wire_body_and_enforces_state() -> None:
    simulator = _simulator()
    start = simulator.stream_function(2, 41)(["START", []])
    accepted = simulator._handle_s2f41(None, SimpleNamespace(data=start.encode()))
    assert accepted.data.get()["HCACK"] == 0
    assert simulator._process_state == ProcessState.EXECUTING

    repeated = simulator._handle_s2f41(None, SimpleNamespace(data=start.encode()))
    assert repeated.data.get()["HCACK"] == 2
    unknown = simulator.stream_function(2, 41)(["DOES_NOT_EXIST", []])
    rejected = simulator._handle_s2f41(None, SimpleNamespace(data=unknown.encode()))
    assert rejected.data.get()["HCACK"] == 1


def test_spts_profile_reports_asynchronous_remote_command_acceptance() -> None:
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=5000,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    simulator = ProfileSimulator(settings, profile_id="spts_fxp_omega")
    request = simulator.stream_function(2, 41)(["START", []])
    response = simulator._handle_s2f41(None, SimpleNamespace(data=request.encode()))
    assert response.data.get()["HCACK"] == 4
    assert simulator._process_state == ProcessState.EXECUTING

    invalid = simulator.stream_function(2, 41)(
        ["STOP", [{"CPNAME": "UNSUPPORTED", "CPVAL": "1"}]]
    )
    response = simulator._handle_s2f41(None, SimpleNamespace(data=invalid.encode()))
    assert response.data.get()["HCACK"] == 3


def test_mqtt_only_is_a_supported_upstream_route() -> None:
    config = service_config_from_dict(
        {
            "linkstuffs": {
                "enabled": True,
                "access_token": "mqtt-secret",
                "tls": True,
            },
            "machines": [
                {
                    "endpoint_id": "M1",
                    "display_name": "M1",
                    "machine_profile": "ptiq_secsgem",
                    "host": "192.0.2.1",
                    "port": 5000,
                    "offline_test_mode": False,
                    "linkstuffs_http": {"enabled": False},
                }
            ],
        }
    )
    assert config.linkstuffs.enabled


def test_release_approval_rejects_missing_unsigned_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "eap_middleware.release_evidence.source_identity",
        lambda root: {"commit": "abc", "dirty": False},
    )
    errors = validate_evidence(
        {
            "schema_version": 1,
            "source": {"commit": "abc", "dirty": False},
            "external_gates": {},
        },
        root=tmp_path,
    )
    assert any("no release artifacts" in error for error in errors)
    assert any("signature" in error or "artifacts" in error for error in errors)
    assert any("external release gate" in error for error in errors)


def test_restore_verifier_checks_databases_without_contacting_equipment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "production.yaml"
    config_path.write_text(
        """
paths:
  install_dir: "{root}"
  log_dir: "{root}/logs"
  data_dir: "{root}/data"
  control_dir: "{root}/control"
  archive_dir: "{root}/archive"
  outbox_db: "{root}/data/outbox.sqlite3"
  legacy_api_outbox_db: "{root}/data/legacy.sqlite3"
  http_outbox_db: "{root}/data/http.sqlite3"
  ingress_journal_db: "{root}/data/ingress.sqlite3"
machines: []
""".format(root=tmp_path.as_posix()),
        encoding="utf-8",
    )
    # Construction creates/migrates all required databases but does not start
    # a publisher, listener, or equipment session.
    from eap_middleware.config import load_service_config
    from eap_middleware.service import EapMiddlewareService

    EapMiddlewareService(load_service_config(config_path), config_path=config_path)
    report = verify_restore(config_path)
    assert report.ok, report.errors
    assert report.checks["offline_startup_probe"] == "passed"


def test_release_tooling_and_upgrade_are_wired_into_distribution() -> None:
    root = Path(__file__).resolve().parents[1]
    dev = (root / "requirements-dev.txt").read_text(encoding="utf-8")
    release_lock = (root / "requirements-release.lock").read_text(encoding="utf-8")
    assert "openpyxl==3.1.5" in dev
    assert "openpyxl" not in release_lock

    upgrade = (root / "deploy" / "upgrade.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $InstallDir "releases"' in upgrade
    assert "New-Junction" in upgrade
    assert "Stop-Service" in upgrade
    assert "Candidate health probe timed out" in upgrade
    assert "rollback also failed" in upgrade

    service = (root / "scripts" / "install_service.ps1").read_text(encoding="utf-8")
    assert '"AppExit", "Default", "Restart"' in service
    assert '"AppRestartDelay", "5000"' in service
