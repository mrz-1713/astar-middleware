"""Shared production data models for the EAP middleware."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .journal import VOLATILE_PAYLOAD_KEYS


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@dataclass(frozen=True)
class MachineStorageConfig:
    """Machine-owned paths; blank values keep the legacy/default behavior."""

    log_dir: Optional[str] = None
    simulator_log_dir: Optional[str] = None
    local_csv_path: Optional[str] = None
    network_csv_path: Optional[str] = None
    admin_config_path: Optional[str] = None


@dataclass(frozen=True)
class MachineLinkstuffsHttpConfig:
    """Effective HTTPS route for one machine."""

    enabled: bool = False
    base_url: str = ""
    device_token: str = ""
    verify_tls: bool = True
    allow_insecure: bool = False
    timeout_sec: float = 10.0
    retry_count: int = 3
    retry_delay_sec: float = 1.0


@dataclass(frozen=True)
class MachineSimulatorConfig:
    """Simulator-only values; production profile objects remain immutable."""

    implementation: str = "profile"
    mdln: str = ""
    softrev: str = ""
    alarm_id: int = 0
    alarm_text: str = ""
    wafer_count: int = 3
    event_interval_sec: float = 0.5
    repeat_lots: bool = True
    emit_alarm: bool = True
    ceid_overrides: Dict[str, int] = field(default_factory=dict)
    svid_values: Dict[str, Any] = field(default_factory=dict)
    event_definitions: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MachineConfig:
    """Client-facing configuration for one machine connection."""

    endpoint_id: str
    display_name: str
    machine_profile: str
    host: str
    port: int
    secs_device_id: int = 0
    enabled: bool = True
    runtime_mode: str = "real"
    offline_test_mode: bool = False
    storage: MachineStorageConfig = field(default_factory=MachineStorageConfig)
    linkstuffs_http: MachineLinkstuffsHttpConfig = field(
        default_factory=MachineLinkstuffsHttpConfig
    )
    simulator: MachineSimulatorConfig = field(default_factory=MachineSimulatorConfig)
    local_csv_path: Optional[str] = None
    network_csv_path: Optional[str] = None
    admin_config_path: Optional[str] = None
    # Per-machine EventSubscription.json. None falls back to the profile's own
    # file. Set it when one tool of a profile has different CEID numbers from
    # the rest - the file supplies both what to subscribe to and (via
    # profile_with_subscription_file) how to decode what comes back.
    event_subscription_path: Optional[str] = None
    event_subscription_enabled: bool = True
    svid_collection_enabled: bool = True
    # Opt-in: when True, the middleware sends S5F3 (Enable All Alarms, E5
    # zero-length ALID) once communications are established so S5F1 reports are
    # guaranteed. Default False because most tools enable alarms by default and
    # not every equipment accepts a host-initiated "enable all" S5F3. Turn on
    # per machine only after confirming the tool accepts it.
    enable_alarms: bool = False
    # Resolved HSMS-SS protocol timers (SEMI E37 T3/T5/T6/T7/T8, seconds).
    # Populated by config loading from the machine's profile, with any
    # per-machine `hsms_timers:` block layered on top. Empty means the shipped
    # default. A host whose timers differ from the tool's drops the link
    # intermittently, so this has to follow the tool, not the host.
    hsms_timers: Dict[str, int] = field(default_factory=dict)
    # Opt-in: when True, the middleware sends S1F17 (Request ON-LINE) once
    # communications are established. Per the DaVinci manual (§5.2.2), an
    # equipment in OFF-LINE control state responds ONLY to establish-comm and a
    # host online request - it ignores S2F33/35/37 (subscription), S1F3 (status)
    # and S5F3 (alarms) and sends NO events. If the tool may be left OFF-LINE,
    # set this True so the host brings it ON-LINE and data can flow. Default
    # False to preserve the read-only/no-side-effect stance (S1F17 does NOT take
    # REMOTE control - the LOCAL/REMOTE substate stays operator-controlled).
    request_online: bool = False
    # Opt-in: when True, the middleware sends S6F23 (Request Spooled Data,
    # RSDC=Transmit) once communications are established so any messages the
    # equipment spooled while the host was disconnected are re-sent (and the
    # tool's spool cleared). Default False because it's an active host message;
    # harmless (RSDA=2) if the tool isn't spooling. Turn on to recover events
    # generated during host/network outages when tool-side spooling is enabled.
    drain_spool_on_connect: bool = False
    # Opt-in: when True, the middleware clears the tool's whole event-report
    # configuration before defining its own - S2F37 CEED=false with a
    # zero-length CEID list, S2F35 with a zero-length DATA list, S2F33 with a
    # zero-length DATA list. All three are the SEMI E5 "delete all" forms and
    # are exactly what the NexGen MG manual prescribes as steps 2-4 of its own
    # lot-start sequence (§9.1 p.170, wire traces §9.1.1.2-9.1.1.4).
    #
    # Why it is not the default: on a tool that has only ever talked to this
    # middleware the reset is a no-op, and on a *commissioned* tool it changes
    # a message sequence the equipment has already accepted. It matters when
    # the tool has previously talked to another host - the commissioning case -
    # because report definitions and CEID links survive on the equipment. A
    # CEID still linked to a report this middleware has just redefined delivers
    # a payload against a layout the mapper no longer expects, and CEIDs the
    # previous host enabled keep arriving as `unknown`.
    #
    # Applied once per connection, before the first band, so a banded
    # subscription does not wipe out the bands that preceded it.
    reset_subscription_on_connect: bool = False
    # HSMS connection direction. "active" = middleware connects out to the
    # machine at host:port (default; assumes the machine is HSMS-passive).
    # "passive" = middleware binds 0.0.0.0:port and waits for the machine to
    # connect inbound (the machine is HSMS-active). Some vendors / tool
    # configurations require one mode or the other; in mixed deployments
    # different machines can use different modes.
    hsms_mode: str = "active"
    # Bind address used only in passive mode. Defaults to "0.0.0.0" so we
    # accept inbound from any interface on the listen port. Override if the
    # server has multiple NICs and only one should accept SECS traffic.
    # Explicit passive-mode bind default; active mode never binds this address.
    hsms_bind_address: str = "0.0.0.0"  # nosec B104
    # Optional per-machine alarm-set rate limit. None disables throttling.
    # Alarm clears and safety-category alarms are never throttled.
    alarm_rate_limit: Optional[int] = None

    @property
    def is_passive(self) -> bool:
        return self.hsms_mode.lower() == "passive"

    @property
    def csv_local_dir(self) -> Path:
        value = self.storage.local_csv_path or self.local_csv_path
        if not value:
            return Path(f"D:/MachineData/EAP_{self.display_name}/csv_in")
        return Path(value)

    @property
    def csv_network_dir(self) -> Optional[Path]:
        value = self.storage.network_csv_path or self.network_csv_path
        if not value:
            return None
        return Path(value)

    @property
    def admin_dir(self) -> Path:
        value = self.storage.admin_config_path or self.admin_config_path
        if not value:
            return Path(f"C:/SECSGEM_EAP/machines/{self.display_name}/config")
        return Path(value)

    @property
    def log_dir(self) -> Path:
        return Path(
            self.storage.log_dir
            or f"C:/SECSGEM_EAP/logs/{self.display_name}"
        )

    @property
    def simulator_log_dir(self) -> Path:
        return Path(
            self.storage.simulator_log_dir
            or self.log_dir / "simulator"
        )

    @property
    def is_simulated(self) -> bool:
        return self.runtime_mode == "simulated"


@dataclass(frozen=True)
class LinkstuffsConfig:
    """Connection settings for the Linkstuffs MQTT gateway."""

    host: str = "127.0.0.1"
    port: int = 8883
    access_token: str = ""
    enabled: bool = False
    tls: bool = True
    allow_insecure: bool = False
    qos: int = 1
    client_id: str = "astar-eap-middleware"
    keepalive_sec: int = 60
    publish_retain: bool = False


@dataclass(frozen=True)
class LinkstuffsHttpConfig:
    """HTTPS REST upstream to Linkstuffs /api/v1/{token}/{telemetry,attributes}.

    Use when MQTT 1883 isn't reachable (Cloudflare blocks it).
    One token per device — create each device in Linkstuffs admin and map
    display_name -> token in production.yaml.
    """

    enabled: bool = False
    base_url: str = ""                # e.g. https://astar-monitoring.linkstuffs.com
    device_tokens: Dict[str, str] = field(default_factory=dict)  # display_name -> token
    timeout_sec: float = 10.0
    retry_count: int = 3
    retry_delay_sec: float = 1.0
    verify_tls: bool = True
    allow_insecure: bool = False


@dataclass(frozen=True)
class LegacyApiConfig:
    """Connection and payload-encryption settings for the legacy HTTP API."""

    enabled: bool = False
    url: str = ""
    allow_insecure: bool = False
    encrypted: bool = True
    encryption_mode: str = ""
    encryption_key_b64: str = ""
    first_key: str = ""
    second_key: str = ""
    first_key_b64: str = ""
    second_key_b64: str = ""
    timeout_sec: float = 30.0
    retry_count: int = 3
    retry_delay_sec: float = 1.0
    send_tool_events: List[str] = field(default_factory=lambda: ["Lot_Start", "Lot_End"])
    token_id: str = ""


@dataclass(frozen=True)
class MiddlewarePaths:
    """Filesystem layout of an installed middleware instance.

    The defaults are Windows-absolute. On POSIX they resolve as relative, so
    tests must override them onto a temporary directory.
    """

    install_dir: str = "C:/SECSGEM_EAP"
    log_dir: str = "C:/SECSGEM_EAP/logs"
    data_dir: str = "C:/SECSGEM_EAP/data"
    control_dir: str = "C:/SECSGEM_EAP/control"
    archive_dir: str = "C:/SECSGEM_EAP/archive"
    outbox_db: str = "C:/SECSGEM_EAP/data/outbox.sqlite3"
    legacy_api_outbox_db: str = "C:/SECSGEM_EAP/data/legacy_api_outbox.sqlite3"
    http_outbox_db: str = "C:/SECSGEM_EAP/data/linkstuffs_http_outbox.sqlite3"
    # Durable record of every SECS message the middleware acknowledged. Written
    # before the acknowledgement goes back to the tool, and replayed on startup,
    # so a crash cannot leave the equipment believing we hold data we lost.
    ingress_journal_db: str = "C:/SECSGEM_EAP/data/ingress_journal.sqlite3"


@dataclass(frozen=True)
class LoggingConfig:
    """Log level and rotation policy for the service."""

    level: str = "INFO"
    max_size_mb: int = 20
    backup_count: int = 10


@dataclass(frozen=True)
class StorageSafetyConfig:
    """Fail-closed local-storage reserve policy.

    A threshold is crossed when either the byte or percentage reserve is
    crossed. Recovery deliberately uses higher thresholds than warning so a
    service cannot flap while an operator is freeing space.
    """

    enabled: bool = True
    sample_interval_sec: float = 5.0
    debounce_samples: int = 2
    warning_free_bytes: int = 5 * 1024**3
    critical_free_bytes: int = 2 * 1024**3
    recovery_free_bytes: int = 6 * 1024**3
    warning_free_percent: float = 10.0
    critical_free_percent: float = 5.0
    recovery_free_percent: float = 12.0


@dataclass(frozen=True)
class ServiceConfig:
    """A fully validated service configuration."""

    machines: List[MachineConfig]
    linkstuffs: LinkstuffsConfig = field(default_factory=LinkstuffsConfig)
    linkstuffs_http: LinkstuffsHttpConfig = field(default_factory=LinkstuffsHttpConfig)
    legacy_api: LegacyApiConfig = field(default_factory=LegacyApiConfig)
    paths: MiddlewarePaths = field(default_factory=MiddlewarePaths)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    outbox_retention_days: int = 30
    reconnect_interval_sec: float = 10.0
    health_interval_sec: float = 30.0
    # v2 Track B: gap between session.start() calls at boot so 22 HSMS
    # connects don't simultaneously spawn 100+ secsgem threads on a small
    # Windows server. 0 disables the stagger.
    startup_stagger_sec: float = 0.2
    # Grace period after a connection reaches COMMUNICATING before the
    # event-liveness watchdog may raise a "no_event_reports" alarm. Gives the
    # async S2F33/35/37 subscription provisioning time to finish so we don't
    # alarm mid-handshake. Only relevant for profiles with a
    # health_last_event_svid (currently DaVinci).
    event_liveness_grace_sec: float = 120.0
    # Identical traffic crossing a connection generation is considered a
    # retry only for this long. Commissioning must tune this to the tool's
    # maximum retransmission period.
    cross_generation_retransmit_window_sec: float = 120.0
    storage_safety: StorageSafetyConfig = field(default_factory=StorageSafetyConfig)


@dataclass(frozen=True)
class EventMapping:
    """How one vendor event is named in the canonical and CSV outputs."""

    event_type: str
    csv_tool_event: str
    secs_raw_event: str
    closes_lot_file: bool = False


@dataclass(frozen=True)
class CanonicalEvent:
    """Vendor-neutral event used by CSV and Linkstuffs pipelines."""

    timestamp: datetime
    endpoint_id: str
    display_name: str
    machine_profile: str
    vendor: str
    model: str
    event_type: str
    raw_event_name: str = ""
    ceid: int = 0
    load_port: str = ""
    chamber: str = ""
    lot_id: str = ""
    wafer_id: str = ""
    recipe: str = ""
    secs_raw_event: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    def event_key(self) -> str:
        """Idempotency key: the same equipment message always maps here.

        Every outbox dedups on this, so it decides two separate things: that a
        message the tool retransmits after a T3 timeout is not published twice,
        and that replaying the ingress journal after a crash re-queues rather
        than duplicates.
        """
        ingress = self.raw_payload.get("_ingress_key")
        if ingress:
            # Derived straight from the SECS transaction, so it survives both
            # a retransmission (fresh arrival clock, same transaction) and a
            # replay (recomputed later, from the stored payload). Nothing
            # clock-derived may enter this branch: the event timestamp falls
            # back to "now" whenever the report carries no CLOCK, which on
            # replay would be a different "now" and a duplicate downstream.
            payload: Dict[str, Any] = {
                "endpoint_id": self.endpoint_id,
                "ingress": ingress,
                # One report can expand into several canonical events (aligned
                # E90 substrate arrays); they share a transaction, not a key.
                "ingress_index": self.raw_payload.get("_e90_index", 0),
                "event_type": self.event_type,
            }
        else:
            # Locally generated (health, SVID samples, storm summaries). There
            # is no transaction to name them by, so digest the payload -
            # minus the arrival stamps, so a retried alarm still collapses -
            # and keep ts so two genuinely different samples stay distinct.
            stable = {
                key: value
                for key, value in self.raw_payload.items()
                if key not in VOLATILE_PAYLOAD_KEYS
            }
            payload = {
                "ts": timestamp_ms(self.timestamp),
                "endpoint_id": self.endpoint_id,
                "event_type": self.event_type,
                "ceid": self.ceid,
                "lot_id": self.lot_id,
                "wafer_id": self.wafer_id,
                "raw_event_name": self.raw_event_name,
                "payload": json.dumps(stable, sort_keys=True, default=str),
            }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def telemetry_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "endpoint_id": self.endpoint_id,
            "display_name": self.display_name,
            "machine_profile": self.machine_profile,
            "vendor": self.vendor,
            "model": self.model,
            "event_type": self.event_type,
            "raw_event_name": self.raw_event_name,
            "ceid": self.ceid,
            "load_port": self.load_port,
            "chamber": self.chamber,
            "lot_id": self.lot_id,
            "wafer_id": self.wafer_id,
            "recipe": self.recipe,
            "secs_raw_event": self.secs_raw_event,
        }
        for key, value in self.raw_payload.items():
            if key in values:
                continue
            # Skip internal markers (V[] raw, storm summary flag, etc.).
            if key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                values[f"raw_{key}"] = value
            elif isinstance(value, (list, tuple, dict)):
                # Measurement payloads (DaVinci TestResults, ResultFile arrays,
                # E90 SubstIDList) arrive as nested SECS lists. Serialize them
                # Serialize nested SECS lists as JSON strings to avoid dropping data.
                try:
                    values[f"raw_{key}"] = json.dumps(value, default=str)
                except (TypeError, ValueError):
                    values[f"raw_{key}"] = str(value)
        # The complete S6F11 report set. Underscore keys are internal markers
        # and are skipped above, but this one is equipment data: skipping it
        # discarded every value in any report the profile has no layout for,
        # and those were the only copy outside the journal.
        reports = self.raw_payload.get("_reports_raw")
        if reports:
            try:
                values["raw_secs_reports"] = json.dumps(reports, default=str)
            except (TypeError, ValueError):
                values["raw_secs_reports"] = str(reports)
        return values

    def linkstuffs_telemetry_payload(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            self.display_name: [
                {
                    "ts": timestamp_ms(self.timestamp),
                    "values": self.telemetry_values(),
                }
            ]
        }

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


@dataclass(frozen=True)
class CsvRow:
    """One row of the per-lot CSV, in column order."""

    datetime: str
    tool_event: str
    eap_toolname: str
    load_port: str
    chamber: str
    lot_id: str
    wafer_id: str
    recipe: str
    secsgem_raw_event: str

    @staticmethod
    def header() -> List[str]:
        return [
            "Datetime",
            "ToolEvent",
            "EAP_ToolName",
            "LoadPort",
            "Chamber",
            "LotID",
            "WaferID",
            "Recipe",
            "SECSGEM_Raw_Event",
        ]

    def values(self) -> List[str]:
        return [
            self.datetime,
            self.tool_event,
            self.eap_toolname,
            self.load_port,
            self.chamber,
            self.lot_id,
            self.wafer_id,
            self.recipe,
            self.secsgem_raw_event,
        ]


@dataclass(frozen=True)
class SvidSelection:
    """One SVID selected for periodic collection."""

    svid: int
    name: str


@dataclass(frozen=True)
class SvidAdminState:
    """Parsed DataCollectSwitch, RecipeList and SvidList admin state.

    ``invalid_entries`` retains entries that failed validation so the GUI can
    show them instead of silently dropping them.
    """

    enabled: bool
    interval_sec: float
    recipe_list: List[str]
    recipe_svid_list: List[str]
    svids: List[SvidSelection]
    invalid_entries: List[str] = field(default_factory=list)
