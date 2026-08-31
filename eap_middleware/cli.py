"""Command line entrypoint for production EAP middleware."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import ConfigError, load_service_config
from .logging_setup import configure_logging
from .profiles import ProfileRegistry
from .service import EapMiddlewareService
from .svid_admin import SvidAdminConfig


def _load_config_or_exit(path: str):
    try:
        config = load_service_config(path)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return config


def cmd_validate_config(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    print(
        json.dumps(
            {
                "valid": True,
                "enabled_machines": sum(1 for m in config.machines if m.enabled),
                "machines": [
                    {
                        "endpoint_id": m.endpoint_id,
                        "display_name": m.display_name,
                        "machine_profile": m.machine_profile,
                        "host": m.host,
                        "port": m.port,
                        "secs_device_id": m.secs_device_id,
                    }
                    for m in config.machines
                ],
            },
            indent=2,
        )
    )
    return 0


def cmd_list_profiles(args: argparse.Namespace) -> int:
    profiles = ProfileRegistry()
    items = []
    for profile_id in profiles.list_profile_ids():
        profile = profiles.get(profile_id)
        event_signatures = {
            (
                mapping.event_type,
                mapping.csv_tool_event,
                mapping.secs_raw_event,
                mapping.closes_lot_file,
            )
            for mapping in profile.event_aliases.values()
        }
        items.append(
            {
                "machine_profile": profile.profile_id,
                "vendor": profile.vendor,
                "model": profile.model,
                "default_port": profile.default_port,
                "default_secs_device_id": profile.default_secs_device_id,
                "events": len(event_signatures),
                "svids": len(profile.svids_by_name),
                "identity_svid_names": profile.identity_svid_names,
                "notes": profile.notes,
            }
        )
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    for item in items:
        print(
            f"{item['machine_profile']}: {item['vendor']} {item['model']} "
            f"(port={item['default_port']}, device_id={item['default_secs_device_id']}, "
            f"events={item['events']}, svids={item['svids']})"
        )
    return 0


def cmd_init_admin_config(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    profiles = ProfileRegistry()
    for machine in config.machines:
        profile = profiles.get(machine.machine_profile)
        SvidAdminConfig(machine.admin_dir, profile).ensure_default_files()
        print(f"ready: {machine.display_name} -> {machine.admin_dir}")
    return 0


def cmd_test_machine(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    targets = [
        machine for machine in config.machines
        if args.endpoint_id == machine.endpoint_id
        or (args.endpoint_id == "ALL" and machine.enabled)
    ]
    if not targets:
        print(f"No matching endpoint_id: {args.endpoint_id}", file=sys.stderr)
        return 2
    # The control panel runs the same probe, so the two can never disagree
    # about whether a link is up.
    from eap_middleware.probe import probe_machines

    results = probe_machines(targets, timeout=args.timeout)
    for result in results:
        print(result.as_line())
    return 1 if any(not result.ok for result in results) else 0


def cmd_test_linkstuffs(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    failures = 0
    if config.linkstuffs_http.enabled:
        context = None
        if not config.linkstuffs_http.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        for machine in config.machines:
            if not machine.enabled:
                continue
            token = config.linkstuffs_http.device_tokens.get(machine.display_name)
            if not token:
                failures += 1
                print(f"https-fail: {machine.display_name} no device token")
                continue
            url = (
                f"{config.linkstuffs_http.base_url.rstrip('/')}/api/v1/"
                f"{token}/attributes?clientKeys=endpoint_id"
            )
            request = urllib.request.Request(
                url,
                method="GET",
                headers={"User-Agent": "astar-eap-middleware/1.0"},
            )
            try:
                # URL scheme/origin was validated by load_service_config.
                with urllib.request.urlopen(  # nosec B310
                    request, timeout=args.timeout, context=context
                ) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f"HTTP {response.status}")
                print(f"https-ok: {machine.display_name}")
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                failures += 1
                print(f"https-fail: {machine.display_name} {exc}")
    if config.linkstuffs.enabled:
        try:
            with socket.create_connection(
                (config.linkstuffs.host, config.linkstuffs.port),
                timeout=args.timeout,
            ):
                print(f"mqtt-tcp-ok: {config.linkstuffs.host}:{config.linkstuffs.port}")
        except OSError as exc:
            failures += 1
            print(f"mqtt-tcp-fail: {config.linkstuffs.host}:{config.linkstuffs.port} {exc}")
    return 1 if failures else 0


def cmd_run_service(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    configure_logging(config.logging, config.paths.log_dir)
    service = EapMiddlewareService(config, config_path=args.config)
    service.run_forever()
    return 0


def cmd_outbox_requeue(args: argparse.Namespace) -> int:
    """Re-queue dead outbox rows after an operator fixed the cause (e.g.
    a Linkstuffs device token or base URL). Without --endpoint-id every
    outbox the configuration owns is processed: the MQTT outbox, the
    legacy outbox, the shared HTTPS outbox and each machine's HTTPS
    outbox file."""
    from .outbox import SQLiteOutbox

    config = _load_config_or_exit(args.config)
    from .service import machine_http_outbox_path

    if args.endpoint_id:
        machine = next(
            (m for m in config.machines if m.endpoint_id == args.endpoint_id),
            None,
        )
        if machine is None:
            print(f"Unknown endpoint_id: {args.endpoint_id}", file=sys.stderr)
            return 2
        candidates = [
            machine_http_outbox_path(Path(config.paths.http_outbox_db), machine.endpoint_id)
        ]
    else:
        candidates = [
            config.paths.outbox_db,
            config.paths.legacy_api_outbox_db,
            config.paths.http_outbox_db,
        ] + [
            machine_http_outbox_path(Path(config.paths.http_outbox_db), machine.endpoint_id)
            for machine in config.machines
        ]
    total = 0
    seen: set[str] = set()
    for db in candidates:
        db_key = str(db)
        if db_key in seen or not Path(db).exists():
            continue
        seen.add(db_key)
        try:
            outbox = SQLiteOutbox(db, retention_days=config.outbox_retention_days)
            requeued = outbox.requeue_dead()
        except Exception as exc:
            print(f"{db}: could not requeue: {exc}", file=sys.stderr)
            continue
        if requeued:
            print(f"{db}: re-queued {requeued} dead row(s)")
        total += requeued
    print(f"Total re-queued: {total}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eap-middleware")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=cmd_validate_config)

    profiles = sub.add_parser("list-profiles")
    profiles.add_argument("--json", action="store_true")
    profiles.set_defaults(func=cmd_list_profiles)

    init_admin = sub.add_parser("init-admin-config")
    init_admin.add_argument("--config", required=True)
    init_admin.set_defaults(func=cmd_init_admin_config)

    test_machine = sub.add_parser("test-machine")
    test_machine.add_argument("--config", required=True)
    test_machine.add_argument("--endpoint-id", default="ALL")
    test_machine.add_argument("--timeout", type=float, default=5.0)
    test_machine.set_defaults(func=cmd_test_machine)

    test_tb = sub.add_parser("test-linkstuffs")
    test_tb.add_argument("--config", required=True)
    test_tb.add_argument("--timeout", type=float, default=5.0)
    test_tb.set_defaults(func=cmd_test_linkstuffs)

    run = sub.add_parser("run-service")
    run.add_argument("--config", required=True)
    run.set_defaults(func=cmd_run_service)

    requeue = sub.add_parser("outbox-requeue")
    requeue.add_argument("--config", required=True)
    requeue.add_argument("--endpoint-id", default=None)
    requeue.set_defaults(func=cmd_outbox_requeue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
