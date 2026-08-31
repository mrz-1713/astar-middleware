"""Windows CI smoke test for the actual PyInstaller executable."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.host import GatewayHost, create_host_settings


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    stop_signal = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
    process.send_signal(stop_signal)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("packaged simulator did not stop after CTRL+BREAK")
    if process.returncode != 0:
        raise RuntimeError(
            f"packaged simulator exited with {process.returncode}"
        )


def run_direction(executable: Path, mode: str, temp_dir: Path) -> None:
    port = free_port()
    config = {
        "connection": {
            "mode": mode,
            "address": "127.0.0.1",
            "port": port,
            "device_id": 0,
        },
        "simulation": {
            "tool_id": f"DAV_EXE_{mode.upper()}",
            "wafer_count": 1,
            "event_interval_sec": 0.02,
            "repeat_lots": False,
            "emit_alarm": False,
        },
        "recovery": {
            "initial_retry_sec": 1,
            "maximum_retry_sec": 2,
            "maximum_restart_attempts": 2,
        },
        "logging": {
            "level": "INFO",
            "directory": str(temp_dir / f"logs-{mode}"),
            "maximum_size_mb": 2,
            "backup_count": 2,
        },
    }
    config_path = temp_dir / f"{mode}.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    output_path = temp_dir / f"{mode}-console.log"
    events = []
    connected = []
    host_mode = "passive" if mode == "active" else "active"
    settings = create_host_settings(
        host="127.0.0.1",
        port=port,
        device_id=0,
        mode=host_mode,
        bind_address="127.0.0.1",
    )
    setattr(settings.timeouts, "t3", 5)
    host = GatewayHost(
        settings=settings,
        tool_id=f"EXE_SMOKE_{mode.upper()}",
        on_event=lambda _tool, ceid, data: events.append((ceid, data)),
        on_connect=lambda _tool: connected.append(1),
    )
    process = None
    with output_path.open("w", encoding="utf-8") as output:
        try:
            if host_mode == "passive":
                host.enable()
                time.sleep(0.4)
            process = subprocess.Popen(
                [str(executable), "run", "--config", str(config_path)],
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
                text=True,
            )
            if host_mode == "active":
                time.sleep(0.6)
                host.enable()
            if not wait_for(lambda: bool(connected)):
                raise RuntimeError(
                    f"{mode}: GEM communication not established"
                )
            response = host.are_you_there()
            if response is None:
                raise RuntimeError(f"{mode}: S1F1 received no S1F2")
            identity = host.settings.streams_functions.decode(response).get()
            if identity != ["DaVinci200", "DaVinci200 Version 4.9.3"]:
                raise RuntimeError(f"{mode}: wrong identity {identity!r}")
            if not wait_for(
                lambda: any(ceid == 3050001 for ceid, _ in events)
            ):
                raise RuntimeError(
                    f"{mode}: no DaVinci MaterialReceived event"
                )
        finally:
            try:
                host.disable()
            except Exception:
                pass
            finally:
                if process is not None:
                    stop_process(process)


def run_host_role(executable: Path, temp_dir: Path) -> None:
    """Prove connection.role: host works in the packaged executable.

    The two run_direction() cases only ever exercise the equipment side.
    A host built into the same exe reaches completely different code
    (gateway.host plus the subscription manager), which PyInstaller can
    drop without anything else noticing until an operator tries it.
    """
    from simulator.equipment import create_equipment_settings
    from simulator.profile_simulator import ProfileSimulator

    port = free_port()
    config = {
        "connection": {
            "role": "host",
            "mode": "active",
            "address": "127.0.0.1",
            "port": port,
            "device_id": 0,
        },
        "simulation": {
            "profile": "davinci_200_mc4_hc1",
            "tool_id": "EXE_HOST_SMOKE",
        },
        "host": {
            "request_online": True,
            "enable_alarms": True,
            "read_identity": True,
        },
        "recovery": {
            "initial_retry_sec": 1,
            "maximum_retry_sec": 2,
            "maximum_restart_attempts": 2,
        },
        "logging": {
            "level": "INFO",
            "directory": str(temp_dir / "logs-host"),
            "maximum_size_mb": 2,
            "backup_count": 2,
        },
    }
    config_path = temp_dir / "host.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    # The peer is an equipment this time, listening for the packaged host.
    equipment = ProfileSimulator(
        settings=create_equipment_settings(
            port=port, device_id=0, address="127.0.0.1"
        ),
        profile_id="davinci_200_mc4_hc1",
        tool_id="EXE_HOST_PEER",
        wafer_count=1,
        step_interval_sec=0.05,
        loop_lots=True,
        fire_alarm=False,
    )
    log_path = temp_dir / "logs-host" / "secsgem-simulator.log"
    output_path = temp_dir / "host-console.log"
    process = None
    with output_path.open("w", encoding="utf-8") as output:
        try:
            equipment.enable()
            equipment.start_events()
            process = subprocess.Popen(
                [str(executable), "run", "--config", str(config_path)],
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
                text=True,
            )

            def received_an_event() -> bool:
                for path in (log_path, output_path):
                    try:
                        text = path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        continue
                    if "event #" in text:
                        return True
                return False

            if not wait_for(received_an_event, timeout=45.0):
                raise RuntimeError(
                    "host role: packaged executable logged no received "
                    f"event; see {output_path}"
                )
        finally:
            if process is not None:
                stop_process(process)
            try:
                equipment.disable()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument(
        "--output-dir",
        help="retain simulator logs and console captures in this directory",
    )
    args = parser.parse_args()
    executable = Path(args.exe).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        run_direction(executable, "active", output_dir)
        run_direction(executable, "passive", output_dir)
        run_host_role(executable, output_dir)
        print(
            "Packaged executable passed Active and Passive HSMS smoke tests "
            f"in both SECS roles; logs: {output_dir}"
        )
        return 0
    with tempfile.TemporaryDirectory(prefix="davinci-exe-smoke-") as temp:
        temp_dir = Path(temp)
        run_direction(executable, "active", temp_dir)
        run_direction(executable, "passive", temp_dir)
        run_host_role(executable, temp_dir)
    print(
        "Packaged executable passed Active and Passive HSMS smoke tests in "
        "both SECS roles"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
