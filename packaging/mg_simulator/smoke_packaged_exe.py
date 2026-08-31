"""Windows CI smoke test for the actual PyInstaller MG executable.

Mirrors packaging/secsgem_simulator/smoke_packaged_exe.py. The MG simulator is
driven by command-line flags rather than a YAML config, so there is no config
file to write - otherwise the shape is identical: start the packaged exe in one
HSMS role, connect a real GatewayHost in the opposite role, and require both a
correct S1F2 identity and a real MG collection event before declaring success.
"""

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.host import GatewayHost, create_host_settings

# port1CasPlaced - the first event of every MG lot, and one whose CEID alone
# identifies the load port.
MG_FIRST_LOT_CEID = 130


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


def stop_process(process: "subprocess.Popen[str]") -> None:
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


def run_direction(executable: Path, mode: str, temp_dir: Path) -> None:
    """Run the packaged exe in `mode`, with the host taking the other role."""
    port = free_port()
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
        tool_id=f"MG_EXE_SMOKE_{mode.upper()}",
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
                [
                    str(executable),
                    "--hsms-mode", mode,
                    "--host", "127.0.0.1",
                    "--port", str(port),
                    "--wafers", "1",
                    "--interval", "0.02",
                    "--no-alarm",
                    "--tool-id", f"MG_EXE_{mode.upper()}",
                ],
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
                raise RuntimeError(f"{mode}: GEM communication not established")
            response = host.are_you_there()
            if response is None:
                raise RuntimeError(f"{mode}: S1F1 received no S1F2")
            identity = host.settings.streams_functions.decode(response).get()
            # The identity the manual itself prints in its lot-start capture
            # (section 9.1.1.1): <A[4] 'MG22'>, <A[7] '3.7.0.0'>.
            if identity != ["MG22", "3.7.0.0"]:
                raise RuntimeError(f"{mode}: wrong identity {identity!r}")
            if not wait_for(
                lambda: any(ceid == MG_FIRST_LOT_CEID for ceid, _ in events)
            ):
                raise RuntimeError(f"{mode}: no MG port1CasPlaced event")
        finally:
            try:
                host.disable()
            except Exception:
                pass
            finally:
                if process is not None:
                    stop_process(process)


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
        print(
            "Packaged MG executable passed Active and Passive HSMS smoke "
            f"tests; logs: {output_dir}"
        )
        return 0
    with tempfile.TemporaryDirectory(prefix="mg-exe-smoke-") as temp:
        temp_dir = Path(temp)
        run_direction(executable, "active", temp_dir)
        run_direction(executable, "passive", temp_dir)
    print("Packaged MG executable passed Active and Passive HSMS smoke tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
