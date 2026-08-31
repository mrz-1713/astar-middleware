"""Command-line interface for the packaged DaVinci simulator."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import (
    SimulatorConfig,
    SimulatorConfigError,
    load_simulator_config,
)
from .runner import EXIT_CONFIG, EXIT_UNEXPECTED, SimulatorRunner


def configure_logging(config: SimulatorConfig) -> Path:
    log_directory = config.log_directory
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "secsgem-simulator.log"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.logging.maximum_size_mb * 1024 * 1024,
        backupCount=config.logging.backup_count,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        handlers=[console, rotating],
        force=True,
    )
    # ponytail: secsgem's "communication" logger is the only place the
    # decoded SECS body of every request and reply appears. Muting it left
    # the log unable to answer "what did the host actually send?", so it
    # now follows logging.level like everything else - DEBUG additionally
    # turns on "bytestream" (raw HSMS bytes).
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="SecsGemSimulator",
        description=(
            "SECS/GEM simulator. The YAML decides which side of the link "
            "this process is: connection.role selects equipment or host, "
            "connection.mode selects HSMS passive or active. There is "
            "deliberately no command-line override for either - one file "
            "is the single source of truth for the wiring."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the simulator")
    run.add_argument("--config", required=True, help="path to simulator YAML")

    check = subparsers.add_parser(
        "check-config", help="validate YAML and exit"
    )
    check.add_argument(
        "--config", required=True, help="path to simulator YAML"
    )

    subparsers.add_parser("version", help="print version and exit")
    return parser


def _load(path: str) -> SimulatorConfig | None:
    try:
        return load_simulator_config(path)
    except SimulatorConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"SecsGemSimulator {__version__}")
        return 0

    config = _load(args.config)
    if config is None:
        return EXIT_CONFIG

    if args.command == "check-config":
        print(json.dumps({"valid": True, **config.summary()}, indent=2))
        return 0

    try:
        log_path = configure_logging(config)
    except OSError as exc:
        print(f"LOGGING ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    logging.getLogger(__name__).info("SecsGemSimulator %s", __version__)
    logging.getLogger(__name__).info("Log file: %s", log_path)
    logging.getLogger(__name__).info(
        "Configured role: %s (HSMS %s) from %s",
        config.connection.role.upper(),
        config.connection.mode.upper(),
        config.source_path,
    )
    try:
        return SimulatorRunner(config).run()
    except Exception:
        logging.getLogger(__name__).exception("Fatal application error")
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
