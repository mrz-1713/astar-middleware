"""Display-free configuration helpers for the passive control panel."""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from eap_middleware import netinfo
from eap_middleware.control import STATUS_FILE
from eap_middleware.profiles import ProfileRegistry

Field = Tuple[str, str, Any]

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
HSMS_MODES = ("active", "passive")
RUNTIME_MODES = ("real", "simulated")
SIMULATOR_IMPLEMENTATIONS = (
    "profile", "davinci_advanced", "nexgen_advanced"
)

HTTP_FIELDS: Tuple[Field, ...] = (
    ("linkstuffs_http.enabled", "Default HTTPS enabled", "bool"),
    ("linkstuffs_http.base_url", "Default server URL (origin only)", "str"),
    ("linkstuffs_http.verify_tls", "Verify TLS certificate", "bool"),
    ("linkstuffs_http.allow_insecure", "Allow insecure test HTTP", "bool"),
    ("linkstuffs_http.timeout_sec", "Timeout (sec)", "float"),
    ("linkstuffs_http.retry_count", "Retry count", "int"),
    ("linkstuffs_http.retry_delay_sec", "Retry delay (sec)", "float"),
)

MQTT_FIELDS: Tuple[Field, ...] = (
    ("linkstuffs.enabled", "Enable MQTT upstream", "bool"),
    ("linkstuffs.host", "Broker host", "str"),
    ("linkstuffs.port", "Broker port", "int"),
    ("linkstuffs.access_token", "Gateway access token", "secret"),
    ("linkstuffs.tls", "Use TLS", "bool"),
    ("linkstuffs.allow_insecure", "Allow plaintext MQTT", "bool"),
    ("linkstuffs.qos", "QoS (0 or 1)", "int"),
    ("linkstuffs.client_id", "Client id", "str"),
    ("linkstuffs.keepalive_sec", "Keepalive (sec)", "int"),
    ("linkstuffs.publish_retain", "Publish retained", "bool"),
)

LEGACY_FIELDS: Tuple[Field, ...] = (
    ("legacy_api.enabled", "Enable legacy Tool Data API", "bool"),
    ("legacy_api.url", "Endpoint URL", "str"),
    ("legacy_api.allow_insecure", "Allow insecure test HTTP", "bool"),
    ("legacy_api.token_id", "TokenID", "secret"),
    ("legacy_api.encrypted", "Encrypt request body", "bool"),
    (
        "legacy_api.encryption_mode",
        "Encryption mode",
        ("aes_256_gcm_v2", "legacy_ctr_v1"),
    ),
    ("legacy_api.encryption_key_b64", "AES-256-GCM key (base64)", "secret"),
    ("legacy_api.first_key", "FIRSTKEY", "secret"),
    ("legacy_api.second_key", "SECONDKEY", "secret"),
    ("legacy_api.first_key_b64", "FIRSTKEY (base64)", "secret"),
    ("legacy_api.second_key_b64", "SECONDKEY (base64)", "secret"),
    ("legacy_api.send_tool_events", "ToolEvents (comma separated)", "csv"),
    ("legacy_api.timeout_sec", "Timeout (sec)", "float"),
    ("legacy_api.retry_count", "Retry count", "int"),
    ("legacy_api.retry_delay_sec", "Retry delay (sec)", "float"),
)

PATH_FIELDS: Tuple[Field, ...] = (
    ("paths.install_dir", "Install dir", "str"),
    ("paths.log_dir", "Global log dir", "str"),
    ("paths.data_dir", "Queue / journal data dir", "str"),
    ("paths.control_dir", "Status / command control dir", "str"),
    ("paths.archive_dir", "Archive dir", "str"),
    ("paths.outbox_db", "MQTT outbox db", "str"),
    ("paths.http_outbox_db", "HTTPS outbox base", "str"),
    ("paths.legacy_api_outbox_db", "Legacy API outbox db", "str"),
    ("paths.ingress_journal_db", "Ingress journal db", "str"),
)

RUNTIME_FIELDS: Tuple[Field, ...] = (
    ("logging.level", "Log level", LOG_LEVELS),
    ("logging.max_size_mb", "Log file size (MB)", "int"),
    ("logging.backup_count", "Log files kept", "int"),
    ("outbox_retention_days", "Outbox retention (days)", "int"),
    ("reconnect_interval_sec", "Reconnect interval (sec)", "float"),
    ("health_interval_sec", "Health publish interval (sec)", "float"),
    (
        "cross_generation_retransmit_window_sec",
        "Cross-generation retry window (sec)",
        "float",
    ),
    ("startup_stagger_sec", "Startup stagger (sec)", "float"),
    ("event_liveness_grace_sec", "Event liveness grace (sec)", "float"),
    ("storage_safety.enabled", "Storage safety enabled", "bool"),
    ("storage_safety.sample_interval_sec", "Storage sample interval (sec)", "float"),
    ("storage_safety.debounce_samples", "Storage debounce samples", "int"),
    ("storage_safety.warning_free_bytes", "Storage warning free bytes", "int"),
    ("storage_safety.critical_free_bytes", "Storage critical free bytes", "int"),
    ("storage_safety.recovery_free_bytes", "Storage recovery free bytes", "int"),
    ("storage_safety.warning_free_percent", "Storage warning free percent", "float"),
    ("storage_safety.critical_free_percent", "Storage critical free percent", "float"),
    ("storage_safety.recovery_free_percent", "Storage recovery free percent", "float"),
)

CONNECTION_FIELDS: Tuple[Field, ...] = (
    ("endpoint_id", "Endpoint id", "str"),
    ("display_name", "Display name", "str"),
    ("machine_profile", "Tailored profile", ()),
    ("runtime_mode", "Runtime mode", RUNTIME_MODES),
    ("offline_test_mode", "Offline test mode", "bool"),
    ("host", "Equipment host / IP", "address"),
    ("port", "Port", "int"),
    ("secs_device_id", "SECS device id", "int"),
    ("hsms_mode", "Middleware HSMS mode", HSMS_MODES),
    ("hsms_bind_address", "Passive bind address", "str"),
    ("hsms_timers", "HSMS timers (T3/T5/T6/T7/T8)", "mapping?"),
    ("enabled", "Enabled", "bool"),
    ("event_subscription_enabled", "Subscribe to events", "bool"),
    ("svid_collection_enabled", "Collect SVIDs", "bool"),
    ("enable_alarms", "Enable all alarms", "bool"),
    ("request_online", "Request ON-LINE", "bool"),
    ("drain_spool_on_connect", "Drain spool on connect", "bool"),
    ("reset_subscription_on_connect", "Reset reports on connect", "bool"),
    ("alarm_rate_limit", "Alarm rate limit", "int?"),
    ("event_subscription_path", "Event definition JSON", "str?"),
)

STORAGE_FIELDS: Tuple[Field, ...] = (
    ("storage.log_dir", "Middleware log dir", "str?"),
    ("storage.simulator_log_dir", "Simulator log dir", "str?"),
    ("storage.local_csv_path", "Local CSV dir", "str?"),
    ("storage.network_csv_path", "Network CSV mirror", "str?"),
    ("storage.admin_config_path", "Admin / SVID dir", "str?"),
)

MACHINE_HTTP_FIELDS: Tuple[Field, ...] = (
    ("linkstuffs_http.enabled", "Enable machine HTTPS", "bool"),
    ("linkstuffs_http.base_url", "Server URL (origin only, no /api/v1)", "str"),
    ("linkstuffs_http.device_token", "Device token", "secret"),
    ("linkstuffs_http.verify_tls", "Verify TLS", "bool"),
    ("linkstuffs_http.allow_insecure", "Allow insecure test HTTP", "bool"),
    ("linkstuffs_http.timeout_sec", "Timeout (sec)", "float"),
    ("linkstuffs_http.retry_count", "Retry count", "int"),
    ("linkstuffs_http.retry_delay_sec", "Retry delay (sec)", "float"),
)

SIM_FIELDS: Tuple[Field, ...] = (
    ("simulator.implementation", "Simulator implementation", SIMULATOR_IMPLEMENTATIONS),
    ("simulator.mdln", "Model identity", "str"),
    ("simulator.softrev", "Software revision", "str"),
    ("simulator.alarm_id", "Alarm ID", "int"),
    ("simulator.alarm_text", "Alarm text", "str"),
    ("simulator.wafer_count", "Wafers per lot", "int"),
    ("simulator.event_interval_sec", "Step interval (sec)", "float"),
    ("simulator.repeat_lots", "Repeat lots", "bool"),
    ("simulator.emit_alarm", "Emit alarm", "bool"),
    ("simulator.ceid_overrides", "CEID overrides", "mapping"),
    ("simulator.svid_values", "SVID values", "mapping"),
    ("simulator.event_definitions", "Event/report/DVID definitions", "mapping"),
)

# One line under the settings whose names do not carry their meaning. These
# are the ones that cost an afternoon when guessed wrong.
FIELD_HELP: Dict[str, str] = {
    "runtime_mode": (
        "real = talk to something over the network: a tool, or a simulator "
        "running on another machine. simulated = run a simulator INSIDE the "
        "middleware and ignore host/port (needs the simulator installed; an "
        "EAP-only install does not have it). For a two-machine installation, "
        "use real."
    ),
    "offline_test_mode": (
        "Run this machine with no upstream at all. Tick it to prove an HSMS "
        "link before any Linkstuffs token or route exists."
    ),
    "enabled": (
        "The service collects from this machine. This only records the "
        "choice; a service has to be running for it to mean anything."
    ),
    "hsms_mode": (
        "active = the middleware dials the equipment. passive = the "
        "middleware listens and the equipment dials in. The two ends must "
        "be opposite."
    ),
    "hsms_bind_address": (
        "Only used when this machine is passive. Leave blank to accept on "
        "every network adapter, which is almost always right."
    ),
    "host": (
        "Where the equipment is. When validating against a simulator, this is "
        "the machine running it. 'Find…' searches your networks for it."
    ),
    "secs_device_id": "Must match the device id set on the equipment.",
    "request_online": (
        "Sends S1F17 once connected, to lift a tool out of OFF-LINE. Needed "
        "on the NexGen MG - an OFF-LINE MG ignores the subscription entirely "
        "and you get a green connect with no events (MG manual 3.2).\n\n"
        "On a DaVinci, leave this off unless the tool owner agrees: if the "
        "tool's own switch is at REMOTE, coming ON-LINE puts it in Online "
        "Remote, and the DaVinci manual (9.6) says that state blocks the "
        "local operator from creating or modifying jobs and from carrier "
        "handling - cancel carrier, proceed with carrier, dock, undock."
    ),
    "drain_spool_on_connect": (
        "Asks the tool to re-send what it buffered while the host was away "
        "(S6F23). Only useful if the tool is actually spooling: a DaVinci "
        "ships with EnableSpooling off and a 20-message buffer that "
        "overwrites its oldest entries, and the NexGen MG has no spool at "
        "all. Confirm with the tool owner before relying on it."
    ),
    "reset_subscription_on_connect": (
        "Clears the tool's existing report definitions, links and event "
        "enables before subscribing (S2F37/S2F35/S2F33 with zero-length "
        "lists). Turn this on when commissioning a tool that has previously "
        "talked to a different host - its old reports and CEID links survive "
        "on the equipment, and a CEID still linked to a report this "
        "middleware then redefines delivers data against a layout the mapper "
        "no longer expects. A no-op on a tool only this middleware has used."
    ),
    "hsms_timers": (
        "Leave blank to use the values this machine's vendor manual "
        "documents. Set them only when the tool's own SECS/GEM screen has "
        "been retuned, and then match it exactly: whichever side has the "
        "shorter timer gives up first and the link drops with nothing in "
        "either log. Seconds, 1 to 120, e.g. {t3: 30, t5: 5}."
    ),
    "linkstuffs_http.base_url": (
        "Server origin only. /api/v1/<token>/telemetry is added for you."
    ),
    "storage.local_csv_path": (
        "Must be writable. The template's D: drive is often a read-only "
        "optical drive; point this somewhere writable."
    ),
}


MACHINE_FIELDS = CONNECTION_FIELDS + STORAGE_FIELDS + MACHINE_HTTP_FIELDS + SIM_FIELDS

# The machine form is forty-odd settings, and about ten of them decide whether
# a link comes up at all. Presented as one flat grid, the ten that matter are
# indistinguishable from the thirty that do not - which is what made the panel
# unusable for anyone meeting it for the first time. These groups become sub-
# tabs, "Essential" first.
_ESSENTIAL_PATHS = (
    "endpoint_id", "display_name", "machine_profile", "runtime_mode",
    "host", "port", "secs_device_id", "hsms_mode",
    "enabled", "offline_test_mode",
)


def _pick(paths: Tuple[str, ...]) -> Tuple[Field, ...]:
    by_path = {path: (path, label, kind) for path, label, kind in MACHINE_FIELDS}
    return tuple(by_path[path] for path in paths if path in by_path)


ESSENTIAL_FIELDS: Tuple[Field, ...] = _pick(_ESSENTIAL_PATHS)

# Everything from the connection block that is not essential: real settings,
# but ones with working defaults that nobody needs on first contact.
BEHAVIOUR_FIELDS: Tuple[Field, ...] = tuple(
    field for field in CONNECTION_FIELDS if field[0] not in _ESSENTIAL_PATHS
)

MACHINE_GROUPS: Tuple[Tuple[str, Tuple[Field, ...], int], ...] = (
    ("Essential", ESSENTIAL_FIELDS, 4),
    ("Behaviour", BEHAVIOUR_FIELDS, 4),
    ("Storage", STORAGE_FIELDS, 2),
    ("Telemetry", MACHINE_HTTP_FIELDS, 3),
    ("Simulator", SIM_FIELDS, 3),
)
TOKEN_FIELD: Field = ("linkstuffs_http.device_token", "Device token", "secret")


def profile_ids() -> List[str]:
    return ProfileRegistry().list_profile_ids()


def candidate_config_paths() -> List[Path]:
    """Likely installed service configs, ordered from explicit to bundled."""
    bundled = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
    )
    candidates: List[Path] = []
    configured = os.environ.get("ASTAR_EAP_CONFIG", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    program_data = os.environ.get("PROGRAMDATA", "").strip()
    if program_data:
        candidates.append(
            Path(program_data) / "ASTAR EAP" / "config" / "production.yaml"
        )
    candidates.extend(
        [
            Path("C:/SECSGEM_EAP/config/production.yaml"),
            Path(sys.executable).resolve().parent / "config" / "production.yaml",
            Path.cwd() / "config" / "production.yaml",
            bundled / "config" / "production.yaml",
        ]
    )
    return list(dict.fromkeys(candidates))


# A service that is alive rewrites runtime_status.json on its health tick.
# Two minutes is several ticks at the default 30s interval, so a file older
# than this means nothing is running rather than a slow tick.
SERVICE_STALE_AFTER_SEC = 120.0


def template_config_path() -> Optional[Path]:
    """The reviewed production.yaml template shipped beside the code.

    An install that has never been configured has no production.yaml at
    all, and a panel that opens empty gives an operator nothing to edit -
    which is why the installer used to open Notepad instead.
    """
    bundled = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
    )
    for candidate in (
        bundled / "config" / "production.yaml",
        Path.cwd() / "config" / "production.yaml",
    ):
        if candidate.is_file():
            return candidate
    return None


def seed_target_path() -> Optional[Path]:
    """Where a first run should create the config it just seeded.

    The rule is deliberately strict: the config directory itself must
    already exist. install.ps1 creates it and puts production.yaml in it, so
    after a real install it is always there and this only ever fires when
    the file was deleted.

    Anything looser writes somewhere absurd. One candidate is derived from
    sys.executable for the frozen-exe case; under a normal interpreter that
    resolves to Python's own bin directory. Another is a Windows-shaped
    absolute path, which on any other platform would fabricate a literal
    "C:/SECSGEM_EAP" tree under the working directory.
    """
    for candidate in candidate_config_paths():
        if candidate.parent.is_dir():
            return candidate
    return None


def seed_config(target: Path, template: Optional[Path] = None) -> Path:
    """Create `target` from the shipped template. Never overwrites.

    Returns the path written. Raises OSError if the template is missing or
    the location is not writable, which the panel reports rather than
    starting up with an empty form.
    """
    source = template or template_config_path()
    if source is None:
        raise OSError("no production.yaml template found beside the panel")
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@dataclasses.dataclass(frozen=True)
class ProbeTarget:
    """Just enough of a machine to open one HSMS link and ask who is there.

    Deliberately NOT a validated ServiceConfig machine. Full config
    validation also demands a complete upstream telemetry route, so an
    operator who has enabled a machine but not yet set up Linkstuffs gets
    "Missing linkstuffs_http.device_tokens" when they press Test
    connection - an answer to a question nobody asked, about a transport
    the test does not use. Proving the cable works must not depend on
    where the data will eventually be sent.
    """

    endpoint_id: str
    host: str
    port: int
    secs_device_id: int
    hsms_mode: str
    hsms_bind_address: str
    # The timers the service will actually run: profile defaults plus any
    # per-machine override. A probe that uses the library defaults while the
    # service uses the profile's documented timers would "prove" a link the
    # service itself might not hold up. None falls back to the host defaults.
    hsms_timers: Optional[Dict[str, int]] = None


def probe_target(machine: Dict[str, Any]) -> ProbeTarget:
    """Read the connection half of a machine row, validating only that.

    Raises ValueError naming the field, so the panel can say which box is
    wrong instead of showing a stack trace.
    """
    host = str(machine.get("host", "")).strip()
    if not host:
        raise ValueError("Equipment host / IP is empty.")
    try:
        port = int(str(machine.get("port", "")).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Port must be a number; got {machine.get('port')!r}.")
    if not 1 <= port <= 65535:
        raise ValueError(f"Port must be between 1 and 65535; got {port}.")
    try:
        device_id = int(str(machine.get("secs_device_id", 0) or 0).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"SECS device id must be a number; got "
            f"{machine.get('secs_device_id')!r}."
        )
    if not 0 <= device_id <= 32767:
        # Same range the config loader enforces; without it the panel would
        # test a link the service would refuse to open.
        raise ValueError(
            f"SECS device id must be between 0 and 32767; got {device_id}."
        )
    mode = str(machine.get("hsms_mode", "active")).strip() or "active"
    if mode not in HSMS_MODES:
        raise ValueError(f"HSMS mode must be one of {HSMS_MODES}; got {mode!r}.")
    return ProbeTarget(
        endpoint_id=str(machine.get("endpoint_id", "")).strip() or "machine",
        host=host,
        port=port,
        secs_device_id=device_id,
        hsms_mode=mode,
        # `or` not `get(default)`: the key is often present and null, and
        # str(None) would bind the literal address "None".
        hsms_bind_address=(
            str(machine.get("hsms_bind_address") or "0.0.0.0").strip()  # nosec B104
            # Explicit operator choice for passive HSMS mode.
            or "0.0.0.0"  # nosec B104
        ),
        hsms_timers=_probe_hsms_timers(machine),
    )


def _probe_hsms_timers(machine: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Resolve the HSMS timers a probe should use, mirroring the service's
    config loader (profile defaults layered with the machine override) so a
    panel "Test connection" proves the same link the running service would
    open rather than the library defaults."""
    profile_id = str(machine.get("machine_profile", "") or "").strip()
    profile = ProfileRegistry().get(profile_id) if profile_id else None
    timers: Dict[str, int] = dict(getattr(profile, "hsms_timers", {}) or {})
    override = machine.get("hsms_timers") or {}
    if not isinstance(override, dict):
        raise ValueError(
            f"HSMS timers must be a mapping; got {type(override).__name__}."
        )
    for key, value in override.items():
        name = str(key).strip().lower()
        try:
            timers[name] = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"HSMS timer {name!r} must be a whole number of seconds."
            )
    return timers or None


def service_is_live(data_dir: Path, now: Optional[float] = None) -> bool:
    """Is a middleware service actually running behind this panel?

    The panel talks to the service by dropping command files it picks up.
    With no service - the normal state of a fresh install - those files
    are never read, so a command silently does nothing. Knowing this lets
    the panel do the work in-process instead of appearing to hang.
    """
    status = Path(data_dir) / STATUS_FILE
    try:
        age = (now if now is not None else time.time()) - status.stat().st_mtime
    except OSError:
        return False
    return age <= SERVICE_STALE_AFTER_SEC


# What the panel says about the thing that actually collects data. The panel
# edits configuration; the service is what connects to machines. Without this
# distinction on screen, "Start" looks broken: it writes enabled: true to a
# file that nothing is reading.
SERVICE_STOPPED = "stopped"
SERVICE_LOCAL = "local"
SERVICE_EXTERNAL = "external"
SERVICE_BUSY = "busy"


def writable_path_problems(config: Any) -> List[str]:
    """Which of the service's working directories cannot be written.

    Run before starting a service in-process. Everything here fails deep
    inside a library otherwise: sqlite3 reports "unable to open database
    file" without naming the file, which is unactionable on a machine where
    the installer created these folders as Administrator and the panel runs
    as a standard user.
    """
    checks = [
        # The single-instance lock is written directly into install_dir, not
        # into any of the subfolders.
        ("the single-instance lock", Path(config.paths.install_dir)),
        ("status and command files", Path(config.paths.control_dir)),
        ("log files", Path(config.paths.log_dir)),
        ("the MQTT outbox", Path(config.paths.outbox_db).parent),
        ("the HTTPS outbox", Path(config.paths.http_outbox_db).parent),
        ("the legacy outbox", Path(config.paths.legacy_api_outbox_db).parent),
        # Easy to miss: its default is a hardcoded C:/SECSGEM_EAP path that
        # does not follow data_dir, so it fails on its own.
        ("the ingress journal", Path(config.paths.ingress_journal_db).parent),
    ]
    problems: List[str] = []
    for what, directory in checks:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".astar-write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{directory}  ({what}): {exc.strerror or exc}")
    return problems


def simulator_unavailable_machines(config: Any) -> List[str]:
    """Enabled machines set to runtime_mode 'simulated' with no simulator.

    A middleware-only install carries no simulator package on purpose, so
    such a machine fails at start with an import error - one machine at a
    time, buried in a log. Checked up front instead, because the setting
    reads as "this machine is a simulator" when it actually means "run one
    inside this process".
    """
    wanted = [
        machine.endpoint_id
        for machine in config.machines
        if machine.enabled and machine.is_simulated
    ]
    if not wanted:
        return []
    try:
        __import__("simulator.runner")
    except ImportError:
        return wanted
    return []


def service_state(
    data_dir: Path, owned: bool = False, busy: str = ""
) -> Tuple[str, str]:
    """(state, sentence) describing what is collecting data right now.

    `owned` means this panel started the service itself. `busy` is a verb
    while a start or stop is in flight.
    """
    if busy:
        return SERVICE_BUSY, f"{busy.capitalize()} the service…"
    if owned:
        return (
            SERVICE_LOCAL,
            "Service running in this window. Enabled machines are being "
            "collected. Closing this window stops it.",
        )
    if service_is_live(data_dir):
        return (
            SERVICE_EXTERNAL,
            "Service running in the background (installed as a Windows "
            "service). This panel edits its configuration.",
        )
    return (
        SERVICE_STOPPED,
        "Service not running. Nothing is collecting from any machine. "
        "Enabling a machine only records the choice until a service runs.",
    )


def discovery_choices(hosts: Sequence[Any]) -> List[str]:
    """Labelled dropdown entries for a machine's host field."""
    return [host.label for host in hosts]


def address_from_choice(text: str) -> str:
    """The bare address behind a dropdown label.

    Applied on selection and again when the form is collected, so a label
    can never reach production.yaml.
    """
    return netinfo.address_from_choice(text)


def local_address_choices() -> List[str]:
    """Addresses to offer for a machine's host field.

    A simulator on this network is reached at one of these; a real tool is
    not, so the box stays editable and these are only suggestions.
    """
    return netinfo.local_ipv4_addresses()


def format_command_result(result: Dict[str, Any]) -> str:
    endpoint = str(result.get("endpoint_id", "machine"))
    action = str(result.get("action", "command")).replace("_", " ")
    if result.get("status") != "ok":
        return f"{action} failed for {endpoint}: {result.get('error', 'unknown error')}"
    details: List[str] = []
    if "connected" in result:
        details.append("connected" if result["connected"] else "not connected")
    if result.get("identity") is not None:
        details.append(f"identity={result['identity']}")
    if result.get("http_status") is not None:
        details.append(f"HTTP {result['http_status']}")
    suffix = f": {', '.join(details)}" if details else ""
    return f"{action} completed for {endpoint}{suffix}"


def profile_label(profile_id: str, runtime: Dict[str, Any]) -> str:
    provenance = str(runtime.get("profile_provenance", ""))
    marker = " ⚠" if "DOCUMENTATION-DERIVED" in provenance.upper() else ""
    return profile_id + marker


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
    if kind == "address":
        # The dropdown shows "10.0.0.9  (listening on 5051)". Strip the note
        # here as well as on selection, so a label pasted or left in the box
        # by hand still saves as an address.
        return netinfo.address_from_choice(text)
    if not text and kind in ("int", "int?", "float", "str?"):
        return None
    if kind in ("int", "int?"):
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "csv":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind in ("mapping", "mapping?"):
        if not text:
            # "mapping?" distinguishes "explicitly empty" from "not set". An
            # empty optional mapping is dropped so the saved YAML keeps only
            # the machines that actually override something.
            return None if kind == "mapping?" else {}
        value = yaml.safe_load(text)
        if not isinstance(value, dict):
            raise ValueError("must be a YAML mapping")
        return value
    return text


def format_value(kind: Any, value: Any) -> Any:
    if kind == "bool":
        return bool(value)
    if value is None:
        return ""
    if kind == "csv":
        return ", ".join(str(item) for item in value)
    if kind in ("mapping", "mapping?"):
        if kind == "mapping?" and not value:
            return ""
        return yaml.safe_dump(value or {}, default_flow_style=True).strip()
    return str(value)


def machines_of(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    machines = config.setdefault("machines", [])
    if not isinstance(machines, list):
        raise ValueError("'machines' must be a list")
    return machines


def new_machine(existing: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    used_ids = {str(machine.get("endpoint_id", "")) for machine in existing}
    used_ports = {int(machine.get("port", 0) or 0) for machine in existing}
    index = 1
    while f"TOOL_{index:02d}" in used_ids:
        index += 1
    port = 5000
    while port in used_ports:
        port += 1
    return {
        "endpoint_id": f"TOOL_{index:02d}",
        "display_name": f"MACHINE_{index:02d}",
        "machine_profile": profile_ids()[0],
        "runtime_mode": "real",
        "offline_test_mode": False,
        "host": "127.0.0.1",
        "port": port,
        "secs_device_id": 0,
        "hsms_mode": "active",
        "enabled": False,
    }


def machine_value(config: Dict[str, Any], machine: Dict[str, Any], path: str) -> Any:
    value = get_path(machine, path)
    if value is not None:
        return value
    if path.startswith("storage."):
        return machine.get(path.split(".", 1)[1])
    if path == "linkstuffs_http.device_token":
        return device_token(config, str(machine.get("display_name", "")))
    if path.startswith("linkstuffs_http."):
        return get_path(config, path)
    defaults = {
        "runtime_mode": "real",
        "offline_test_mode": False,
        "simulator.wafer_count": 3,
        "simulator.implementation": "profile",
        "simulator.alarm_id": 0,
        "simulator.event_interval_sec": 0.5,
        "simulator.repeat_lots": True,
        "simulator.emit_alarm": True,
        "simulator.ceid_overrides": {},
        "simulator.svid_values": {},
        "simulator.event_definitions": {},
    }
    return defaults.get(path)


def device_token(config: Dict[str, Any], display_name: str) -> str:
    tokens = get_path(config, "linkstuffs_http.device_tokens", {}) or {}
    return str(tokens.get(display_name, "") or "") if isinstance(tokens, dict) else ""


def set_device_token(
    config: Dict[str, Any], display_name: str, token: str, previous_name: str = ""
) -> None:
    tokens = get_path(config, "linkstuffs_http.device_tokens")
    if not isinstance(tokens, dict):
        tokens = {}
        set_path(config, "linkstuffs_http.device_tokens", tokens)
    if previous_name and previous_name != display_name:
        tokens.pop(previous_name, None)
    tokens[display_name] = token
