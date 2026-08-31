"""Strict configuration model for the packaged DaVinci simulator."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import yaml

from eap_middleware.profiles import ProfileRegistry
from .profile_simulator import LOT_END_FLOW, LOT_FLOW, WAFER_FLOW

logger = logging.getLogger(__name__)


class SimulatorConfigError(ValueError):
    """Raised when a simulator configuration file is invalid."""


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Two independent axes that operators kept conflating, so they are two
# separate settings and never derived from one another:
#   role -> who this process pretends to be in SECS/GEM terms
#   mode -> who opens the TCP connection at the HSMS layer
# Every combination is legal: an equipment may listen or dial out, and so
# may a host.
GEM_ROLES = ("equipment", "host")
HSMS_MODES = ("passive", "active")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SimulatorConfigError(f"{path} must be a mapping")
    return value


def _reject_unknown(
    data: Mapping[str, Any], allowed: set[str], path: str
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SimulatorConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}"
        )


def _string(data: Mapping[str, Any], key: str, default: str, path: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise SimulatorConfigError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _integer(
    data: Mapping[str, Any],
    key: str,
    default: int,
    path: str,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimulatorConfigError(f"{path}.{key} must be an integer")
    if not minimum <= value <= maximum:
        raise SimulatorConfigError(
            f"{path}.{key} must be between {minimum} and {maximum}"
        )
    return value


def _number(
    data: Mapping[str, Any],
    key: str,
    default: float,
    path: str,
    minimum: float,
    maximum: float,
) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulatorConfigError(f"{path}.{key} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise SimulatorConfigError(
            f"{path}.{key} must be between {minimum:g} and {maximum:g}"
        )
    return result


def _int_map(
    data: Mapping[str, Any],
    key: str,
    path: str,
    minimum: int,
    maximum: int,
    allowed: Optional[Sequence[str]] = None,
) -> Dict[str, int]:
    """A {name: integer} table, e.g. simulation.ceid_overrides."""
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise SimulatorConfigError(f"{path}.{key} must be a mapping")
    result: Dict[str, int] = {}
    for name, raw in value.items():
        if allowed is not None and str(name) not in allowed:
            # A typo here is otherwise silent: the simulator keeps the entry
            # and simply never sends it.
            raise SimulatorConfigError(
                f"{path}.{key}.{name} is not a lifecycle step; expected one "
                f"of: {', '.join(allowed)}"
            )
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SimulatorConfigError(f"{path}.{key}.{name} must be an integer")
        if not minimum <= raw <= maximum:
            raise SimulatorConfigError(
                f"{path}.{key}.{name} must be between {minimum} and {maximum}"
            )
        result[str(name)] = raw
    return result


SECS_TYPE_NAMES = frozenset({
    "A", "ASCII", "STRING", "BOOLEAN",
    "U1", "U2", "U4", "U8", "I1", "I2", "I4", "I8", "F4", "F8",
})


def _type_map(value: Any, path: str) -> Dict[int, str]:
    """An {SVID: SECS type name} table, e.g. simulation.svid_types."""
    result: Dict[int, str] = {}
    for key, raw in _mapping(value, path).items():
        try:
            svid = int(key)
        except (TypeError, ValueError):
            raise SimulatorConfigError(f"{path}.{key} must be a numeric SVID")
        name = str(raw).upper()
        if name not in SECS_TYPE_NAMES:
            raise SimulatorConfigError(
                f"{path}.{key} type {raw!r} is not a SECS type; expected one "
                f"of: {', '.join(sorted(SECS_TYPE_NAMES))}"
            )
        result[svid] = name
    return result


def _scalar_map(
    data: Mapping[str, Any], key: str, path: str
) -> Dict[int, Any]:
    """An {integer id: scalar} table, e.g. simulation.svid_values."""
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise SimulatorConfigError(f"{path}.{key} must be a mapping")
    result: Dict[int, Any] = {}
    for name, raw in value.items():
        try:
            svid = int(name)
        except (TypeError, ValueError):
            raise SimulatorConfigError(
                f"{path}.{key} keys must be integer ids, got {name!r}"
            ) from None
        if not isinstance(raw, (str, int, float, bool)):
            raise SimulatorConfigError(
                f"{path}.{key}.{name} must be a string, number or boolean"
            )
        result[svid] = raw
    return result


def _boolean(
    data: Mapping[str, Any], key: str, default: bool, path: str
) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SimulatorConfigError(f"{path}.{key} must be true or false")
    return value


def _validate_address(
    address: str,
    mode: str,
    allow_external_bind: bool,
) -> None:
    if len(address) > 253 or any(char.isspace() for char in address):
        raise SimulatorConfigError(
            "connection.address is not a valid IPv4 address or hostname"
        )
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        if not _HOSTNAME_RE.fullmatch(address):
            raise SimulatorConfigError(
                "connection.address is not a valid IPv4 address or hostname"
            )
    else:
        if parsed.version != 4:
            raise SimulatorConfigError(
                "connection.address must be IPv4 because secsgem 0.3.0 uses IPv4 sockets"
            )
    if mode == "active" and address == "0.0.0.0":  # nosec B104
        raise SimulatorConfigError(
            "connection.address cannot be 0.0.0.0 in active mode; use the middleware IP"
        )
    if mode == "passive" and not allow_external_bind:
        try:
            is_loopback = ipaddress.ip_address(address).is_loopback
        except ValueError:
            is_loopback = address.lower() == "localhost"
        if not is_loopback:
            raise SimulatorConfigError(
                "connection.allow_external_bind must be true before a passive "
                "simulator listens beyond loopback; use 127.0.0.1 for local tests"
            )
    elif mode == "passive" and allow_external_bind:
        try:
            is_loopback = ipaddress.ip_address(address).is_loopback
        except ValueError:
            is_loopback = address.lower() == "localhost"
        if not is_loopback:
            logger.warning(
                "LAB EXTERNAL BIND override: simulator will listen on %s; "
                "restrict the network segment and inbound firewall rule.",
                address,
            )


@dataclass(frozen=True)
class ConnectionConfig:
    """HSMS transport settings for the simulated equipment."""

    mode: str
    address: str
    port: int
    device_id: int
    # Defaulted so every existing caller that only passes the transport
    # settings keeps the historical behaviour: this is an equipment.
    role: str = "equipment"
    allow_external_bind: bool = False
    # HSMS-SS protocol timers (SEMI E37, seconds). Empty means "use the
    # shipped defaults", which is what every caller got before this existed.
    #
    # This has to be settable because the simulator stands in for the tool in
    # the two-VM rig, and the tool is the side the middleware's timers must
    # match. gateway.host explains the failure mode: whichever side has the
    # shorter timer declares a communications failure while the other still
    # considers the transaction open - an intermittent link drop with nothing
    # in either log to point at. With the simulator pinned to the library
    # defaults, that is the one class of fault the rig could never reproduce.
    hsms_timers: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    @property
    def is_listener(self) -> bool:
        return self.mode == "passive"

    @property
    def peer_role(self) -> str:
        return "equipment" if self.is_host else "host"

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"

    def describe_self(self) -> str:
        """One sentence: what this process is and which side it dials."""
        action = (
            f"listens on {self.endpoint}"
            if self.is_listener
            else f"dials out to {self.endpoint}"
        )
        return (
            f"This simulator acts as the {self.role.upper()} and "
            f"{action} (device id {self.device_id})."
        )

    def describe_peer(self) -> str:
        """One sentence: how the machine at the other end must be set."""
        peer_mode = "active" if self.is_listener else "passive"
        peer_action = (
            f"connect to this machine on port {self.port}"
            if self.is_listener
            else f"listen on port {self.port}"
        )
        return (
            f"The peer must therefore be the {self.peer_role.upper()} in "
            f"HSMS {peer_mode.upper()} mode and {peer_action}, "
            f"device id {self.device_id}."
        )


@dataclass(frozen=True)
class SimulationConfig:
    """What the simulator produces: lots, wafers, and event pacing."""

    tool_id: str = "DAV_SIM_01"
    wafer_count: int = 3
    event_interval_sec: float = 0.5
    repeat_lots: bool = True
    emit_alarm: bool = True
    alarm_id: int = 0
    alarm_text: str = ""
    mdln: str = ""
    softrev: str = ""
    subscription_path: Optional[str] = None
    # Which machine this simulator pretends to be. Any id from the middleware's
    # ProfileRegistry; the simulator then uses that vendor's own CEIDs and SVIDs.
    profile: str = "davinci_200_mc4_hc1"
    # canonical event_type -> CEID, for a tool that renumbered its events (or a
    # profile that documents none). Same keys the profile resolves to:
    # mounted, loaded, clamped, lot_start, wafer_start, process_start,
    # process_end, wafer_end, lot_end, unloaded, unmounted.
    ceid_overrides: Mapping[str, int] = field(default_factory=dict)
    # SVID -> value returned by S1F3, for tools whose identity SVs matter.
    svid_values: Mapping[int, Any] = field(default_factory=dict)
    svid_types: Mapping[int, str] = field(default_factory=dict)
    dvid_values: Mapping[str, Any] = field(default_factory=dict)
    dvid_types: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HostConfig:
    """Opening sequence used when connection.role is 'host'.

    Mirrors the per-machine switches the production middleware exposes, so
    a link proven with this simulator behaves the same once the real
    middleware replaces it.
    """

    # S1F17. A tool left OFF-LINE ignores subscriptions and sends nothing,
    # which is the single most common "connected but no data" report - so
    # the test host asks by default.
    request_online: bool = True
    # S5F3 with a zero-length ALID list: enable reporting for every alarm.
    enable_alarms: bool = True
    # S6F23: recover whatever the tool spooled while no host was attached.
    drain_spool: bool = False
    # S1F3 read-back of the profile's identity SVs once communicating.
    read_identity: bool = True


@dataclass(frozen=True)
class RecoveryConfig:
    """Reconnect and restart backoff for the simulator."""

    initial_retry_sec: int = 1
    maximum_retry_sec: int = 30
    maximum_restart_attempts: int = 0


@dataclass(frozen=True)
class SimulatorLoggingConfig:
    """Log level and rotation policy for the simulator."""

    level: str = "INFO"
    directory: str = "logs"
    maximum_size_mb: int = 10
    backup_count: int = 5


@dataclass(frozen=True)
class SimulatorConfig:
    """A fully validated simulator configuration."""

    connection: ConnectionConfig
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    host: HostConfig = field(default_factory=HostConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    logging: SimulatorLoggingConfig = field(
        default_factory=SimulatorLoggingConfig
    )
    source_path: Path = field(
        default=Path("davinci-simulator.yaml"), repr=False, compare=False
    )

    @property
    def log_directory(self) -> Path:
        configured = Path(self.logging.directory)
        if configured.is_absolute():
            return configured
        return self.source_path.parent / configured

    def summary(self) -> dict[str, Any]:
        endpoint_kind = (
            "remote" if self.connection.mode == "active" else "listener"
        )
        summary = {
            "gem_role": self.connection.role,
            "hsms_mode": self.connection.mode,
            endpoint_kind: self.connection.endpoint,
            "device_id": self.connection.device_id,
            "profile": self.simulation.profile,
            "tool_id": self.simulation.tool_id,
            "log_directory": str(self.log_directory),
            "this_side": self.connection.describe_self(),
            "other_side": self.connection.describe_peer(),
        }
        if self.connection.is_host:
            summary.update(
                {
                    "request_online": self.host.request_online,
                    "enable_alarms": self.host.enable_alarms,
                    "drain_spool": self.host.drain_spool,
                }
            )
        else:
            # Lot generation only exists on the equipment side; reporting it
            # for a host would suggest the host drives wafers, which it does
            # not - it only subscribes and receives.
            summary.update(
                {
                    "wafer_count": self.simulation.wafer_count,
                    "repeat_lots": self.simulation.repeat_lots,
                    "emit_alarm": self.simulation.emit_alarm,
                }
            )
        return summary


def load_simulator_config(path: str | Path) -> SimulatorConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SimulatorConfigError(f"configuration file not found: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SimulatorConfigError(
            f"could not read YAML configuration: {exc}"
        ) from exc
    return simulator_config_from_dict(raw, source)


def _simulator_hsms_timers(connection_data: Mapping[str, Any]) -> Dict[str, int]:
    """Parse `connection.hsms_timers`, validated the same way the middleware
    validates its own. Absent means "shipped defaults"."""
    from gateway.host import DEFAULT_HSMS_TIMERS, HSMS_TIMER_MAX, HSMS_TIMER_MIN

    raw = connection_data.get("hsms_timers")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SimulatorConfigError(
            "connection.hsms_timers must be a mapping of timer name to "
            f"seconds, e.g. {{t3: 30, t5: 5, t6: 10, t7: 5, t8: 6}}; got "
            f"{type(raw).__name__}"
        )
    timers: Dict[str, int] = {}
    for name, value in raw.items():
        key = str(name).strip().lower()
        if key not in DEFAULT_HSMS_TIMERS:
            raise SimulatorConfigError(
                f"connection.hsms_timers.{name} is not an HSMS timer; "
                f"expected one of {sorted(DEFAULT_HSMS_TIMERS)}"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise SimulatorConfigError(
                f"connection.hsms_timers.{key} must be a whole number of "
                f"seconds; got {value!r}"
            )
        if not HSMS_TIMER_MIN <= value <= HSMS_TIMER_MAX:
            raise SimulatorConfigError(
                f"connection.hsms_timers.{key} must be between "
                f"{HSMS_TIMER_MIN} and {HSMS_TIMER_MAX} seconds; got {value}"
            )
        timers[key] = int(value)
    return timers


def simulator_config_from_dict(
    raw: Any, source_path: str | Path = Path("simulator.yaml")
) -> SimulatorConfig:
    """Validate an already-parsed configuration mapping.

    Split out of load_simulator_config so the GUI validates exactly what
    the packaged executable will later load, without having to write the
    file first.
    """
    source = Path(source_path)
    top = _mapping(raw, "configuration")
    _reject_unknown(
        top,
        {"connection", "simulation", "host", "recovery", "logging"},
        "configuration",
    )
    if "connection" not in top:
        raise SimulatorConfigError("configuration.connection is required")

    connection_data = _mapping(top["connection"], "connection")
    _reject_unknown(
        connection_data,
        {
            "role",
            "mode",
            "address",
            "port",
            "device_id",
            "allow_external_bind",
            "hsms_timers",
        },
        "connection",
    )
    # Omitting role keeps older configuration files working: before this
    # setting existed every packaged simulator was an equipment.
    role = _string(
        connection_data, "role", "equipment", "connection"
    ).lower()
    if role not in GEM_ROLES:
        raise SimulatorConfigError(
            "connection.role must be 'equipment' (simulate the tool) or "
            "'host' (simulate the EAP/host); it is the SECS/GEM role and is "
            "independent of connection.mode"
        )
    mode = _string(connection_data, "mode", "", "connection").lower()
    if mode not in HSMS_MODES:
        raise SimulatorConfigError(
            "connection.mode must be 'active' (dial out) or 'passive' "
            "(listen); it is the HSMS transport and is independent of "
            "connection.role"
        )
    address = _string(connection_data, "address", "", "connection")
    allow_external_bind = _boolean(
        connection_data,
        "allow_external_bind",
        False,
        "connection",
    )
    _validate_address(address, mode, allow_external_bind)
    connection = ConnectionConfig(
        role=role,
        mode=mode,
        address=address,
        port=_integer(connection_data, "port", 5050, "connection", 1, 65535),
        device_id=_integer(
            connection_data, "device_id", 0, "connection", 0, 32767
        ),
        allow_external_bind=allow_external_bind,
        hsms_timers=_simulator_hsms_timers(connection_data),
    )

    simulation_data = _mapping(top.get("simulation", {}), "simulation")
    _reject_unknown(
        simulation_data,
        {
            "tool_id",
            "wafer_count",
            "event_interval_sec",
            "repeat_lots",
            "emit_alarm",
            "alarm_id",
            "alarm_text",
            "mdln",
            "softrev",
            "subscription_path",
            "profile",
            "ceid_overrides",
            "svid_values",
            "svid_types",
            "dvid_values",
            "dvid_types",
        },
        "simulation",
    )
    profile = _string(
        simulation_data, "profile", "davinci_200_mc4_hc1", "simulation"
    )
    known_profiles = ProfileRegistry().list_profile_ids()
    if profile not in known_profiles:
        raise SimulatorConfigError(
            f"simulation.profile must be one of: {', '.join(known_profiles)}"
        )
    simulation = SimulationConfig(
        profile=profile,
        ceid_overrides=_int_map(
            simulation_data,
            "ceid_overrides",
            "simulation",
            1,
            2**31 - 1,
            allowed=LOT_FLOW + WAFER_FLOW + LOT_END_FLOW,
        ),
        svid_values=_scalar_map(simulation_data, "svid_values", "simulation"),
        svid_types=_type_map(
            simulation_data.get("svid_types", {}), "simulation.svid_types"
        ),
        tool_id=_string(
            simulation_data, "tool_id", "DAV_SIM_01", "simulation"
        ),
        wafer_count=_integer(
            simulation_data, "wafer_count", 3, "simulation", 1, 300
        ),
        event_interval_sec=_number(
            simulation_data,
            "event_interval_sec",
            0.5,
            "simulation",
            0.01,
            3600.0,
        ),
        repeat_lots=_boolean(
            simulation_data, "repeat_lots", True, "simulation"
        ),
        emit_alarm=_boolean(simulation_data, "emit_alarm", True, "simulation"),
        alarm_id=_integer(
            simulation_data, "alarm_id", 0, "simulation", 0, 2**31 - 1
        ),
        alarm_text=str(simulation_data.get("alarm_text", "")),
        mdln=str(simulation_data.get("mdln", "")),
        softrev=str(simulation_data.get("softrev", "")),
        subscription_path=(
            str(simulation_data["subscription_path"])
            if simulation_data.get("subscription_path")
            else None
        ),
        dvid_values={
            str(key): value
            for key, value in _mapping(
                simulation_data.get("dvid_values", {}),
                "simulation.dvid_values",
            ).items()
        },
        dvid_types={
            str(key): str(value).upper()
            for key, value in _mapping(
                simulation_data.get("dvid_types", {}),
                "simulation.dvid_types",
            ).items()
        },
    )

    host_data = _mapping(top.get("host", {}), "host")
    _reject_unknown(
        host_data,
        {"request_online", "enable_alarms", "drain_spool", "read_identity"},
        "host",
    )
    if host_data and not connection.is_host:
        # Silently ignoring these would leave an operator convinced the
        # simulator is doing an opening sequence it never runs.
        raise SimulatorConfigError(
            "configuration.host only applies when connection.role is "
            "'host'; remove the section or set connection.role: host"
        )
    host_config = HostConfig(
        request_online=_boolean(host_data, "request_online", True, "host"),
        enable_alarms=_boolean(host_data, "enable_alarms", True, "host"),
        drain_spool=_boolean(host_data, "drain_spool", False, "host"),
        read_identity=_boolean(host_data, "read_identity", True, "host"),
    )

    recovery_data = _mapping(top.get("recovery", {}), "recovery")
    _reject_unknown(
        recovery_data,
        {"initial_retry_sec", "maximum_retry_sec", "maximum_restart_attempts"},
        "recovery",
    )
    recovery = RecoveryConfig(
        initial_retry_sec=_integer(
            recovery_data, "initial_retry_sec", 1, "recovery", 1, 3600
        ),
        maximum_retry_sec=_integer(
            recovery_data, "maximum_retry_sec", 30, "recovery", 1, 3600
        ),
        maximum_restart_attempts=_integer(
            recovery_data,
            "maximum_restart_attempts",
            0,
            "recovery",
            0,
            1_000_000,
        ),
    )
    if recovery.maximum_retry_sec < recovery.initial_retry_sec:
        raise SimulatorConfigError(
            "recovery.maximum_retry_sec must be greater than or equal to initial_retry_sec"
        )

    logging_data = _mapping(top.get("logging", {}), "logging")
    _reject_unknown(
        logging_data,
        {"level", "directory", "maximum_size_mb", "backup_count"},
        "logging",
    )
    level = _string(logging_data, "level", "INFO", "logging").upper()
    if level not in _LOG_LEVELS:
        raise SimulatorConfigError(
            f"logging.level must be one of: {', '.join(sorted(_LOG_LEVELS))}"
        )
    logging_config = SimulatorLoggingConfig(
        level=level,
        directory=_string(logging_data, "directory", "logs", "logging"),
        maximum_size_mb=_integer(
            logging_data, "maximum_size_mb", 10, "logging", 1, 1024
        ),
        backup_count=_integer(
            logging_data, "backup_count", 5, "logging", 1, 100
        ),
    )

    return SimulatorConfig(
        connection=connection,
        simulation=simulation,
        host=host_config,
        recovery=recovery,
        logging=logging_config,
        source_path=source,
    )
