"""A failed start() must leave nothing behind.

start() takes the single-instance lockfile before any other side effect, then
starts outbox maintenance threads, the publishers, the legacy-API listener and
every machine session. Any of those can fail for ordinary operational reasons:
an unreachable broker, a legacy-API port already in use, a machine entry that
reconcile rejects.

Without an unwind the caller sees the real exception but the process is left
holding the lockfile with no owner and with `_running` still True - so the
retry returns instantly at the `if self._running` guard and does nothing, and
every *later* attempt fails on the lock rather than on the original cause. The
control panel already unwound by hand for exactly this reason (gui/app.py);
these tests pin the behaviour in the service itself, which is what the CLI
(`run-service` -> run_forever) and any embedder get.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from eap_middleware.config import service_config_from_dict
from eap_middleware.service import EapMiddlewareService


def _config(tmp_path):
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
            "machines": [],
        }
    )


class _Boom(RuntimeError):
    """Stands in for an unreachable broker or a port already in use."""


def test_failed_start_releases_the_single_instance_lock(tmp_path):
    service = EapMiddlewareService(_config(tmp_path))
    with patch.object(
        type(service.publisher), "start", side_effect=_Boom("broker unreachable")
    ):
        with pytest.raises(_Boom):
            service.start()
    assert not service.instance_lock._held, (
        "the lockfile is still held after a failed start; every later start "
        "would fail on the lock instead of on the real cause"
    )
    assert not service.instance_lock.lock_path.exists()


def test_failed_start_leaves_the_service_restartable(tmp_path):
    """`_running` must not stay True, or the retry is a silent no-op."""
    service = EapMiddlewareService(_config(tmp_path))
    with patch.object(
        type(service.publisher), "start", side_effect=_Boom("broker unreachable")
    ):
        with pytest.raises(_Boom):
            service.start()
    assert service._running is False

    # The retry must actually run start() again rather than returning at the
    # `if self._running` guard.
    service.start()
    try:
        assert service._running is True
        assert service.instance_lock._held
    finally:
        service.stop()


def test_failed_start_reports_the_original_cause_not_the_lock(tmp_path):
    """The exception that surfaces is the one an operator has to act on."""
    service = EapMiddlewareService(_config(tmp_path))
    with patch.object(
        type(service.legacy_api), "start", side_effect=_Boom("port 8080 in use")
    ):
        with pytest.raises(_Boom, match="port 8080 in use"):
            service.start()


def test_a_failing_cleanup_still_frees_the_lock(tmp_path):
    """stop() releases the lockfile last, so a fault inside it must not keep
    the lock alive in a process that carries on running (the GUI case)."""
    service = EapMiddlewareService(_config(tmp_path))
    with (
        patch.object(
            type(service.publisher), "start", side_effect=_Boom("broker unreachable")
        ),
        patch.object(
            EapMiddlewareService, "stop", side_effect=_Boom("teardown also failed")
        ),
    ):
        with pytest.raises(_Boom):
            service.start()
    assert not service.instance_lock._held
