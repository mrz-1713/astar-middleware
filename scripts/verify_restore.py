"""Verify an ASTAR application-data or bare-system restore offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eap_middleware.restore import verify_restore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release-identity", type=Path)
    parser.add_argument("--expected-release", default="")
    parser.add_argument("--csv-root", action="append", type=Path, default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = verify_restore(
        args.config,
        expected_release=args.expected_release,
        release_identity_path=args.release_identity,
        csv_roots=args.csv_root,
    )
    payload = {"ok": report.ok, "checks": report.checks, "errors": report.errors}
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
