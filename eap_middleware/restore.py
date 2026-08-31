"""Offline restore verification; never contacts equipment or upstreams."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import load_service_config
from .service import EapMiddlewareService


@dataclass
class RestoreReport:
    ok: bool = True
    checks: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


def _sqlite_check(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"present": False, "integrity": False, "foreign_keys": []}
    connection = sqlite3.connect(str(path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()
        return {
            "present": True,
            "integrity": bool(integrity and str(integrity[0]).lower() == "ok"),
            "foreign_keys": [list(row) for row in foreign_keys],
            "user_version": int(version[0]) if version else 0,
        }
    finally:
        connection.close()


def verify_restore(
    config_path: Path,
    *,
    expected_release: str = "",
    release_identity_path: Path | None = None,
    csv_roots: Iterable[Path] = (),
) -> RestoreReport:
    report = RestoreReport()
    try:
        config = load_service_config(config_path)
        report.checks["configuration"] = "valid"
    except Exception as exc:
        report.fail(f"configuration validation failed: {exc}")
        return report

    databases = {
        "ingress_journal": Path(config.paths.ingress_journal_db),
        "mqtt_outbox": Path(config.paths.outbox_db),
        "https_outbox": Path(config.paths.http_outbox_db),
        "legacy_outbox": Path(config.paths.legacy_api_outbox_db),
    }
    db_results = {name: _sqlite_check(path) for name, path in databases.items()}
    report.checks["databases"] = db_results
    for name, result in db_results.items():
        if not result["present"]:
            report.fail(f"required restored database is missing: {name}")
        elif not result["integrity"] or result["foreign_keys"]:
            report.fail(f"restored database failed integrity checks: {name}")

    csv_result: dict[str, Any] = {"files": 0, "rows": 0, "invalid": []}
    for root in csv_roots:
        for path in root.rglob("*.csv") if root.exists() else ():
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.reader(handle)
                    header = next(reader)
                    if not header or len(set(header)) != len(header):
                        raise ValueError("empty or duplicate CSV header")
                    rows = sum(1 for _ in reader)
                csv_result["files"] += 1
                csv_result["rows"] += rows
            except Exception as exc:
                csv_result["invalid"].append({"path": str(path), "error": str(exc)})
    report.checks["csv"] = csv_result
    if csv_result["invalid"]:
        report.fail("one or more restored CSV files are invalid")

    if release_identity_path is not None:
        try:
            identity = json.loads(release_identity_path.read_text(encoding="utf-8"))
            actual = str(identity.get("commit", ""))
            report.checks["release_identity"] = actual
            if expected_release and actual != expected_release:
                report.fail(
                    f"restored release identity {actual!r} does not match "
                    f"expected {expected_release!r}"
                )
        except Exception as exc:
            report.fail(f"release identity validation failed: {exc}")

    # Constructing the service validates schema compatibility and imports all
    # startup components, but deliberately does not call start(): no equipment,
    # tenant, listener, or publisher is contacted by a restore verification.
    try:
        EapMiddlewareService(config, config_path=config_path)
        report.checks["offline_startup_probe"] = "passed"
    except Exception as exc:
        report.fail(f"offline startup probe failed: {exc}")
    return report
