import csv

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.linkstuffs import (
    LINKSTUFFS_TOPIC_ATTRIBUTES,
    LINKSTUFFS_TOPIC_CONNECT,
    LINKSTUFFS_TOPIC_TELEMETRY,
    LinkstuffsGatewayPublisher,
)


def _machine(tmp_path):
    return MachineConfig(
        endpoint_id="TOOL_01",
        display_name="SPTS_fxP_OMEGA_01",
        machine_profile="spts_fxp_omega",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
    )


def test_mapper_preserves_interrupted_lot_end_raw_event(tmp_path):
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    event = CanonicalMapper(profile).from_secs_event(
        machine,
        367,
        {
            "DATETIME": "2025-11-20 13:57:59.031103",
            "TOOL_EVENT": "Lot_End",
            "SECSGEM_RAW_EVENT": "OpStop2",
            "LOAD_PORT": 2,
            "LOT_ID": "TEST2025112_01",
        },
    )
    assert event.event_type == "lot_end"
    assert event.secs_raw_event == "OpStop2"
    assert event.lot_id == "TEST2025112_01"


def test_spts_ceid_overrides_generic_lot_end_label(tmp_path):
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    event = CanonicalMapper(profile).from_secs_event(
        machine,
        385,
        {
            "DATETIME": "2025-11-20 13:58:03.309802",
            "TOOL_EVENT": "Lot_End",
            "LOAD_PORT": 2,
            "LOT_ID": "TEST2025112_01",
        },
    )
    assert event.event_type == "lot_end"
    assert event.secs_raw_event == "OpCancel2"


def test_per_lot_csv_writer_matches_required_header_and_mirrors(tmp_path):
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    raw_events = [
        ("2025-11-18 19:57:52.173086", "Loaded", "SMIFPodPresent2", ""),
        ("2025-11-18 19:58:01.751927", "Clamped", "SMIFPodClamped2", ""),
        ("2025-11-18 19:58:39.210996", "Mounted", "MaterialReceived", ""),
        ("2025-11-18 20:00:54.748808", "Lot_Start", "LotStarted", "TEST20251118_33"),
        ("2025-11-18 20:01:05.725535", "Wfr_Start", "WaferStarted", "TEST20251118_33"),
        ("2025-11-18 20:02:44.994497", "Lot_End", "LotEnded", "TEST20251118_33"),
        ("2025-11-18 20:05:31.037108", "Unloaded", "SMIFPodAbsent2", "TEST20251118_33"),
    ]
    written = []
    for dt, tool_event, raw_event, lot_id in raw_events:
        event = mapper.from_secs_event(
            machine,
            0,
            {
                "DATETIME": dt,
                "TOOL_EVENT": tool_event,
                "SECSGEM_RAW_EVENT": raw_event,
                "LOAD_PORT": 2,
                "LOT_ID": lot_id,
                "WAFER_ID": "01",
                "RECIPE": "Met_Etch_ANISO_Rcp1",
            },
        )
        written.extend(writer.append(machine, profile, event))

    local_files = sorted((tmp_path / "local").glob("*.csv"))
    assert len(local_files) == 1
    # The network copy is deferred to CsvMirrorWorker: _write_buffer runs
    # inside the S6F11 acknowledgement path, and a copy to a sick share
    # blocks long enough to push the tool past T3. Drive the worker's pass
    # explicitly here, which is exactly what the service's thread does.
    assert not sorted((tmp_path / "network").glob("*.csv")), (
        "the mirror must not be copied on the acknowledgement thread"
    )
    assert writer.retry_mirrors() == 1
    network_files = sorted((tmp_path / "network").glob("*.csv"))
    assert len(network_files) == 1
    assert local_files[0].name.startswith("SPTS_fxP_OMEGA_01_Lot_20251118_195752_173086")

    with local_files[0].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == [
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
    assert rows[1][1] == "Loaded"
    assert rows[-1][1] == "Unloaded"
    assert rows[-1][-1] == "SMIFPodAbsent2"


def test_linkstuffs_payloads_and_outbox(tmp_path):
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    publisher = LinkstuffsGatewayPublisher(
        config=type(
            "Config",
            (),
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 1883,
                "access_token": "token",
                "tls": False,
                "qos": 1,
                "client_id": "test",
                "keepalive_sec": 60,
                "publish_retain": False,
            },
        )(),
        outbox=outbox,
    )
    publisher.queue_machine_connect(machine)
    publisher.queue_machine_attributes(machine, profile)
    event = CanonicalMapper(profile).from_secs_event(
        machine,
        0,
        {
            "DATETIME": "2025-11-18 20:00:54.748808",
            "TOOL_EVENT": "Lot_Start",
            "SECSGEM_RAW_EVENT": "LotStarted",
            "LOT_ID": "LOT1",
        },
    )
    publisher.queue_event(event)

    pending = outbox.pending(limit=10)
    topics = [item.topic for item in pending]
    assert topics == [LINKSTUFFS_TOPIC_CONNECT, LINKSTUFFS_TOPIC_ATTRIBUTES, LINKSTUFFS_TOPIC_TELEMETRY]
    assert pending[0].payload == {"device": "SPTS_fxP_OMEGA_01", "type": "spts_fxp_omega"}
    telemetry = pending[2].payload["SPTS_fxP_OMEGA_01"][0]
    assert telemetry["values"]["event_type"] == "lot_start"
    assert telemetry["values"]["lot_id"] == "LOT1"
