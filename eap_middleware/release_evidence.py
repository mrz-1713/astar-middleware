"""Fail-closed release evidence creation and approval validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class EvidenceError(ValueError):
    pass


EXTERNAL_GATES = (
    "tenant_https_mqtt",
    "davinci_equipment",
    "spts_equipment",
    "ptiq_equipment",
    "nexgen_equipment",
    "windows_reboot_power_loss",
    "disk_full_drill",
    "backup_restore_drill",
    "oem_safety_access_approval",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git command, no shell
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def source_identity(root: Path) -> dict[str, object]:
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "dirty": bool(_git(root, "status", "--porcelain")),
    }


def _authenticode(path: Path) -> dict[str, object]:
    if os.name != "nt":
        verifier = shutil.which("osslsigncode")
        if verifier is None:
            return {"verified": False, "status": "verifier-unavailable"}
        completed = subprocess.run(  # noqa: S603 - resolved verifier, no shell
            [verifier, "verify", "-in", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "verified": completed.returncode == 0,
            "status": "valid" if completed.returncode == 0 else "invalid",
            "message": completed.stdout[-1000:] + completed.stderr[-1000:],
        }
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "$s | Select-Object Status,StatusMessage,SignerCertificate | "
        "ConvertTo-Json -Compress"
    )
    completed = subprocess.run(  # noqa: S603 - fixed PowerShell command
        ["powershell.exe", "-NoProfile", "-Command", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(completed.stdout)
    return {
        "verified": str(raw.get("Status", "")).lower() == "valid",
        "status": raw.get("Status"),
        "message": raw.get("StatusMessage"),
    }


def create_evidence(
    root: Path,
    artifacts: Iterable[Path],
    *,
    sbom: Path,
    test_results: Iterable[Path] = (),
) -> dict[str, object]:
    identity = source_identity(root)
    artifact_rows = []
    for artifact in artifacts:
        artifact = artifact.resolve()
        artifact_rows.append(
            {
                "path": str(artifact),
                "sha256": sha256_file(artifact),
                "signature": _authenticode(artifact),
            }
        )
    locks = [root / "requirements.txt", root / "requirements-release.lock"]
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": identity,
        "provenance": {
            "commit": identity["commit"],
            "builder": os.environ.get("GITHUB_WORKFLOW_REF", "local-explicit-build"),
            "build_id": os.environ.get("GITHUB_RUN_ID", "local"),
        },
        "dependency_locks": [
            {"path": str(path), "sha256": sha256_file(path)} for path in locks
        ],
        "artifacts": artifact_rows,
        "tests": [
            {"path": str(path.resolve()), "sha256": sha256_file(path)}
            for path in test_results
        ],
        "sbom": {"path": str(sbom.resolve()), "sha256": sha256_file(sbom)},
        "external_gates": {gate: False for gate in EXTERNAL_GATES},
    }


def validate_evidence(
    evidence: Mapping[str, Any],
    *,
    root: Path,
    require_external: bool = True,
) -> list[str]:
    errors: list[str] = []
    if evidence.get("schema_version") != 1:
        errors.append("unsupported or missing evidence schema_version")
    try:
        actual_source = source_identity(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"could not establish source identity: {exc}")
        actual_source = {}
    source = evidence.get("source")
    if not isinstance(source, Mapping):
        errors.append("source identity is missing")
    else:
        if source.get("dirty") is not False:
            errors.append("evidence was not created from a clean tree")
        if actual_source.get("dirty") is not False:
            errors.append("current tree is dirty")
        if source.get("commit") != actual_source.get("commit"):
            errors.append("evidence commit does not match the current commit")

    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("no release artifacts are recorded")
    else:
        for index, row in enumerate(artifacts):
            if not isinstance(row, Mapping):
                errors.append(f"artifact[{index}] is malformed")
                continue
            path = Path(str(row.get("path", "")))
            if not path.is_file():
                errors.append(f"artifact is missing: {path}")
                continue
            if row.get("sha256") != sha256_file(path):
                errors.append(f"artifact hash mismatch: {path}")
            signature = row.get("signature")
            if not isinstance(signature, Mapping) or signature.get("verified") is not True:
                errors.append(f"artifact signature is absent or invalid: {path}")
            else:
                actual_signature = _authenticode(path)
                if actual_signature.get("verified") is not True:
                    errors.append(
                        f"artifact signature could not be independently verified: {path}"
                    )

    locks = evidence.get("dependency_locks")
    if not isinstance(locks, list) or not locks:
        errors.append("dependency-lock evidence is missing")
    else:
        for row in locks:
            if not isinstance(row, Mapping):
                errors.append("dependency-lock evidence is malformed")
                continue
            path = Path(str(row.get("path", "")))
            if not path.is_file() or row.get("sha256") != sha256_file(path):
                errors.append(f"dependency lock is missing or mismatched: {path}")

    tests = evidence.get("tests")
    if not isinstance(tests, list) or not tests:
        errors.append("test-result evidence is missing")
    else:
        for row in tests:
            if not isinstance(row, Mapping):
                errors.append("test-result evidence is malformed")
                continue
            path = Path(str(row.get("path", "")))
            if not path.is_file() or row.get("sha256") != sha256_file(path):
                errors.append(f"test result is missing or mismatched: {path}")

    provenance = evidence.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("commit") != actual_source.get("commit")
        or not provenance.get("builder")
    ):
        errors.append("build provenance is missing or commit-mismatched")

    sbom = evidence.get("sbom")
    if not isinstance(sbom, Mapping):
        errors.append("SBOM evidence is missing")
    else:
        sbom_path = Path(str(sbom.get("path", "")))
        if not sbom_path.is_file() or sbom.get("sha256") != sha256_file(sbom_path):
            errors.append("SBOM is missing or its hash does not match")

    if require_external:
        gates = evidence.get("external_gates")
        for gate in EXTERNAL_GATES:
            if not isinstance(gates, Mapping) or gates.get(gate) is not True:
                errors.append(f"external release gate is incomplete: {gate}")
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--root", type=Path, default=Path.cwd())
    create.add_argument("--artifact", action="append", type=Path, required=True)
    create.add_argument("--sbom", type=Path, required=True)
    create.add_argument("--test-result", action="append", type=Path, default=[])
    create.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("evidence", type=Path)
    validate.add_argument("--root", type=Path, default=Path.cwd())
    validate.add_argument("--candidate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create":
        payload = create_evidence(
            args.root.resolve(), args.artifact, sbom=args.sbom,
            test_results=args.test_result,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 0
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    errors = validate_evidence(
        payload, root=args.root.resolve(), require_external=not args.candidate_only
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print("Release evidence is valid")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
