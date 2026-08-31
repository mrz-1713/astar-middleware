"""Smoke test: exercise mapper + CSV + encrypted legacy payload end-to-end
for each machine profile (SPTS Omega, DaVinci, PTIQ, NexGen MG).

This is the dry run we can do without a SECS host or MQTT broker - it proves
the per-vendor CEID/SVID maps resolve correctly, the per-lot CSV writer opens
and closes files at the right events, and the encrypted legacy payload
round-trips through explicitly supplied test keys.
"""

from __future__ import annotations

import csv

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.legacy_api import build_legacy_api_payload
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.secure_payload import SecurePayloadCodec


TEST_FIRST_KEY = "three-vendor-test-first-key-not-for-production"
TEST_SECOND_KEY = "three-vendor-test-second-key-not-for-production"


def _machine(tmp_path, display, profile_id):
    return MachineConfig(
        endpoint_id=f"TOOL_{display}",
        display_name=display,
        machine_profile=profile_id,
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / display / "local"),
        network_csv_path=str(tmp_path / display / "network"),
        admin_config_path=str(tmp_path / display / "admin"),
    )


# ----- SPTS Omega -----

def test_spts_omega_end_to_end(tmp_path):
    machine = _machine(tmp_path, "SPTS_fxP_OMEGA_01", "spts_fxp_omega")
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    sequence = [
        # (CEID, raw_event_in_data, expected_event_type, ToolEvent expected)
        (722, "SMIFPodPresent2", "loaded", "Loaded"),
        (742, "SMIFPodClamped2", "clamped", "Clamped"),
        (3, "MaterialReceived", "mounted", "Mounted"),
        (851, "CassetteStarted", "lot_start", "Lot_Start"),
        (859, "WaferStarted", "wafer_start", "Wfr_Start"),
        (855, "ProcessingStarted", "process_start", "Proc_Start"),
        (856, "ProcessingFinished", "process_end", "Proc_End"),
        (860, "WaferComplete", "wafer_end", "Wfr_End"),
        (852, "CassetteComplete", "lot_end", "Lot_End"),
        (4, "MaterialRemoved", "unmounted", "UnMounted"),
        (752, "SMIFPodUnClamped2", "unclamped", "UnClamped"),
        (732, "SMIFPodAbsent2", "unloaded", "Unloaded"),
    ]

    base_dt = "2025-11-28 09:46:59.345559"
    for i, (ceid, raw, _, _) in enumerate(sequence):
        event = mapper.from_secs_event(
            machine,
            ceid,
            {
                "DATETIME": base_dt,
                "SECSGEM_RAW_EVENT": raw,
                "LOAD_PORT": 2,
                "LOT_ID": "TEST-LOT-001",
                "WAFER_ID": f"W{i+1:02d}",
                "RECIPE": "RcpA",
            },
        )
        # Mapper resolved expected canonical type
        idx = i
        expected_type = sequence[idx][2]
        expected_tool = sequence[idx][3]
        assert event.event_type == expected_type, (raw, event.event_type)
        # CSV writer accepts every step
        writer.append(machine, profile, event)
        # Legacy payload builds without errors and contains the tool event
        payload = build_legacy_api_payload(event, profile, token_id="tok")
        assert payload["ToolEvent"] == expected_tool
        assert payload["EAP_ToolName"] == "SPTS_fxP_OMEGA_01"

    # One CSV file per lot, closed on unload
    local_files = sorted((tmp_path / "SPTS_fxP_OMEGA_01" / "local").glob("*.csv"))
    assert len(local_files) == 1, [f.name for f in local_files]
    with local_files[0].open(newline="", encoding="utf-8") as h:
        rows = list(csv.reader(h))
    # Header + 12 events
    assert len(rows) == 13
    assert rows[0][0] == "Datetime"
    assert rows[-1][1] == "Unloaded"


# ----- DaVinci (MueTec) -----

def test_davinci_end_to_end(tmp_path):
    machine = _machine(tmp_path, "DAVINCI200_MC4_HC1_01", "davinci_200_mc4_hc1")
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    # CEIDs sourced from the SECS-Items_MueTec DaVinci 200 MC4_HC1 workbook
    sequence = [
        (3160001, "LP1/CarrierArrived", "loaded"),
        (3210002, "CarrierClamped", "clamped"),
        (3050001, "MaterialReceived", "mounted"),
        (3200017, "ControlJob:Selected-Executing", "lot_start"),
        (3220013, "NeedsProcessing2InProcess", "wafer_start"),
        (3140002, "PM1/ProcessingStarted", "process_start"),
        (3140003, "PM1/ProcessingFinished", "process_end"),
        (3220016, "InProcess2Processed", "wafer_end"),
        (3200002, "ControlJob:Executing-Completed", "lot_end"),
        (3050002, "MaterialRemoved", "unmounted"),
        (3160002, "LP1/CarrierDeparted", "unloaded"),  # closes lot file
    ]

    for i, (ceid, raw, expected_type) in enumerate(sequence):
        event = mapper.from_secs_event(
            machine,
            ceid,
            {
                "DATETIME": "2025-11-28 10:00:00.000000",
                "SECSGEM_RAW_EVENT": raw,
                "LotID": "DAV-LOT-001",
                "WaferID": f"S{i+1:02d}",
                "RecipeName": "DaVinci_Rcp_001",
                "LoadPort": "LP1",
            },
        )
        assert event.event_type == expected_type, (raw, event.event_type)
        # The CEID alias must round-trip through the profile
        assert profile.ceid_aliases.get(ceid) is not None
        writer.append(machine, profile, event)

    local_files = sorted((tmp_path / "DAVINCI200_MC4_HC1_01" / "local").glob("*.csv"))
    assert len(local_files) == 1
    # MachineName is ECID 4030003 (not an SV), so it must not appear in svids_by_name
    assert "MachineName" not in profile.svids_by_name
    assert profile.svids_by_name["PM1/RecipeName"] == 1060007
    assert profile.svids_by_name["ControlState"] == 1010001


# ----- PTIQ (generic Cimetrix GEM) -----

def test_ptiq_end_to_end(tmp_path):
    machine = _machine(tmp_path, "PTIQ_01", "ptiq_secsgem")
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    # PTIQ is generic: events are resolved by NAME (the per-equipment EIB picks
    # the actual CEID numbers). The mapper still resolves them correctly.
    sequence = [
        ("CarrierArrived", "loaded"),
        ("MaterialReceived", "mounted"),
        ("SCH1.LotStarted", "lot_start"),
        ("ProcessingStarted", "process_start"),
        ("ProcessingCompleted", "process_end"),
        ("SCH1.LotComplete", "lot_end"),
        ("MaterialRemoved", "unmounted"),
        ("CarrierDeparted", "unloaded"),  # closes the lot file
    ]

    for raw, expected in sequence:
        event = mapper.from_secs_event(
            machine,
            0,  # CEID unknown / left to EIB
            {
                "DATETIME": "2025-11-28 11:30:00.000000",
                "SECSGEM_RAW_EVENT": raw,
                "LotID": "PTIQ-LOT-9",
                "Recipe": "PTIQ_BaseFlow",
            },
        )
        assert event.event_type == expected, (raw, event.event_type)
        writer.append(machine, profile, event)

    local_files = sorted((tmp_path / "PTIQ_01" / "local").glob("*.csv"))
    assert len(local_files) == 1


# ----- NexGen MG Series -----
#
# The MG profile was transcribed from a PDF and has never met hardware, so
# these cases feed realistic POSITIONAL report payloads - the V[] array in the
# exact VID order the generated EventSubscription.json asks for - and assert on
# the resulting CSV columns. Asserting a constant equals itself would prove
# nothing; what needs protecting is that the transcribed layout decodes.

MG_PROFILE_ID = "nexgen_mg_series"


def _mg_report(values):
    """One S6F11 as the host parser delivers it: raw positional V[] only."""
    return {"DATETIME": "2026-08-11 08:00:00.000000", "_v_raw": list(values)}


def _mg_identity(lot, recipe, port, slot, substrate="", carrier="CAR_1",
                 job="JOB_1", with_substrate=True):
    """V[] for a process-module event, in report VID order."""
    values = [substrate] if with_substrate else []
    return values + [lot, recipe, port, slot, carrier, job, slot, port]


def _mg_rows(tmp_path, display):
    files = sorted((tmp_path / display / "local").glob("*.csv"))
    out = []
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            out.append((path.name, list(csv.reader(handle))))
    return out


def test_mg_full_lot_lifecycle_on_one_port(tmp_path):
    """Placed -> mapped -> started -> wafers -> ready-to-unload -> removed."""
    display = "NEXGEN_MG_01"
    machine = _machine(tmp_path, display, MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    steps = [
        (130, []),                                   # port1CasPlaced
        (140, [25]),                                 # port1CasMapped
        (150, [25, 1, "2026-08-11", "08:00:00"]),    # processingStartedPort1
        (212, _mg_identity("LOT_A", "MG_CLEAN_01", 1, 1, "LOT_A.01")),
        (213, _mg_identity("LOT_A", "MG_CLEAN_01", 1, 1, "LOT_A.01")),
        (124, [25, 120, 95]),                        # port1ReadyToUnload
        (134, []),                                   # port1CasRemoved -> closes
    ]
    for ceid, values in steps:
        for event in mapper.from_secs_events(machine, ceid, _mg_report(values)):
            writer.append(machine, profile, event)

    files = _mg_rows(tmp_path, display)
    assert len(files) == 1, [name for name, _ in files]
    name, rows = files[0]
    assert "_LP1.csv" in name, name
    header, *body = rows
    assert header == [
        "Datetime", "ToolEvent", "EAP_ToolName", "LoadPort", "Chamber",
        "LotID", "WaferID", "Recipe", "SECSGEM_Raw_Event",
    ]
    assert [row[1] for row in body] == [
        "Loaded", "Mapped", "Lot_Start", "Wfr_Start", "Wfr_End",
        "Lot_End", "Unloaded",
    ]
    # Every row belongs to load port 1, and the wafer rows carry the identity
    # block the process module reported.
    assert {row[3] for row in body} == {"1"}
    wafer_started = body[3]
    assert wafer_started[4] == "PM1"                  # Chamber
    assert wafer_started[5] == "LOT_A"                # LotID
    assert wafer_started[6] == "LOT_A.01"             # WaferID (substrate ID)
    assert wafer_started[7] == "MG_CLEAN_01"          # Recipe
    assert wafer_started[8] == "pm1WaferStarted"


def test_mg_two_ports_run_concurrently_without_interleaving(tmp_path):
    """Two lots, two ports, two process modules, events interleaved.

    This is the case that breaks attribution-by-inference. The MG reports the
    originating load port inside every process-module event, so each lot must
    land in its own file even though PM2's wafers are fed from port 2 while
    PM1's are fed from port 1.
    """
    display = "NEXGEN_MG_01"
    machine = _machine(tmp_path, display, MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    interleaved = [
        (130, []), (131, []),
        (140, [25]), (141, [13]),
        (150, [25, 1, "2026-08-11", "08:00:00"]),
        (151, [13, 2, "2026-08-11", "08:00:05"]),
        # PM1 runs port 1's wafer while PM2 runs port 2's, alternating.
        (212, _mg_identity("LOT_A", "RCP_A", 1, 1, "LOT_A.01")),
        (312, _mg_identity("LOT_B", "RCP_B", 2, 1, "LOT_B.01")),
        (213, _mg_identity("LOT_A", "RCP_A", 1, 1, "LOT_A.01")),
        (313, _mg_identity("LOT_B", "RCP_B", 2, 1, "LOT_B.01")),
        (124, [25, 120, 95]), (125, [13, 90, 70]),
        (134, []), (135, []),
    ]
    for ceid, values in interleaved:
        for event in mapper.from_secs_events(machine, ceid, _mg_report(values)):
            writer.append(machine, profile, event)

    files = _mg_rows(tmp_path, display)
    assert len(files) == 2, [name for name, _ in files]
    by_port = {}
    for name, rows in files:
        body = rows[1:]
        ports = {row[3] for row in body}
        assert len(ports) == 1, (name, ports)
        by_port[ports.pop()] = body

    assert set(by_port) == {"1", "2"}
    # No row interleaving: each file holds exactly one lot.
    assert {row[5] for row in by_port["1"] if row[5]} == {"LOT_A"}
    assert {row[5] for row in by_port["2"] if row[5]} == {"LOT_B"}
    # Chamber distinguishes the two process modules.
    assert {row[4] for row in by_port["1"] if row[4] != "NA"} == {"PM1"}
    assert {row[4] for row in by_port["2"] if row[4] != "NA"} == {"PM2"}


def test_mg_wafer_from_port_two_processed_in_pm2_keeps_its_port(tmp_path):
    """A wafer fed from port 2 into PM2 is attributed to port 2, chamber PM2."""
    machine = _machine(tmp_path, "NEXGEN_MG_01", MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    event = CanonicalMapper(profile).from_secs_event(
        machine, 313,
        _mg_report(_mg_identity("LOT_B", "RCP_B", 2, 7, "LOT_B.07")),
    )
    assert event.load_port == "2"
    assert event.chamber == "PM2"
    assert event.event_type == "wafer_end"
    assert event.lot_id == "LOT_B"
    assert event.wafer_id == "LOT_B.07"


def test_mg_wafer_id_prefers_substrate_id_then_falls_back_to_slot(tmp_path):
    """GEM300 tools give a substrate ID; cassette tools give the slot number.

    Both come out of the canonical mapper's existing key precedence - the
    substrate ID occupies the key it already prefers and the load slot a
    lower-priority one, so there is no branching and no invented composite id.
    """
    machine = _machine(tmp_path, "NEXGEN_MG_01", MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    mapper = CanonicalMapper(profile)

    gem300 = mapper.from_secs_event(
        machine, 212,
        _mg_report(_mg_identity("LOT_A", "R", 1, 4, "SUBST-9911")),
    )
    assert gem300.wafer_id == "SUBST-9911"

    cassette = mapper.from_secs_event(
        machine, 212,
        _mg_report(_mg_identity("LOT_A", "R", 1, 4, "")),
    )
    assert cassette.wafer_id == "4", "no substrate ID -> bare cassette slot"

    # CEIDs where the manual marks no substrate ID valid have no slot for it
    # at all, so they degrade to the slot number too.
    stopping = mapper.from_secs_event(
        machine, 214,
        _mg_report(
            _mg_identity("LOT_A", "R", 1, 6, with_substrate=False)),
    )
    assert stopping.wafer_id == "6"


def test_mg_carrier_and_job_survive_in_the_raw_payload(tmp_path):
    """No CSV column, but nothing the tool reported is discarded."""
    machine = _machine(tmp_path, "NEXGEN_MG_01", MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    event = CanonicalMapper(profile).from_secs_event(
        machine, 212,
        _mg_report(_mg_identity(
            "LOT_A", "R", 1, 3, "S1", carrier="CAR_77", job="JOB_88")),
    )
    values = event.telemetry_values()
    assert values["raw_CarrierID"] == "CAR_77"
    assert values["raw_JobID"] == "JOB_88"
    assert values["raw_UnloadPort"] == 1


def test_mg_unknown_ceid_is_readable_not_dropped(tmp_path):
    """A constant that changed since publication must show up in the data."""
    display = "NEXGEN_MG_01"
    machine = _machine(tmp_path, display, MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    # A CEID that does not exist in the manual at all resolves to a readable
    # fallback naming the number, so the changed constant is visible.
    unknown = mapper.from_secs_event(machine, 9999, {"_v_raw": []})
    assert unknown.event_type == "unknown"
    assert unknown.raw_event_name == "CEID_9999"
    assert unknown.secs_raw_event == "CEID_9999"
    assert unknown.telemetry_values()["ceid"] == 9999

    # And it reaches the per-lot CSV rather than being filtered out, whenever
    # the payload lets it be attributed to a lot.
    for ceid, values in ((130, []), (150, [25, 1, "2026-08-11", "08:00:00"]),
                         (212, _mg_identity("LOT_A", "R", 1, 1, "S1"))):
        writer.append(machine, profile,
                      mapper.from_secs_event(machine, ceid, _mg_report(values)))
    attributable = mapper.from_secs_event(
        machine, 9999,
        {"DATETIME": "2026-08-11 08:00:01.000000", "LotID": "LOT_A", "PortID": 1},
    )
    assert attributable.event_type == "unknown"
    writer.append(machine, profile, attributable)
    writer.flush_all(reason="test")

    rows = _mg_rows(tmp_path, display)[0][1]
    assert any(row[8] == "CEID_9999" for row in rows[1:]), rows


def test_mg_process_state_decodes_as_integer_and_as_ascii(tmp_path):
    """The manual calls ProcessState a one-byte integer in one section and
    ASCII in another. Whichever the tool sends must reach telemetry intact."""
    machine = _machine(tmp_path, "NEXGEN_MG_01", MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    mapper = CanonicalMapper(profile)
    process_state_svid = profile.svids_by_name["ProcessState"]

    as_int = mapper.svid_event(machine, {process_state_svid: 9})
    as_ascii = mapper.svid_event(machine, {process_state_svid: "9"})
    assert as_int.raw_payload["svid_ProcessState"] == 9
    assert as_ascii.raw_payload["svid_ProcessState"] == "9"


def test_mg_profile_records_its_provenance_and_attributes_by_chamber():
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    assert profile.vendor == "NexGen Wafersystems"
    assert "V1.1.18" in profile.notes
    assert "NOT HARDWARE-VERIFIED" in profile.notes
    # The wafer-level PM reports carry pmNCurrWaferLoadPort, but the step
    # family and the rest of the chamber band link no report at all, so they
    # are resolved against the chamber binding those reports leave behind.
    assert 223 in profile.chamber_event_ceids       # pm1MediumStepFinished
    assert 323 in profile.chamber_event_ceids       # pm2MediumStepFinished
    # The events that DO state their own port are not in the set: they need
    # no inference and must never be overridden by it.
    assert 212 not in profile.chamber_event_ceids   # pm1WaferStarted
    assert 213 not in profile.chamber_event_ceids   # pm1WaferFinished
    # All four load ports drive activation, via the payload-resolved port
    # rather than a per-port transition tag (only ports 1 and 2 have one).
    assert set(profile.ceid_state_transitions) == {130, 131, 132, 133,
                                                   134, 135, 136, 137}
    # AlarmsSet and the spool variables are documented "not supported"; their
    # absence is what drives the alarm-state-unknown signal and disables the
    # spool-backlog check.
    assert profile.resolve_svid_name("AlarmsSet") is None
    assert profile.health_spool_count_svid is None
    assert profile.health_last_event_svid == 16


# ----- Encryption round-trip with explicit test keys -----

def test_encrypted_payload_round_trips_with_explicit_keys(tmp_path):
    """Build a legacy payload from a real event, encrypt with explicit keys,
    decrypt with the same codec - prove the wire format works end-to-end."""
    machine = _machine(tmp_path, "SPTS_fxP_OMEGA_01", "spts_fxp_omega")
    profile = ProfileRegistry().get(machine.machine_profile)
    event = CanonicalMapper(profile).from_secs_event(
        machine,
        851,
        {
            "DATETIME": "2025-11-28 09:46:59.345559",
            "SECSGEM_RAW_EVENT": "CassetteStarted",
            "LOAD_PORT": 1,
            "LOT_ID": "25110302-08 rap si",
        },
    )
    payload = build_legacy_api_payload(event, profile, token_id="skd29f-kd204j")
    codec = SecurePayloadCodec.from_raw_keys(
        TEST_FIRST_KEY,
        TEST_SECOND_KEY,
    )
    ciphertext = codec.encrypt_json(payload)
    decoded = codec.decrypt_json(ciphertext)

    assert decoded == payload
    assert decoded["ToolEvent"] == "Lot_Start"
    assert decoded["EAP_ToolName"] == "SPTS_fxP_OMEGA_01"
    assert decoded["TokenID"] == "skd29f-kd204j"
    # SPTS lot_start events normalize to "LotStarted" in the wire payload
    # (matches the unencrypted sample in the project spec).
    assert decoded["SECSGEM_Raw_Event"] == "LotStarted"


def test_mg_step_events_are_attributed_to_the_chamber_that_ran_them(tmp_path):
    """Two lots, two chambers, one tool.

    The step family (222-231, 322-331) links no report, so it arrives with no
    load port. The wafer-level reports that precede it do carry
    pmNCurrWaferLoadPort, and that pairing is what these are resolved against.
    """
    from eap_middleware.job_tracker import JobTracker

    machine = _machine(tmp_path, "MG_01", MG_PROFILE_ID)
    profile = ProfileRegistry().get(MG_PROFILE_ID)
    tracker = JobTracker()
    mapper = CanonicalMapper(profile, tracker=tracker)

    def fire(ceid, payload=None):
        return mapper.from_secs_events(machine, ceid, payload or {})[0]

    fire(130)   # cassette placed on load port 1
    fire(132)   # cassette placed on load port 3
    fire(212, {"WaferID": "W01", "LotID": "LOT-A", "PortID": "1"})
    fire(312, {"WaferID": "W55", "LotID": "LOT-B", "PortID": "3"})

    # No single load port is "the active one" here, which is exactly the case
    # a machine-wide guess gets wrong.
    assert tracker.snapshot(machine.endpoint_id)["active_lp"] is None

    for ceid in (220, 223, 225, 227):
        event = fire(ceid)
        assert event.chamber == "PM1"
        assert event.load_port == "1", (ceid, event.load_port)
    for ceid in (320, 323, 325, 327):
        event = fire(ceid)
        assert event.chamber == "PM2"
        assert event.load_port == "3", (ceid, event.load_port)

    # The cassette leaves LP1. PM1 must go quiet rather than inherit LP3.
    fire(134)
    assert fire(223).load_port == ""
    assert fire(323).load_port == "3"
