"""Display-free helpers for the simulator control panel.

Everything here is pure data so it can be unit tested without a display.
app.py owns the widgets and nothing else.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from eap_middleware import netinfo
from eap_middleware.profiles import ProfileRegistry
from gateway.host import DEFAULT_HSMS_TIMERS
from simulator.config import (
    GEM_ROLES,
    HSMS_MODES,
    ConnectionConfig,
    HostConfig,
    RecoveryConfig,
    SimulatorConfig,
    SimulatorConfigError,
    SimulatorLoggingConfig,
    simulator_config_from_dict,
)

Field = Tuple[str, str, Any]

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# The two settings this panel exists to make unambiguous. Each option
# carries the sentence that explains it, so the GUI never shows a bare
# word like "passive" on its own.
ROLE_CHOICES: Tuple[Tuple[str, str, str], ...] = (
    (
        "equipment",
        "EQUIPMENT - simulate the tool",
        "This process answers as the machine. The peer is the host/EAP.",
    ),
    (
        "host",
        "HOST - simulate the EAP",
        "This process subscribes and collects. The peer is the equipment.",
    ),
)

MODE_CHOICES: Tuple[Tuple[str, str, str], ...] = (
    (
        "passive",
        "PASSIVE - listen for the peer",
        "This process opens the TCP port and waits to be dialled.",
    ),
    (
        "active",
        "ACTIVE - dial out to the peer",
        "This process connects to the address below; the peer listens.",
    ),
)

CONNECTION_FIELDS: Tuple[Field, ...] = (
    ("connection.port", "HSMS port", "int"),
    ("connection.device_id", "SECS device id", "int"),
    # allow_external_bind is bespoke, like role/mode/address: app.py derives
    # it from the bind address the operator already chose (see
    # requires_external_bind) instead of asking for it a second time in a
    # checkbox that lives nowhere near the address controls and can drift
    # out of sync with them.
    # The five HSMS-SS protocol timers (SEMI E37, 1-120 s). Editable because
    # this simulator stands in for the tool, and the tool is the side the
    # middleware's timers have to match: whichever side has the shorter value
    # declares a communications failure first, and the link then drops with
    # nothing in either log to explain it. Rehearsing a tool's real timers
    # here is the only way the rig can reproduce that before the tool does.
    ("connection.hsms_timers.t3", "T3 reply (sec)", "int"),
    ("connection.hsms_timers.t5", "T5 connect separation (sec)", "int"),
    ("connection.hsms_timers.t6", "T6 control transaction (sec)", "int"),
    ("connection.hsms_timers.t7", "T7 not selected (sec)", "int"),
    ("connection.hsms_timers.t8", "T8 network intercharacter (sec)", "int"),
)

# Lot generation. Only meaningful when role is equipment.
EQUIPMENT_FIELDS: Tuple[Field, ...] = (
    ("simulation.tool_id", "Tool id", "str"),
    ("simulation.wafer_count", "Wafers per lot", "int"),
    ("simulation.event_interval_sec", "Step interval (sec)", "float"),
    ("simulation.repeat_lots", "Repeat lots forever", "bool"),
    ("simulation.emit_alarm", "Emit an alarm each lot", "bool"),
    ("simulation.alarm_id", "Alarm ID", "int"),
    ("simulation.alarm_text", "Alarm text", "str"),
    ("simulation.mdln", "MDLN (model)", "str"),
    ("simulation.softrev", "SOFTREV (revision)", "str"),
)

# Opening sequence. Only meaningful when role is host.
HOST_FIELDS: Tuple[Field, ...] = (
    ("host.request_online", "Request ON-LINE (S1F17)", "bool"),
    ("host.enable_alarms", "Enable all alarms (S5F3)", "bool"),
    ("host.drain_spool", "Drain spooled messages (S6F23)", "bool"),
    ("host.read_identity", "Read identity SVs (S1F3)", "bool"),
)

LOGGING_FIELDS: Tuple[Field, ...] = (
    ("logging.level", "Log level", LOG_LEVELS),
    ("logging.directory", "Log directory", "str"),
    ("logging.maximum_size_mb", "Log file size (MB)", "int"),
    ("logging.backup_count", "Log files kept", "int"),
)

RECOVERY_FIELDS: Tuple[Field, ...] = (
    ("recovery.initial_retry_sec", "Initial retry (sec)", "int"),
    ("recovery.maximum_retry_sec", "Maximum retry (sec)", "int"),
    ("recovery.maximum_restart_attempts", "Restart limit (0 = forever)", "int"),
)


def profile_ids() -> List[str]:
    return ProfileRegistry().list_profile_ids()


def address_label(mode: str) -> str:
    """The address field means opposite things in the two HSMS modes.

    Labelling it "Address" in both is the single most common source of a
    simulator that binds to the peer's IP and never accepts a connection.
    """
    if mode == "passive":
        return "Bind address (which local NIC to listen on)"
    return "Peer address (the IP this simulator dials)"


def address_hint(mode: str) -> str:
    if mode == "passive":
        return "0.0.0.0 accepts on every interface."
    return "Must be the peer's real IP; 0.0.0.0 is rejected."


# Everything below takes the detected adapters as an argument instead of
# looking them up. Discovery calls getaddrinfo, which on a Windows host with no
# reachable DNS server blocks for seconds - and these are driven from widget
# traces that fire on every keystroke. The panel detects once, off the UI
# thread, and passes the result in.


def discovery_choices(mode: str, hosts: Sequence[Any]) -> List[str]:
    """Labelled dropdown entries for the peer address box.

    Each entry leads with the bare address so address_from_choice can
    recover it, and carries why it is being offered - "this pc" above all,
    because dialling your own address is the mistake that leaves a link
    permanently not connecting.
    """
    if mode == "passive":
        return []
    return [host.label for host in hosts]


def address_from_choice(text: str) -> str:
    """The bare address behind a dropdown label."""
    return netinfo.address_from_choice(text)


def scan_networks(interfaces: Sequence[Any]) -> List[Any]:
    return [item.network for item in interfaces if item.network is not None]


def address_choices(mode: str, addresses: Sequence[str]) -> List[str]:
    """What to offer in the address box for the selected HSMS mode.

    A passive listener may bind every adapter; an active dialler may not
    dial 0.0.0.0. Offering the same list for both is what produces a
    simulator bound to an adapter the peer machine cannot see.
    """
    if mode == "passive":
        return [netinfo.ALL_INTERFACES, *addresses]
    return list(addresses)


def peer_target(mode: str, address: str, port: Any, primary: str = "") -> str:
    """The address:port to enter on the *other* machine.

    Only meaningful when this process listens. 0.0.0.0 is a correct bind
    and a useless destination, so it resolves to `primary` - the address a
    peer on the LAN would actually reach this machine on.
    """
    if mode != "passive":
        return ""
    chosen = str(address or "").strip()
    resolved = chosen if chosen and chosen != netinfo.ALL_INTERFACES else primary
    if not resolved or resolved == netinfo.ALL_INTERFACES:
        return ""
    return f"{resolved}:{_as_int(port, 0)}"


def binds_every_adapter(address: str) -> bool:
    """Is this the default, unrestricted bind?

    Kept here so the panel and its tests agree on what "no restriction"
    looks like in a saved file.
    """
    chosen = str(address or "").strip()
    return not chosen or chosen == netinfo.ALL_INTERFACES


def requires_external_bind(mode: str, address: str) -> bool:
    """Does listening at this address need simulator.config's external-bind
    consent (connection.allow_external_bind)?

    Passive mode hands `address` straight to socket.bind, so anything other
    than this machine's own loopback address can be reached from other
    hosts on the network. simulator.config makes that an explicit opt-in;
    this mirrors its loopback check so app.py can set the flag as a side
    effect of the bind address the operator already chose on the Link tab,
    rather than through a second, disconnected checkbox that could say
    something different from what the address controls actually do.
    """
    if mode != "passive":
        return False
    candidate = str(address or "").strip()
    if not candidate or candidate == netinfo.ALL_INTERFACES:
        return True
    try:
        is_loopback = ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        is_loopback = candidate.lower() == "localhost"
    return not is_loopback


def listening_summary(mode: str, address: str, addresses: Sequence[str]) -> str:
    """What a passive listener is actually accepting on.

    The bind address is a restriction, not a destination: in passive mode
    it is handed straight to socket.bind, so 0.0.0.0 already accepts on
    every adapter. Presenting it as a required choice is what leads an
    operator to pin one NIC on a multi-homed machine and become unreachable.
    """
    if mode != "passive":
        return ""
    external = (
        " This machine will accept connections from other machines on "
        "the network."
    )
    if binds_every_adapter(address):
        count = len(addresses)
        if not count:
            return "Accepting on every adapter on this machine." + external
        return (
            f"Accepting on every adapter on this machine "
            f"({count} found: {', '.join(addresses)})." + external
        )
    refused = (
        " Connections arriving on any other adapter of this machine will "
        "be refused."
    )
    note = external if requires_external_bind(mode, address) else ""
    return f"Restricted to {address.strip()} only.{refused}{note}"


def firewall_command(port: Any) -> str:
    """PowerShell that opens this simulator's inbound port."""
    return netinfo.firewall_command(_as_int(port, 0))


def firewall_argv(port: Any) -> Sequence[str]:
    """The elevated command line the panel's firewall button runs."""
    return netinfo.elevated_powershell_argv(firewall_command(port))


# secsgem reports its link as an enum name. Showing NOT_COMMUNICATING raw
# tells an operator nothing about whether that is normal (nobody has dialled
# in yet) or wrong (the peer is refusing), which is the whole question while
# a link is being brought up.
LINK_STATE_SENTENCES = {
    "NOT_COMMUNICATING": "waiting: no peer has completed the HSMS handshake",
    "NOT_SELECTED": "TCP connected, waiting for HSMS Select",
    "WAIT_CRA": "HSMS selected, waiting for the peer to establish communication",
    "WAIT_DELAY": "handshake failed, backing off before retrying",
    "COMMUNICATING": "CONNECTED: the peer is communicating",
}


def link_state_sentence(state: str, mode: str) -> str:
    """Plain language for a raw secsgem communication state."""
    described = LINK_STATE_SENTENCES.get(state)
    if described is None:
        return f"link state: {state}"
    if state == "NOT_COMMUNICATING":
        described += (
            " (normal until the middleware connects)"
            if mode == "passive"
            else " (still dialling the peer)"
        )
    return described


def wiring_lines(
    role: str, mode: str, address: str, port: Any, device_id: Any
) -> Tuple[str, str]:
    """The two sentences shown live under the role/mode selectors."""
    connection = ConnectionConfig(
        role=role if role in GEM_ROLES else "equipment",
        mode=mode if mode in HSMS_MODES else "passive",
        address=str(address or ""),
        port=_as_int(port, 0),
        device_id=_as_int(device_id, 0),
    )
    return connection.describe_self(), connection.describe_peer()


def peer_middleware_hint(role: str, mode: str) -> str:
    """What to type into the middleware's own config for this wiring.

    The peer is usually production.yaml, whose setting is named from the
    middleware's point of view - so spell it out rather than making the
    operator invert the role themselves.
    """
    if role == "host":
        return (
            "The peer here is equipment, not the middleware. Point this "
            "simulator at the tool's IP/port; the middleware is not "
            "involved in this link."
        )
    peer_mode = "active" if mode == "passive" else "passive"
    return (
        f"In the middleware's production.yaml set this machine to "
        f"hsms_mode: {peer_mode}"
        + (
            " and host: <this machine's IP>."
            if peer_mode == "active"
            else " (it will listen; this simulator dials it)."
        )
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def default_config() -> Dict[str, Any]:
    """A working equipment simulator, the role operators want most.

    The non-connection sections come straight from the loader's own
    dataclass defaults: a blank form must produce a file the packaged
    executable accepts, and hand-copied numbers here drifted from the
    loader's allowed ranges.
    """
    return {
        "connection": {
            "role": "equipment",
            "mode": "passive",
            "address": "127.0.0.1",
            "allow_external_bind": False,
            "port": 5051,
            "device_id": 0,
            # Spelled out rather than left absent so the panel's timer fields
            # are never blank: parse_value("int", "") raises, and "leave it
            # empty for the default" would surface as a validation error at
            # save time. Written explicitly into every saved file, which is
            # what you want on a rig whose job is diagnosing timer mismatches.
            "hsms_timers": dict(DEFAULT_HSMS_TIMERS),
        },
        "simulation": {
            "profile": "davinci_200_mc4_hc1",
            "tool_id": "SIM_01",
            "wafer_count": 3,
            "event_interval_sec": 0.5,
            "repeat_lots": True,
            "emit_alarm": True,
        },
        "host": dataclasses.asdict(HostConfig()),
        "recovery": dataclasses.asdict(RecoveryConfig()),
        "logging": dataclasses.asdict(SimulatorLoggingConfig()),
    }


def candidate_config_paths() -> List[Path]:
    """Where a packaged simulator is likely to keep its YAML."""
    bundled = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
    )
    candidates: List[Path] = []
    configured = os.environ.get("ASTAR_SIMULATOR_CONFIG", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path(sys.executable).resolve().parent / "simulator.yaml",
            Path.cwd() / "simulator.yaml",
            bundled / "simulator.yaml",
        ]
    )
    return list(dict.fromkeys(candidates))


def get_path(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    if value is None:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value


def parse_value(kind: Any, raw: Any) -> Any:
    if kind == "bool":
        return bool(raw)
    text = "" if raw is None else str(raw).strip()
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


def format_value(kind: Any, value: Any) -> Any:
    if kind == "bool":
        return bool(value)
    if value is None:
        return ""
    return str(value)


def strip_inapplicable(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the section that does not belong to the selected role.

    The loader rejects a host: block on an equipment, and a saved file
    that carries both would leave the operator guessing which half is
    live.
    """
    result = dict(data)
    if get_path(result, "connection.role") != "host":
        result.pop("host", None)
    return result


def validate(
    data: Dict[str, Any], source_path: Optional[Path] = None
) -> SimulatorConfig:
    """Raise SimulatorConfigError unless the packaged exe would load this."""
    return simulator_config_from_dict(
        strip_inapplicable(data), source_path or Path("simulator.yaml")
    )


def load_yaml(path: Path) -> Dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise SimulatorConfigError("top-level configuration must be a mapping")
    return loaded


def dump_yaml(data: Dict[str, Any]) -> str:
    return yaml.safe_dump(
        strip_inapplicable(data), sort_keys=False, default_flow_style=False
    )


def save_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write atomically so a half-written file never becomes the config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dump_yaml(data), encoding="utf-8")
    os.replace(temporary, path)
