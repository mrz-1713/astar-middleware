"""Configuration loading and validation for production middleware."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from .models import (
    LegacyApiConfig,
    LinkstuffsHttpConfig,
    LoggingConfig,
    MachineConfig,
    MachineLinkstuffsHttpConfig,
    MachineSimulatorConfig,
    MachineStorageConfig,
    MiddlewarePaths,
    ServiceConfig,
    StorageSafetyConfig,
    LinkstuffsConfig,
)
from .profiles import ProfileRegistry, profile_with_subscription_file
from .secure_payload import (
    AES_256_GCM_V2,
    LEGACY_CTR_V1,
    SUPPORTED_ENCRYPTION_MODES,
    SecurePayloadCodec,
    SecurePayloadError,
)

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when middleware configuration is invalid."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|%([A-Za-z_][A-Za-z0-9_]*)%")
_SIMULATOR_EVENT_TYPES = {
    "mounted", "loaded", "clamped", "lot_start", "wafer_start",
    "process_start", "process_end", "wafer_end", "lot_end", "unloaded",
    "unmounted",
}


def _expand_env_value(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            if name not in os.environ:
                raise ConfigError(
                    f"Environment variable '{name}' referenced in config is not set"
                )
            return os.environ[name]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_value(item) for key, item in value.items()}
    return value


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"machines[] must include non-empty '{key}'")
    return value.strip()


_FILENAME_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def _require_filename_safe(data: Mapping[str, Any], key: str) -> str:
    """A non-empty string that is also safe to put in a path.

    display_name is not just a label: it becomes part of every per-lot CSV
    filename, and of the default log, data and admin directory names. Only the
    load-port segment of the filename was ever sanitised, so a name containing
    a character Windows forbids in a filename - ':' in "TOOL:1" is the easy one
    to type - made every CSV write for that machine raise. The rows survived in
    the journal, but the machine produced no lot files at all and the only
    signal was a stack trace per event. Catch it here, where the operator is
    looking at the field they just typed.
    """
    value = _require_str(data, key)
    # "." and ".." are directory entries, not names, and Windows reserves the
    # device names below no matter what extension follows them - a file called
    # CON_Lot_....csv cannot be created at all.
    if set(value) == {"."} or value.upper().split(".")[0] in _WINDOWS_RESERVED:
        raise ConfigError(
            f"'{key}' must not be {value!r}: Windows cannot create a file "
            f"whose name starts with that."
        )
    if not _FILENAME_SAFE.match(value):
        bad = sorted({c for c in value if not _FILENAME_SAFE.match(c)})
        raise ConfigError(
            f"'{key}' must contain only letters, digits, dot, dash and "
            f"underscore - it is used in file and directory names. "
            f"Got {value!r} (offending: {' '.join(repr(c) for c in bad)})"
        )
    return value


def _as_int(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be an integer") from exc


def _as_float(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be a number") from exc


def _require_range(
    key: str,
    value: int | float,
    *,
    minimum: int | float,
    maximum: int | float | None = None,
) -> None:
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise ConfigError(f"'{key}' must be in range {minimum}{suffix}, got {value}")


def _as_bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.upper() in {"TRUE", "YES", "ON", "1"}:
            return True
        if value.upper() in {"FALSE", "NO", "OFF", "0"}:
            return False
    raise ConfigError(f"'{key}' must be boolean")


def _as_str_list(data: Mapping[str, Any], key: str, default: List[str]) -> List[str]:
    value = data.get(key, default)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"'{key}' must be a list")
    result: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"'{key}' items must be strings")
        if item.strip():
            result.append(item.strip())
    return result


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {}) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"machines[].{key} must be a mapping")
    return value


_SECS_TYPES = {
    "A", "ASCII", "STRING", "BOOLEAN",
    "U1", "U2", "U4", "U8", "I1", "I2", "I4", "I8", "F4", "F8",
}


def _validate_secs_value(path: str, secs_type: str, value: Any) -> None:
    normalized = secs_type.upper()
    if normalized not in _SECS_TYPES:
        raise ConfigError(f"{path} has unsupported SECS type {secs_type!r}")
    valid = True
    if normalized in {"A", "ASCII", "STRING"}:
        valid = isinstance(value, str)
    elif normalized == "BOOLEAN":
        valid = isinstance(value, bool)
    elif normalized.startswith(("U", "I")):
        valid = isinstance(value, int) and not isinstance(value, bool)
        if valid:
            bits = int(normalized[1:]) * 8
            minimum = 0 if normalized.startswith("U") else -(2 ** (bits - 1))
            maximum = 2**bits - 1 if minimum == 0 else 2 ** (bits - 1) - 1
            valid = minimum <= value <= maximum
    elif normalized.startswith("F"):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not valid:
        raise ConfigError(f"{path} value {value!r} is incompatible with type {normalized}")


def _validate_simulator_definitions(
    raw: Any, profile: Any
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate inline event/report/DVID/SVID definitions and extract SV values."""
    if raw in (None, {}):
        return {}, {}
    if not isinstance(raw, dict):
        raise ConfigError("machines[].simulator.event_definitions must be a mapping")
    allowed = {"reports", "events", "dvid_names", "dvid_types", "dvid_values", "svids"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "machines[].simulator.event_definitions has unknown key(s): "
            + ", ".join(unknown)
        )

    reports_raw = raw.get("reports", [])
    events_raw = raw.get("events", [])
    names_raw = raw.get("dvid_names", {})
    types_raw = raw.get("dvid_types", {})
    values_raw = raw.get("dvid_values", {})
    svids_raw = raw.get("svids", [])
    if not isinstance(reports_raw, list) or not isinstance(events_raw, list):
        raise ConfigError("simulator reports/events must be lists")
    if not all(isinstance(item, dict) for item in (names_raw, types_raw, values_raw)):
        raise ConfigError("simulator dvid_names/dvid_types/dvid_values must be mappings")
    if not isinstance(svids_raw, list):
        raise ConfigError("simulator svids must be a list")

    try:
        dvid_names = {int(key): str(value) for key, value in names_raw.items()}
        dvid_types = {int(key): str(value).upper() for key, value in types_raw.items()}
        dvid_values = {int(key): value for key, value in values_raw.items()}
    except (TypeError, ValueError) as exc:
        raise ConfigError("simulator DVID keys must be integer IDs") from exc

    reports: List[Dict[str, Any]] = []
    report_ids: set[int] = set()
    for item in reports_raw:
        try:
            rptid = int(item["rptid"])
            dvids = [int(value) for value in item.get("dvids", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("simulator reports require integer rptid/dvids") from exc
        if rptid in report_ids:
            raise ConfigError(f"simulator definitions contain duplicate RPTID {rptid}")
        report_ids.add(rptid)
        for dvid in dvids:
            if dvid not in dvid_names or dvid not in dvid_types:
                raise ConfigError(f"simulator report {rptid} references undefined DVID {dvid}")
        reports.append({**item, "rptid": rptid, "dvids": dvids})

    events: List[Dict[str, Any]] = []
    ceids: set[int] = set()
    lifecycle: set[str] = set()
    for item in events_raw:
        try:
            ceid = int(item["ceid"])
            rptids = [int(value) for value in item.get("rptids", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("simulator events require integer ceid/rptids") from exc
        if ceid in ceids:
            raise ConfigError(f"simulator definitions contain duplicate CEID {ceid}")
        ceids.add(ceid)
        missing_reports = sorted(set(rptids) - report_ids)
        if missing_reports:
            raise ConfigError(
                f"simulator event {ceid} references unknown report {missing_reports[0]}"
            )
        name = str(item.get("name", "")).strip()
        event_type = name if name in _SIMULATOR_EVENT_TYPES else profile.resolve_event(
            raw_event=name
        ).event_type
        if event_type != "unknown":
            lifecycle.add(event_type)
        events.append({**item, "ceid": ceid, "name": name, "rptids": rptids})

    svid_values: Dict[str, Any] = {}
    svid_ids: set[int] = set()
    svids: List[Dict[str, Any]] = []
    for item in svids_raw:
        if not isinstance(item, dict):
            raise ConfigError("simulator svids entries must be mappings")
        try:
            svid = int(item["svid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("simulator svids require integer svid") from exc
        if svid in svid_ids:
            raise ConfigError(f"simulator definitions contain duplicate SVID {svid}")
        svid_ids.add(svid)
        secs_type = str(item.get("type", "")).upper()
        if "value" in item:
            _validate_secs_value(f"simulator SVID {svid}", secs_type, item["value"])
            svid_values[str(svid)] = item["value"]
        svids.append({**item, "svid": svid, "type": secs_type})

    for dvid, value in dvid_values.items():
        if dvid not in dvid_types:
            raise ConfigError(f"simulator DVID value {dvid} has no declared type")
        _validate_secs_value(f"simulator DVID {dvid}", dvid_types[dvid], value)

    if events:
        required = {"lot_start", "lot_end", "wafer_start", "wafer_end"}
        missing_lifecycle = sorted(required - lifecycle)
        if missing_lifecycle:
            raise ConfigError(
                "simulator event definitions are lifecycle-incomplete; missing "
                + ", ".join(missing_lifecycle)
            )
    normalized = {
        "reports": reports,
        "events": events,
        "dvid_names": {str(key): value for key, value in dvid_names.items()},
        "dvid_types": {str(key): value for key, value in dvid_types.items()},
        "dvid_values": {str(key): value for key, value in dvid_values.items()},
        "svids": svids,
    }
    return normalized, svid_values


# SEMI E37 permits 1..120 s for each HSMS-SS timer, and both vendor manuals
# that state a range agree. Kept here so a bad value is a config error the
# operator sees at load, not a link that misbehaves in production.
_HSMS_TIMER_NAMES = ("t3", "t5", "t6", "t7", "t8")
_HSMS_TIMER_MIN = 1
_HSMS_TIMER_MAX = 120


def _hsms_timers_from_dict(
    data: Mapping[str, Any], profile: Any, endpoint_id: str
) -> Dict[str, int]:
    """The machine's HSMS timers: the profile's documented values, plus any
    per-machine override.

    The profile carries what the vendor manual states for that model. A tool
    whose timers were retuned on site needs the override, because the host must
    follow the tool: whichever side has the shorter timer declares a
    communications failure first, and the link then drops for no visible
    reason.
    """
    resolved: Dict[str, int] = {}
    for raw_name, raw_value in (getattr(profile, "hsms_timers", {}) or {}).items():
        key = str(raw_name).strip().lower()
        if key not in _HSMS_TIMER_NAMES:
            raise ConfigError(
                f"Machine {endpoint_id}: profile supplies unknown HSMS timer "
                f"{raw_name!r}; expected one of {list(_HSMS_TIMER_NAMES)}"
            )
        try:
            seconds = int(raw_value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"Machine {endpoint_id}: profile hsms_timers.{key} must be a "
                f"whole number of seconds, got {raw_value!r}"
            ) from None
        if not _HSMS_TIMER_MIN <= seconds <= _HSMS_TIMER_MAX:
            raise ConfigError(
                f"Machine {endpoint_id}: profile hsms_timers.{key} must be "
                f"between {_HSMS_TIMER_MIN} and {_HSMS_TIMER_MAX} seconds, got "
                f"{seconds}"
            )
        resolved[key] = seconds
    raw = data.get("hsms_timers")
    if raw is None:
        return resolved
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"Machine {endpoint_id}: 'hsms_timers' must be a mapping of "
            f"timer name to seconds, got {type(raw).__name__}"
        )
    for name, value in raw.items():
        key = str(name).strip().lower()
        if key not in _HSMS_TIMER_NAMES:
            raise ConfigError(
                f"Machine {endpoint_id}: unknown HSMS timer {name!r}; "
                f"expected one of {list(_HSMS_TIMER_NAMES)}"
            )
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"Machine {endpoint_id}: hsms_timers.{key} must be a whole "
                f"number of seconds, got {value!r}"
            ) from None
        if not _HSMS_TIMER_MIN <= seconds <= _HSMS_TIMER_MAX:
            raise ConfigError(
                f"Machine {endpoint_id}: hsms_timers.{key} must be between "
                f"{_HSMS_TIMER_MIN} and {_HSMS_TIMER_MAX} seconds, got {seconds}"
            )
        resolved[key] = seconds
    return resolved


def machine_from_dict(
    data: Mapping[str, Any],
    profiles: ProfileRegistry,
    http_defaults: Optional[LinkstuffsHttpConfig] = None,
) -> MachineConfig:
    endpoint_id = _require_str(data, "endpoint_id")
    display_name = _require_filename_safe(data, "display_name")
    machine_profile = _require_str(data, "machine_profile")
    if not profiles.has(machine_profile):
        known = ", ".join(profiles.list_profile_ids())
        raise ConfigError(
            f"Machine {endpoint_id} uses Unknown profile '{machine_profile}'. Known: {known}"
        )
    host = _require_str(data, "host")
    profile = profiles.get(machine_profile)
    hsms_mode = str(data.get("hsms_mode", "active")).strip().lower()
    if hsms_mode not in {"active", "passive"}:
        raise ConfigError(
            f"Machine {endpoint_id}: hsms_mode must be 'active' or 'passive', "
            f"got {data.get('hsms_mode')!r}"
        )
    port = _as_int(data, "port", profile.default_port)
    secs_device_id = _as_int(
        data, "secs_device_id", profile.default_secs_device_id
    )
    _require_range("port", port, minimum=1, maximum=65535)
    _require_range("secs_device_id", secs_device_id, minimum=0, maximum=32767)
    hsms_timers = _hsms_timers_from_dict(data, profile, endpoint_id)
    nexgen_safeguards = machine_profile == "nexgen_mg_series"
    alarm_rate_limit_raw = data.get("alarm_rate_limit")
    alarm_rate_limit = 50 if nexgen_safeguards else None
    if alarm_rate_limit_raw is not None:
        alarm_rate_limit = _as_int(data, "alarm_rate_limit", 0)
        _require_range("alarm_rate_limit", alarm_rate_limit, minimum=1)
    runtime_mode = str(data.get("runtime_mode", "real")).strip().lower()
    if runtime_mode not in {"real", "simulated"}:
        raise ConfigError(
            f"Machine {endpoint_id}: runtime_mode must be 'real' or "
            f"'simulated', got {data.get('runtime_mode')!r}"
        )
    storage_raw = _section(data, "storage")
    simulator_raw = _section(data, "simulator")
    machine_http_raw = _section(data, "linkstuffs_http")
    defaults = http_defaults or LinkstuffsHttpConfig()
    default_token = defaults.device_tokens.get(display_name, "")
    token = str(machine_http_raw.get("device_token", default_token) or "")
    ceid_raw = simulator_raw.get("ceid_overrides", {}) or {}
    svid_raw = simulator_raw.get("svid_values", {}) or {}
    event_definitions, definition_svid_values = _validate_simulator_definitions(
        simulator_raw.get("event_definitions", {}), profile
    )
    if not isinstance(ceid_raw, dict):
        raise ConfigError("machines[].simulator.ceid_overrides must be a mapping")
    if not isinstance(svid_raw, dict):
        raise ConfigError("machines[].simulator.svid_values must be a mapping")
    try:
        ceid_overrides = {str(key): int(value) for key, value in ceid_raw.items()}
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "machines[].simulator.ceid_overrides values must be integers"
        ) from exc
    unknown_overrides = sorted(set(ceid_overrides) - _SIMULATOR_EVENT_TYPES)
    if unknown_overrides:
        raise ConfigError(
            "machines[].simulator.ceid_overrides contains unknown lifecycle "
            "event(s): " + ", ".join(unknown_overrides)
        )
    if len(set(ceid_overrides.values())) != len(ceid_overrides):
        raise ConfigError(
            "machines[].simulator.ceid_overrides values must be unique"
        )
    override_profile = profile_with_subscription_file(
        profile,
        data.get("event_subscription_path") or profile.event_subscription_path,
    )
    for event_type, ceid in ceid_overrides.items():
        existing = override_profile.resolve_event(ceid=ceid)
        inline_event = next(
            (
                item
                for item in event_definitions.get("events", [])
                if int(item["ceid"]) == ceid
            ),
            None,
        )
        inline_type = (
            str(inline_event.get("name", ""))
            if inline_event is not None
            else "unknown"
        )
        if inline_type not in _SIMULATOR_EVENT_TYPES and inline_event is not None:
            inline_type = profile.resolve_event(raw_event=inline_type).event_type
        if inline_type not in {"unknown", event_type}:
            raise ConfigError(
                "machines[].simulator.ceid_overrides cannot reuse inline CEID "
                f"{ceid} ({inline_type}) for {event_type}"
            )
        if existing.event_type not in {"unknown", event_type}:
            raise ConfigError(
                "machines[].simulator.ceid_overrides cannot reuse CEID "
                f"{ceid} ({existing.event_type}) for {event_type}"
            )
    for key, value in svid_raw.items():
        try:
            int(key)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"machines[].simulator.svid_values key {key!r} must be an integer"
            ) from exc
        if not isinstance(value, (str, int, float, bool)):
            raise ConfigError(
                f"machines[].simulator.svid_values.{key} must be scalar"
            )
    combined_svid_values = {
        **definition_svid_values,
        **{str(key): value for key, value in svid_raw.items()},
    }
    implementation = str(
        simulator_raw.get("implementation", "profile")
    ).strip().lower()
    allowed_implementations = {"profile", "davinci_advanced", "nexgen_advanced"}
    if implementation not in allowed_implementations:
        raise ConfigError(
            "machines[].simulator.implementation must be one of: "
            + ", ".join(sorted(allowed_implementations))
        )
    required_profile = {
        "davinci_advanced": "davinci_200_mc4_hc1",
        "nexgen_advanced": "nexgen_mg_series",
    }.get(implementation)
    if required_profile is not None and machine_profile != required_profile:
        raise ConfigError(
            f"simulator implementation {implementation} requires profile "
            f"{required_profile}"
        )
    machine = MachineConfig(
        endpoint_id=endpoint_id,
        display_name=display_name,
        machine_profile=machine_profile,
        host=host,
        port=port,
        secs_device_id=secs_device_id,
        enabled=_as_bool(data, "enabled", True),
        runtime_mode=runtime_mode,
        offline_test_mode=_as_bool(data, "offline_test_mode", False),
        storage=MachineStorageConfig(
            log_dir=storage_raw.get("log_dir"),
            simulator_log_dir=storage_raw.get("simulator_log_dir"),
            local_csv_path=storage_raw.get("local_csv_path"),
            network_csv_path=storage_raw.get("network_csv_path"),
            admin_config_path=storage_raw.get("admin_config_path"),
        ),
        linkstuffs_http=MachineLinkstuffsHttpConfig(
            enabled=_as_bool(
                machine_http_raw, "enabled", defaults.enabled
            ),
            # `or` not `get(default)`: a key present with a null value is
            # common in hand-edited YAML and str(None) is the literal "None",
            # which then fails validation with a baffling message.
            base_url=_validate_base_url(
                str(machine_http_raw.get("base_url") or defaults.base_url or ""),
                f"Machine {endpoint_id}: linkstuffs_http.base_url",
            ),
            device_token=token,
            verify_tls=_as_bool(
                machine_http_raw, "verify_tls", defaults.verify_tls
            ),
            allow_insecure=_as_bool(
                machine_http_raw, "allow_insecure", defaults.allow_insecure
            ),
            timeout_sec=_as_float(
                machine_http_raw, "timeout_sec", defaults.timeout_sec
            ),
            retry_count=_as_int(
                machine_http_raw, "retry_count", defaults.retry_count
            ),
            retry_delay_sec=_as_float(
                machine_http_raw, "retry_delay_sec", defaults.retry_delay_sec
            ),
        ),
        simulator=MachineSimulatorConfig(
            implementation=implementation,
            mdln=str(simulator_raw.get("mdln", "")),
            softrev=str(simulator_raw.get("softrev", "")),
            alarm_id=_as_int(simulator_raw, "alarm_id", 0),
            alarm_text=str(simulator_raw.get("alarm_text", "")),
            wafer_count=_as_int(simulator_raw, "wafer_count", 3),
            event_interval_sec=_as_float(
                simulator_raw, "event_interval_sec", 0.5
            ),
            repeat_lots=_as_bool(simulator_raw, "repeat_lots", True),
            emit_alarm=_as_bool(simulator_raw, "emit_alarm", True),
            ceid_overrides=ceid_overrides,
            svid_values=combined_svid_values,
            event_definitions=event_definitions,
        ),
        local_csv_path=data.get("local_csv_path"),
        network_csv_path=data.get("network_csv_path"),
        admin_config_path=data.get("admin_config_path"),
        event_subscription_path=data.get("event_subscription_path"),
        event_subscription_enabled=_as_bool(data, "event_subscription_enabled", True),
        svid_collection_enabled=_as_bool(data, "svid_collection_enabled", True),
        enable_alarms=_as_bool(data, "enable_alarms", nexgen_safeguards),
        request_online=_as_bool(data, "request_online", nexgen_safeguards),
        drain_spool_on_connect=_as_bool(data, "drain_spool_on_connect", False),
        reset_subscription_on_connect=_as_bool(
            data, "reset_subscription_on_connect", False
        ),
        hsms_mode=hsms_mode,
        # Explicit operator-configured passive HSMS listener address.
        hsms_bind_address=str(
            data.get("hsms_bind_address", "0.0.0.0")  # nosec B104
        ),
        hsms_timers=hsms_timers,
        alarm_rate_limit=alarm_rate_limit,
    )
    route = machine.linkstuffs_http
    _require_range(
        "machines[].linkstuffs_http.timeout_sec",
        route.timeout_sec,
        minimum=0.001,
    )
    _require_range(
        "machines[].linkstuffs_http.retry_count",
        route.retry_count,
        minimum=0,
    )
    _require_range(
        "machines[].linkstuffs_http.retry_delay_sec",
        route.retry_delay_sec,
        minimum=0,
    )
    _require_range(
        "machines[].simulator.wafer_count",
        machine.simulator.wafer_count,
        minimum=1,
        maximum=300,
    )
    _require_range(
        "machines[].simulator.event_interval_sec",
        machine.simulator.event_interval_sec,
        minimum=0.01,
    )
    _require_range(
        "machines[].simulator.alarm_id",
        machine.simulator.alarm_id,
        minimum=0,
    )
    for event_type, ceid in machine.simulator.ceid_overrides.items():
        _require_range(
            f"machines[].simulator.ceid_overrides.{event_type}",
            ceid,
            minimum=1,
        )
    if machine.enabled and not machine.is_simulated and machine.machine_profile == "ptiq_secsgem":
        subscription_path = machine.event_subscription_path or profile.event_subscription_path
        effective = profile_with_subscription_file(profile, subscription_path)
        if not effective.ceid_aliases:
            raise ConfigError(
                f"Machine {endpoint_id}: enabled real PTIQ requires a valid "
                "event_subscription_path with installation CEIDs"
            )
        required_lifecycle = {
            "lot_start", "lot_end", "wafer_start", "wafer_end"
        }
        complete = {
            effective.resolve_event(ceid=ceid).event_type
            for ceid, layout in effective.ceid_dv_layout.items()
            if layout
        }
        missing = sorted(required_lifecycle - complete)
        if missing:
            raise ConfigError(
                f"Machine {endpoint_id}: enabled real PTIQ requires a complete "
                "event/report/DVID definition; missing " + ", ".join(missing)
            )
    return machine


def _validate_base_url(value: str, where: str) -> str:
    """Reject a base_url that already carries the device endpoint path.

    LinkstuffsHttpPublisher._url_for appends /api/v1/<token>/<suffix>, so
    base_url must be the origin alone. Pasting a full endpoint URL - which
    is what the Linkstuffs UI shows you, complete with the token - yields
    .../api/v1/<token>/telemetry/api/v1/<token>/telemetry and every publish
    404s while CSVs keep being written, so it reads as a server fault.

    It also keeps a device token out of a field the panel does not mask.
    """
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(
            f"{where} must be an absolute http:// or https:// URL with a host"
        )
    # The substring match is deliberate: LinkstuffsHttpPublisher._url_for
    # appends /api/v1/<token>/<suffix> unconditionally, so ANY base_url whose
    # path already contains /api/v1/ would produce a doubled
    # /api/v1/.../api/v1/... path. `endswith` alone is not enough because the
    # UI's full endpoint URL carries /api/v1/<token>/telemetry, not a bare
    # /api/v1 tail.
    if "/api/v1/" in cleaned or cleaned.endswith("/api/v1"):
        origin = cleaned.split("/api/v1", 1)[0] or "http://<host>:<port>"
        raise ConfigError(
            f"{where} must be the server origin only, not a full endpoint "
            f"URL. The /api/v1/<device-token>/telemetry path is added "
            f"automatically. Use: {origin} and put the device token in the "
            f"machine's own 'Device token' field."
        )
    return cleaned


def _validate_unique(machines: Iterable[MachineConfig]) -> None:
    endpoints: Dict[str, str] = {}
    display_names: Dict[str, str] = {}
    # (bind_address, port) -> endpoint_id, only for PASSIVE machines because
    # they actually bind a TCP socket. ACTIVE machines just dial out, so two
    # ACTIVE configs sharing host:port (different physical machines on a
    # routed network) is legitimate.
    passive_binds: Dict[Tuple[str, int], str] = {}
    simulator_ports: Dict[int, str] = {}
    simulator_listeners: Dict[int, str] = {}
    machine_list = list(machines)
    for machine in machine_list:
        if machine.endpoint_id in endpoints:
            raise ConfigError(f"Duplicate endpoint_id: {machine.endpoint_id}")
        endpoints[machine.endpoint_id] = machine.display_name
        if machine.display_name in display_names:
            first = display_names[machine.display_name]
            raise ConfigError(
                f"Duplicate display_name '{machine.display_name}' used by "
                f"{first} and {machine.endpoint_id}"
            )
        display_names[machine.display_name] = machine.endpoint_id
        if machine.is_passive and machine.enabled:
            key = (machine.hsms_bind_address, machine.port)
            first = passive_binds.get(key)
            if first is None:
                if machine.hsms_bind_address == "0.0.0.0":  # nosec B104
                    first = next(
                        (
                            endpoint_id
                            for (_, port), endpoint_id in passive_binds.items()
                            if port == machine.port
                        ),
                        None,
                    )
                else:
                    first = passive_binds.get(("0.0.0.0", machine.port))  # nosec B104
            if first is not None:
                raise ConfigError(
                    f"Two passive machines cannot share an overlapping bind on "
                    f"port {machine.port}: {first} and {machine.endpoint_id}. "
                    "The 0.0.0.0 wildcard overlaps every IPv4 address on that port."
                )
            passive_binds[key] = machine.endpoint_id
        if machine.enabled and machine.is_simulated:
            first = simulator_ports.get(machine.port)
            if first is not None:
                raise ConfigError(
                    "Two simulated machines cannot share simulator endpoint "
                    f"port {machine.port}: {first} and {machine.endpoint_id}"
                )
            simulator_ports[machine.port] = machine.endpoint_id
            if not machine.is_passive:
                simulator_listeners[machine.port] = machine.endpoint_id
    for port, simulator_endpoint in simulator_listeners.items():
        conflict = next(
            (
                endpoint_id
                for (_address, passive_port), endpoint_id in passive_binds.items()
                if passive_port == port and endpoint_id != simulator_endpoint
            ),
            None,
        )
        if conflict is not None:
            raise ConfigError(
                "Conflicting simulator endpoint listener on port "
                f"{port}: {simulator_endpoint} overlaps {conflict}"
            )


def load_service_config(
    config_path: str | Path,
    profiles: Optional[ProfileRegistry] = None,
) -> ServiceConfig:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ConfigError(f"Could not read config {path}: {problem}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Could not read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")
    return service_config_from_dict(raw, profiles=profiles)


def service_config_from_dict(
    raw: Mapping[str, Any],
    profiles: Optional[ProfileRegistry] = None,
) -> ServiceConfig:
    raw = _expand_env_value(dict(raw))
    profiles = profiles or ProfileRegistry()
    machine_items = raw.get("machines", [])
    if not isinstance(machine_items, list):
        raise ConfigError("'machines' must be a list")

    linkstuffs_raw = raw.get("linkstuffs", {}) or {}
    if not isinstance(linkstuffs_raw, dict):
        raise ConfigError("'linkstuffs' must be a mapping")
    linkstuffs = LinkstuffsConfig(
        host=str(linkstuffs_raw.get("host", "127.0.0.1")),
        port=_as_int(linkstuffs_raw, "port", 8883),
        access_token=str(linkstuffs_raw.get("access_token", "")),
        enabled=_as_bool(linkstuffs_raw, "enabled", False),
        tls=_as_bool(linkstuffs_raw, "tls", True),
        allow_insecure=_as_bool(linkstuffs_raw, "allow_insecure", False),
        qos=_as_int(linkstuffs_raw, "qos", 1),
        client_id=str(linkstuffs_raw.get("client_id", "astar-eap-middleware")),
        keepalive_sec=_as_int(linkstuffs_raw, "keepalive_sec", 60),
        publish_retain=_as_bool(linkstuffs_raw, "publish_retain", False),
    )
    if linkstuffs.enabled and not linkstuffs.access_token:
        raise ConfigError("linkstuffs.access_token is required when Linkstuffs is enabled")
    if linkstuffs.enabled and not linkstuffs.tls and not linkstuffs.allow_insecure:
        raise ConfigError(
            "Plaintext MQTT with an access token is disabled; set linkstuffs.tls=true "
            "or explicitly set linkstuffs.allow_insecure=true for an approved test network"
        )
    if linkstuffs.qos not in (0, 1):
        raise ConfigError("linkstuffs.qos must be 0 or 1")
    _require_range("linkstuffs.port", linkstuffs.port, minimum=1, maximum=65535)
    _require_range("linkstuffs.keepalive_sec", linkstuffs.keepalive_sec, minimum=1)

    http_raw = raw.get("linkstuffs_http", {}) or {}
    if not isinstance(http_raw, dict):
        raise ConfigError("'linkstuffs_http' must be a mapping")
    tokens_raw = http_raw.get("device_tokens", {}) or {}
    if not isinstance(tokens_raw, dict):
        raise ConfigError("'linkstuffs_http.device_tokens' must be a mapping")
    linkstuffs_http = LinkstuffsHttpConfig(
        enabled=_as_bool(http_raw, "enabled", False),
        base_url=_validate_base_url(
            str(http_raw.get("base_url", "")), "linkstuffs_http.base_url"
        ),
        device_tokens={str(k): str(v) for k, v in tokens_raw.items() if v},
        timeout_sec=_as_float(http_raw, "timeout_sec", 10.0),
        retry_count=_as_int(http_raw, "retry_count", 3),
        retry_delay_sec=_as_float(http_raw, "retry_delay_sec", 1.0),
        verify_tls=_as_bool(http_raw, "verify_tls", True),
        allow_insecure=_as_bool(http_raw, "allow_insecure", False),
    )
    http_insecure = (
        not linkstuffs_http.verify_tls
        or linkstuffs_http.base_url.lower().startswith("http://")
    )
    if linkstuffs_http.enabled and http_insecure and not linkstuffs_http.allow_insecure:
        raise ConfigError(
            "Insecure Linkstuffs HTTP is disabled; use an https:// base_url with "
            "verify_tls=true, or explicitly set linkstuffs_http.allow_insecure=true "
            "only for an approved test/lab network"
        )
    if linkstuffs_http.enabled and http_insecure:
        logger.warning(
            "TEST/LAB INSECURE HTTP override is enabled for %s; device tokens "
            "and telemetry are not protected by the production TLS policy.",
            linkstuffs_http.base_url or "(no base_url)",
        )
    _require_range("linkstuffs_http.timeout_sec", linkstuffs_http.timeout_sec, minimum=0.001)
    _require_range("linkstuffs_http.retry_count", linkstuffs_http.retry_count, minimum=0)
    _require_range("linkstuffs_http.retry_delay_sec", linkstuffs_http.retry_delay_sec, minimum=0)

    machines = [
        machine_from_dict(item, profiles, linkstuffs_http)
        for item in machine_items
    ]
    _validate_unique(machines)

    for machine in machines:
        route = machine.linkstuffs_http
        route_insecure = (
            not route.verify_tls or route.base_url.lower().startswith("http://")
        )
        if route.enabled and route_insecure and not route.allow_insecure:
            raise ConfigError(
                f"Machine {machine.endpoint_id}: insecure linkstuffs_http is "
                "disabled; use HTTPS with verify_tls=true or explicitly set "
                "allow_insecure=true only for an approved test/lab network"
            )
        if route.enabled and route_insecure:
            logger.warning(
                "TEST/LAB INSECURE HTTP override is enabled for machine %s; "
                "its device token and telemetry are not protected by the "
                "production TLS policy.",
                machine.endpoint_id,
            )

    enabled_machines = [machine for machine in machines if machine.enabled]
    routed_machines = [
        machine for machine in enabled_machines if not machine.offline_test_mode
    ]
    missing_base_urls = [
        machine.display_name
        for machine in routed_machines
        if machine.linkstuffs_http.enabled
        and not machine.linkstuffs_http.base_url
    ]
    if missing_base_urls:
        raise ConfigError(
            "linkstuffs_http.base_url is required for enabled machines: "
            + ", ".join(sorted(missing_base_urls))
        )
    # A routed machine may use its own HTTPS route, the global MQTT gateway,
    # or both. Tokens belong only to HTTPS users; requiring one for an
    # MQTT-only machine made the documented fallback impossible to configure.
    missing_tokens = [
        machine.display_name
        for machine in routed_machines
        if machine.linkstuffs_http.enabled
        and not machine.linkstuffs_http.device_token.strip()
    ]
    if missing_tokens:
        raise ConfigError(
            "Missing linkstuffs_http.device_tokens for enabled machines: "
            + ", ".join(sorted(missing_tokens))
            + ". Enter the device token for each, or tick 'Offline test mode' "
            "on the machine to run it with no upstream at all."
        )
    missing_routes = [
        machine.display_name
        for machine in routed_machines
        if not machine.linkstuffs_http.enabled and not linkstuffs.enabled
    ]
    if missing_routes:
        raise ConfigError(
            "No upstream route for enabled machines: "
            + ", ".join(sorted(missing_routes))
            + ". Enable machine HTTPS with a device token, enable the global "
            "MQTT gateway with secure credentials, or tick 'Offline test "
            "mode' to run the machine with no upstream. If a token is already "
            "present in linkstuffs_http.device_tokens, this is not about the "
            "device token: the HTTPS route itself is disabled."
        )

    legacy_raw = raw.get("legacy_api", {}) or {}
    if not isinstance(legacy_raw, dict):
        raise ConfigError("'legacy_api' must be a mapping")
    legacy_api = LegacyApiConfig(
        enabled=_as_bool(legacy_raw, "enabled", False),
        url=str(legacy_raw.get("url", "")),
        allow_insecure=_as_bool(legacy_raw, "allow_insecure", False),
        encrypted=_as_bool(legacy_raw, "encrypted", True),
        encryption_mode=str(legacy_raw.get("encryption_mode") or "")
        .strip()
        .lower(),
        encryption_key_b64=str(legacy_raw.get("encryption_key_b64") or ""),
        first_key=str(legacy_raw.get("first_key") or ""),
        second_key=str(legacy_raw.get("second_key") or ""),
        first_key_b64=str(legacy_raw.get("first_key_b64", "")),
        second_key_b64=str(legacy_raw.get("second_key_b64", "")),
        timeout_sec=_as_float(legacy_raw, "timeout_sec", 30.0),
        retry_count=_as_int(legacy_raw, "retry_count", 3),
        retry_delay_sec=_as_float(legacy_raw, "retry_delay_sec", 1.0),
        send_tool_events=_as_str_list(
            legacy_raw,
            "send_tool_events",
            ["Lot_Start", "Lot_End"],
        ),
        token_id=str(legacy_raw.get("token_id", "")),
    )
    if legacy_api.enabled:
        if not legacy_api.url:
            raise ConfigError("legacy_api.url is required when legacy_api is enabled")
        legacy_url = urlsplit(legacy_api.url)
        if (
            legacy_url.scheme.lower() not in {"http", "https"}
            or not legacy_url.hostname
        ):
            raise ConfigError(
                "legacy_api.url must be an absolute http:// or https:// URL with a host"
            )
        if legacy_url.scheme.lower() != "https" and not legacy_api.allow_insecure:
            raise ConfigError(
                "Insecure legacy_api HTTP is disabled; use an https:// URL or "
                "explicitly set legacy_api.allow_insecure=true only for an "
                "approved test/lab network"
            )
        if legacy_url.scheme.lower() != "https":
            logger.warning(
                "TEST/LAB INSECURE HTTP override is enabled for legacy_api; "
                "payload transport is not protected by the production TLS policy."
            )
        if legacy_api.encrypted:
            if not legacy_api.encryption_mode:
                raise ConfigError(
                    "legacy_api.encryption_mode must be explicit when encrypted "
                    "legacy_api is enabled; use aes_256_gcm_v2 for new peers or "
                    "legacy_ctr_v1 only for an existing compatible peer"
                )
            if legacy_api.encryption_mode not in SUPPORTED_ENCRYPTION_MODES:
                raise ConfigError(
                    "legacy_api.encryption_mode must be one of: "
                    + ", ".join(sorted(SUPPORTED_ENCRYPTION_MODES))
                )
            if legacy_api.encryption_mode == AES_256_GCM_V2:
                if not legacy_api.encryption_key_b64:
                    raise ConfigError(
                        "legacy_api.encryption_key_b64 is required for "
                        "aes_256_gcm_v2"
                    )
                try:
                    SecurePayloadCodec.from_aes256_gcm_key_base64(
                        legacy_api.encryption_key_b64
                    )
                except SecurePayloadError as exc:
                    raise ConfigError(
                        "legacy_api.encryption_key_b64 must be valid base64 "
                        "encoding exactly 32 bytes"
                    ) from exc
            elif legacy_api.encryption_mode == LEGACY_CTR_V1:
                has_raw_keys = bool(
                    legacy_api.first_key and legacy_api.second_key
                )
                has_b64_keys = bool(
                    legacy_api.first_key_b64 and legacy_api.second_key_b64
                )
                if not has_raw_keys and not has_b64_keys:
                    raise ConfigError(
                        "legacy_api first_key/second_key or "
                        "first_key_b64/second_key_b64 are required for "
                        "legacy_ctr_v1"
                    )
    _require_range("legacy_api.timeout_sec", legacy_api.timeout_sec, minimum=0.001)
    _require_range("legacy_api.retry_count", legacy_api.retry_count, minimum=0)
    _require_range("legacy_api.retry_delay_sec", legacy_api.retry_delay_sec, minimum=0)

    paths_raw = raw.get("paths", {}) or {}
    if not isinstance(paths_raw, dict):
        raise ConfigError("'paths' must be a mapping")
    data_dir = str(paths_raw.get("data_dir", "C:/SECSGEM_EAP/data"))
    default_control_dir = str(Path(data_dir).parent / "control")
    paths = MiddlewarePaths(
        install_dir=str(paths_raw.get("install_dir", "C:/SECSGEM_EAP")),
        log_dir=str(paths_raw.get("log_dir", "C:/SECSGEM_EAP/logs")),
        data_dir=data_dir,
        control_dir=str(paths_raw.get("control_dir", default_control_dir)),
        archive_dir=str(paths_raw.get("archive_dir", "C:/SECSGEM_EAP/archive")),
        outbox_db=str(paths_raw.get("outbox_db", "C:/SECSGEM_EAP/data/outbox.sqlite3")),
        legacy_api_outbox_db=str(
            paths_raw.get(
                "legacy_api_outbox_db",
                "C:/SECSGEM_EAP/data/legacy_api_outbox.sqlite3",
            )
        ),
        http_outbox_db=str(
            paths_raw.get(
                "http_outbox_db",
                "C:/SECSGEM_EAP/data/linkstuffs_http_outbox.sqlite3",
            )
        ),
        ingress_journal_db=str(
            paths_raw.get(
                "ingress_journal_db",
                "C:/SECSGEM_EAP/data/ingress_journal.sqlite3",
            )
        ),
    )

    logging_raw = raw.get("logging", {}) or {}
    if not isinstance(logging_raw, dict):
        raise ConfigError("'logging' must be a mapping")
    logging = LoggingConfig(
        level=str(logging_raw.get("level", "INFO")).upper(),
        max_size_mb=_as_int(logging_raw, "max_size_mb", 20),
        backup_count=_as_int(logging_raw, "backup_count", 10),
    )

    retention_days = _as_int(raw, "outbox_retention_days", 30)
    reconnect_interval = _as_float(raw, "reconnect_interval_sec", 10.0)
    health_interval = _as_float(raw, "health_interval_sec", 30.0)
    startup_stagger = _as_float(raw, "startup_stagger_sec", 0.2)
    event_liveness_grace = _as_float(raw, "event_liveness_grace_sec", 120.0)
    cross_generation_window = _as_float(
        raw, "cross_generation_retransmit_window_sec", 120.0
    )
    storage_safety_raw = raw.get("storage_safety", {}) or {}
    if not isinstance(storage_safety_raw, dict):
        raise ConfigError("'storage_safety' must be a mapping")
    storage_safety = StorageSafetyConfig(
        enabled=_as_bool(storage_safety_raw, "enabled", True),
        sample_interval_sec=_as_float(
            storage_safety_raw, "sample_interval_sec", 5.0
        ),
        debounce_samples=_as_int(storage_safety_raw, "debounce_samples", 2),
        warning_free_bytes=_as_int(
            storage_safety_raw, "warning_free_bytes", 5 * 1024**3
        ),
        critical_free_bytes=_as_int(
            storage_safety_raw, "critical_free_bytes", 2 * 1024**3
        ),
        recovery_free_bytes=_as_int(
            storage_safety_raw, "recovery_free_bytes", 6 * 1024**3
        ),
        warning_free_percent=_as_float(
            storage_safety_raw, "warning_free_percent", 10.0
        ),
        critical_free_percent=_as_float(
            storage_safety_raw, "critical_free_percent", 5.0
        ),
        recovery_free_percent=_as_float(
            storage_safety_raw, "recovery_free_percent", 12.0
        ),
    )
    _require_range("logging.max_size_mb", logging.max_size_mb, minimum=1)
    _require_range("logging.backup_count", logging.backup_count, minimum=0)
    _require_range("outbox_retention_days", retention_days, minimum=0)
    _require_range("reconnect_interval_sec", reconnect_interval, minimum=0.001)
    _require_range("health_interval_sec", health_interval, minimum=0.001)
    _require_range("startup_stagger_sec", startup_stagger, minimum=0)
    _require_range("event_liveness_grace_sec", event_liveness_grace, minimum=0)
    _require_range(
        "cross_generation_retransmit_window_sec",
        cross_generation_window,
        minimum=0,
        maximum=3600,
    )
    _require_range(
        "storage_safety.sample_interval_sec",
        storage_safety.sample_interval_sec,
        minimum=0.1,
    )
    _require_range(
        "storage_safety.debounce_samples",
        storage_safety.debounce_samples,
        minimum=1,
        maximum=100,
    )
    for suffix, value in (
        ("critical_free_bytes", storage_safety.critical_free_bytes),
        ("warning_free_bytes", storage_safety.warning_free_bytes),
        ("recovery_free_bytes", storage_safety.recovery_free_bytes),
    ):
        _require_range(f"storage_safety.{suffix}", value, minimum=1)
    if not (
        storage_safety.critical_free_bytes
        < storage_safety.warning_free_bytes
        < storage_safety.recovery_free_bytes
    ):
        raise ConfigError(
            "storage_safety byte thresholds must satisfy critical < warning "
            "< recovery and leave a positive reserve margin"
        )
    if not (
        0 < storage_safety.critical_free_percent
        < storage_safety.warning_free_percent
        < storage_safety.recovery_free_percent
        <= 100
    ):
        raise ConfigError(
            "storage_safety percentage thresholds must satisfy 0 < critical "
            "< warning < recovery <= 100"
        )

    return ServiceConfig(
        machines=machines,
        linkstuffs=linkstuffs,
        linkstuffs_http=linkstuffs_http,
        legacy_api=legacy_api,
        paths=paths,
        logging=logging,
        outbox_retention_days=retention_days,
        reconnect_interval_sec=reconnect_interval,
        health_interval_sec=health_interval,
        startup_stagger_sec=startup_stagger,
        event_liveness_grace_sec=event_liveness_grace,
        cross_generation_retransmit_window_sec=cross_generation_window,
        storage_safety=storage_safety,
    )
