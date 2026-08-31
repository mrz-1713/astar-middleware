"""Regressions for the 2026-08-19 vendor-conformance audit.

Each test names the vendor document section or the failure mode it pins, so a
later change that reintroduces the defect fails with the reason attached rather
than with a bare assertion.

See docs/VENDOR_CONFORMANCE_AUDIT_2026-08-19.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from eap_middleware.config import ConfigError, machine_from_dict
from eap_middleware.control import leading_comment_block, save_config_atomic
from eap_middleware.csv_store import (
    _RECENT_ERRORS_MAX,
    _RECENT_FILES_MAX,
    PerLotCsvWriter,
)
from eap_middleware.job_tracker import MAX_LOT_IDS, MAX_WAFER_IDS, JobTracker
from eap_middleware.journal import MIRROR_BATCH_LIMIT, IngressJournal
from eap_middleware.models import CanonicalEvent, MachineStorageConfig
from eap_middleware.profiles import ProfileRegistry

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# M-1  NexGen MG process metrics (manual section 8.2, "CEID" column)
# --------------------------------------------------------------------------

def _mg_declared_pairs():
    """(DV, CEID) validity pairs the MG manual declares, from the profile."""
    from eap_middleware.profiles import NEXGEN_MG_METRIC_REPORTS
    return NEXGEN_MG_METRIC_REPORTS


def test_mg_step_finished_events_carry_their_documented_metrics():
    """Every medium / DI / N2-dry / DiwO3 step-finished event used to link an
    empty report, so the tool said "a step finished" and nothing else. The
    manual gives each of them a data-variable set (section 8.2, and the figure
    on page 20 for 223 / 225 / 227)."""
    sub = json.loads(
        (ROOT / "output" / "nexgen_mg_series" / "EventSubscription.json").read_text()
    )
    reports = {r["rptid"]: set(r.get("dvids", [])) for r in sub["reports"]}
    delivered = {
        int(e["ceid"]): set().union(
            *[reports.get(r, set()) for r in e.get("rptids", [])] or [set()]
        )
        for e in sub["events"]
    }
    for ceid in (223, 225, 227, 229, 323, 325, 327, 329):
        assert delivered[ceid], f"CEID {ceid} is back to an empty report"

    # The page-20 figure names these exactly; they are the load-bearing ones.
    assert {1100, 1101, 1102, 1150, 1151, 1152, 1130, 1131, 1132} <= delivered[223]
    assert {1100, 1101, 1102, 1160, 1161, 1162} <= delivered[225]
    assert {1100, 1101, 1102, 1103, 1104, 1105} <= delivered[227]


def test_mg_metric_bands_are_isolated_from_the_lifecycle_bands():
    """S2F33 is all-or-nothing: one VID an MG variant does not implement
    returns DRACK=4 and rejects the whole message. The chemistry families must
    therefore never share a band with the wafer-start/finish lifecycle, or a
    tool without CO2 would lose its lot files to gain nothing."""
    sub = json.loads(
        (ROOT / "output" / "nexgen_mg_series" / "EventSubscription.json").read_text()
    )
    band = {int(e["ceid"]): e["band"] for e in sub["events"]}
    lifecycle = band[212]                      # pm1WaferStarted
    for ceid in (223, 225, 227, 229, 510, 511, 515, 519, 523, 531):
        assert band[ceid] != lifecycle, (
            f"CEID {ceid} shares the lifecycle band; a refused chemistry VID "
            f"would take pm1WaferStarted down with it"
        )
    # Each chemistry family is independently refusable.
    assert band[225] != band[223], "DI and medium steps must fail separately"
    assert band[515] != band[519], "HPC and BEM must fail separately"


def test_mg_slot_map_encodings_are_not_confusable():
    """The manual defines two slot-map encodings in which 3 means opposite
    things: CROSSSLOTTED in SVID 3110/4306, CORRECTLY OCCUPIED in the E87
    carrier attribute (section 6.3). They must not share a field name."""
    profile = ProfileRegistry().get("nexgen_mg_series")
    assert profile.svids_by_name["SlotMapGem"] == 4306
    assert profile.dvs_by_name["SlotMap"] == 2093
    assert profile.ceid_dv_layout[145] == ("PortID", "SlotMapGem")


def test_mg_manual_misnames_three_pm2_bevel_etch_variables():
    """VIDs 2159-2161 are printed "pm1Bem..." but their CEID column says 521 =
    Pm2BemStepFinished. Renaming them is what keeps them from colliding with
    the real pm1 variables at 2144-2146 and being silently dropped."""
    profile = ProfileRegistry().get("nexgen_mg_series")
    dvs = profile.dvs_by_name
    assert dvs["pm1BemFlowMaxPrevStep"] == 2144
    assert dvs["pm2BemFlowMaxPrevStep"] == 2159
    for vid in (2144, 2145, 2146, 2159, 2160, 2161):
        assert vid in set(dvs.values()), f"VID {vid} lost to a name collision"


# --------------------------------------------------------------------------
# M-2  HCACK=4 is a success code (MG section 5.2, Omega section 15.2)
# --------------------------------------------------------------------------

def test_hcack_four_is_accepted_as_success():
    from gateway.host import HCACK_ACCEPTED

    assert 0 in HCACK_ACCEPTED
    assert 4 in HCACK_ACCEPTED, (
        "MG manual 5.2: '4 = Acknowledge, command will be performed with "
        "completion signaled later by an event'; the manual's own traces show "
        "S2F42 <B 04> for PPSELECT, MAP and START"
    )
    for refused in (1, 2, 3, 5):
        assert refused not in HCACK_ACCEPTED


# --------------------------------------------------------------------------
# M-7  Omega alarm ids decode to a module (manual section 8.3)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alid,station,station_type,offset",
    [
        (22400005, "Process Module 1", "Etch PM", 5),
        (22410005, "Process Module 1", "Etch PM", 5),      # ON CEID form
        (1022410005, "Process Module 1", "Etch PM", 5),    # OFF CEID form
    ],
)
def test_spts_alarm_id_decodes_to_its_module(alid, station, station_type, offset):
    from eap_middleware.spts_module_vids import decode_alarm_id

    identity = decode_alarm_id(alid)
    assert identity is not None
    assert identity.station_name == station
    assert identity.station_type_name == station_type
    assert identity.offset == offset


def test_spts_alarm_decode_declines_rather_than_guessing():
    from eap_middleware.spts_module_vids import decode_alarm_id

    # Station 9 is not in the manual's list (0-8 and 10 are).
    assert decode_alarm_id(92400005) is None
    assert decode_alarm_id("not a number") is None
    assert decode_alarm_id(-1) is None


# --------------------------------------------------------------------------
# M-11  Every documented way a DaVinci wafer leaves the flow
# --------------------------------------------------------------------------

def test_davinci_reports_stopped_rejected_lost_and_skipped_wafers():
    """Workbook sheet "Events", CEIDs 3220018-3220023. Without them a wafer
    that was rejected or lost produced no event at all, and the lot file just
    had one fewer row than the cassette had wafers."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    outcomes = {
        3220018: "WaferStopped",
        3220019: "WaferRejected",
        3220020: "WaferLost",
        3220021: "WaferSkipped",
        3220022: "WaferLost",
        3220023: "WaferSkipped",
    }
    for ceid, raw_name in outcomes.items():
        mapping = profile.resolve_event(ceid=ceid)
        assert mapping.event_type == "wafer_end", ceid
        assert mapping.secs_raw_event == raw_name, ceid

    sub = json.loads(
        (ROOT / "output" / "davinci200_mc4_hc1" / "EventSubscription.json").read_text()
    )
    subscribed = {int(e["ceid"]) for e in sub["events"]}
    assert set(outcomes) <= subscribed, "outcomes are mapped but not subscribed"


# --------------------------------------------------------------------------
# F-1 / F-8  Mirror queue: batched, backed off, leased
# --------------------------------------------------------------------------

def test_mirror_queue_hands_out_a_bounded_batch(tmp_path):
    journal = IngressJournal(tmp_path / "j.sqlite3")
    source = tmp_path / "lot.csv"
    source.write_text("a\n")
    for index in range(MIRROR_BATCH_LIMIT * 3):
        journal.enqueue_mirror(source, tmp_path / "share" / f"{index}.csv")

    batch = journal.pending_mirrors()
    assert len(batch) == MIRROR_BATCH_LIMIT, (
        "an unbounded batch is what let one unreachable share block the "
        "caller for the OS timeout times the whole queue"
    )
    # Leased: a second caller gets different work, not the same rows again.
    assert not {t.id for t in batch} & {t.id for t in journal.pending_mirrors()}


def test_a_failed_mirror_backs_off_instead_of_retrying_immediately(tmp_path):
    journal = IngressJournal(tmp_path / "j.sqlite3")
    source = tmp_path / "lot.csv"
    source.write_text("a\n")
    journal.enqueue_mirror(source, tmp_path / "share" / "lot.csv")

    task = journal.pending_mirrors()[0]
    journal.fail_mirror(task.id, "The specified network name is no longer available")
    assert journal.pending_mirrors() == [], "a just-failed task must not be due"


def test_mirror_retry_does_not_run_on_the_supervisor_thread():
    """The supervisor also reloads config, drains the command inbox, replays
    the journal and writes runtime_status.json. A blocking SMB copy on that
    thread makes the panel report the service dead."""
    source = (ROOT / "eap_middleware" / "service" / "control_plane.py").read_text()
    supervisor = source[source.index("def _start_supervisor"):source.index("def _start_mirror_worker")]
    assert "retry_mirrors" not in supervisor
    assert "def _start_mirror_worker" in source


# --------------------------------------------------------------------------
# F-4  A CSV failure is not a publish failure
# --------------------------------------------------------------------------

def test_a_csv_write_failure_is_recorded_against_the_csv_sink(tmp_path):
    journal = IngressJournal(tmp_path / "j.sqlite3")
    entry, _ = journal.append(
        endpoint_id="E1", kind="event", stream=6, function=11,
        ceid=4, payload={"x": 1},
    )
    journal.mark_dispatched(entry.seq)
    journal.mark_csv_failed(entry.seq, "disk full")

    fresh = journal.entry(entry.seq)
    assert fresh is not None
    assert fresh.dispatch_status == "done", (
        "a CSV failure used to overwrite a publish that had already succeeded"
    )
    assert fresh.csv_status == "pending", "still replayable for the CSV sink"


# --------------------------------------------------------------------------
# F-5  display_name reaches the filesystem
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["TOOL:1", "LINE-A/TOOL_1", "LINE\\TOOL", "TOOL*1", "TOOL|1", "..", "A B"]
)
def test_a_display_name_that_cannot_be_a_filename_is_refused(name):
    with pytest.raises(ConfigError) as excinfo:
        machine_from_dict(
            {
                "endpoint_id": "E1", "display_name": name,
                "machine_profile": "nexgen_mg_series",
                "host": "127.0.0.1", "port": 5000,
            },
            ProfileRegistry(),
        )
    assert "display_name" in str(excinfo.value)


def test_an_ordinary_display_name_still_writes_a_lot_file(tmp_path):
    machine = machine_from_dict(
        {
            "endpoint_id": "E1", "display_name": "NEXGEN_MG-01.a",
            "machine_profile": "nexgen_mg_series",
            "host": "127.0.0.1", "port": 5000,
        },
        ProfileRegistry(),
    )
    machine = replace(
        machine, storage=MachineStorageConfig(local_csv_path=str(tmp_path))
    )
    writer = PerLotCsvWriter()
    profile = ProfileRegistry().get("nexgen_mg_series")
    writer.append(machine, profile, CanonicalEvent(
        timestamp=datetime.now(timezone.utc), endpoint_id="E1",
        display_name=machine.display_name, machine_profile="nexgen_mg_series",
        vendor="v", model="m", event_type="lot_start",
        raw_event_name="ProcessingStarted", ceid=4, load_port="1",
        chamber="NA", lot_id="LOT1", wafer_id="", recipe="R",
        secs_raw_event="ProcessingStarted",
    ))
    written = writer.flush_all("test")
    assert written and written[0].exists()
    assert written[0].parent == tmp_path


# --------------------------------------------------------------------------
# F-6 / F-7  Nothing grows for the life of the process
# --------------------------------------------------------------------------

def test_job_tracker_identifier_maps_are_bounded():
    """`ptiq_secsgem` declares no state transitions, so nothing ever prunes;
    on the other profiles a refused load-port band has the same effect."""
    profile = ProfileRegistry().get("ptiq_secsgem")
    tracker = JobTracker()
    for index in range(MAX_WAFER_IDS * 2):
        tracker.note_event("M1", profile, 1001, {
            "PortID": "1", "WaferID": f"W{index}", "LotID": f"LOT{index // 25}",
        })
    state = tracker.snapshot("M1")
    assert len(state["wafer_ports"]) == MAX_WAFER_IDS
    assert len(state["lot_ports"]) <= MAX_LOT_IDS
    # Most recent survives; oldest is the one evicted.
    assert f"W{MAX_WAFER_IDS * 2 - 1}" in state["wafer_ports"]
    assert "W0" not in state["wafer_ports"]


def test_csv_writer_diagnostic_lists_are_bounded():
    writer = PerLotCsvWriter()
    for index in range(_RECENT_FILES_MAX + 50):
        writer.written_files.append(Path(f"/tmp/lot{index}.csv"))
    for index in range(_RECENT_ERRORS_MAX + 50):
        writer.mirror_errors.append(f"error {index}")
    assert len(writer.written_files) == _RECENT_FILES_MAX
    assert len(writer.mirror_errors) == _RECENT_ERRORS_MAX


# --------------------------------------------------------------------------
# F-9  Saving from the panel keeps the file's own documentation
# --------------------------------------------------------------------------

def test_saving_the_config_keeps_its_comment_header(tmp_path):
    target = tmp_path / "production.yaml"
    target.write_text(
        "# ASTAR middleware configuration\n"
        "# request_online is ON for the MG per manual section 3.2.\n"
        "\n"
        "service:\n  poll: 5\n",
        encoding="utf-8",
    )
    save_config_atomic(target, {"service": {"poll": 9}})
    saved = target.read_text(encoding="utf-8")
    assert "manual section 3.2" in saved, "the panel used to erase every comment"
    assert "poll: 9" in saved


def test_leading_comment_block_stops_at_the_first_setting():
    assert leading_comment_block("# a\n# b\n\nkey: 1\n# trailing\n") == "# a\n# b"
    assert leading_comment_block("key: 1\n") == ""


# --------------------------------------------------------------------------
# F-10  An inherited timer is a visible choice
# --------------------------------------------------------------------------

def test_every_profile_states_its_protocol_timers():
    registry = ProfileRegistry()
    for profile_id in registry.list_profile_ids():
        timers = registry.get(profile_id).hsms_timers
        assert set(timers) == {"t3", "t5", "t6", "t7", "t8"}, (
            f"{profile_id} leaves its timers implicit; an empty mapping still "
            f"resolves to the DaVinci's numbers, just invisibly"
        )
        for name, value in timers.items():
            assert 1 <= value <= 120, f"{profile_id}.{name}={value} out of E37 range"
