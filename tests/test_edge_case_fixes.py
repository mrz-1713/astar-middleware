"""Regression tests for the five blocker fixes from the edge-case audit.

Each test pins one specific bug class so future refactors can't silently
re-introduce it.
"""

from __future__ import annotations

import logging
from datetime import datetime


from eap_middleware.csv_store import CSV_EVENT_TYPES, PerLotCsvWriter
from eap_middleware.mapper import CanonicalMapper, _UNKNOWN_CEID_WARNED
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import ProfileRegistry


def _spts_machine(tmp_path) -> MachineConfig:
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


def _davinci_machine(tmp_path) -> MachineConfig:
    return MachineConfig(
        endpoint_id="DAV_01",
        display_name="DAVINCI_01",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
    )


# ----- Fix #1: unknown CEIDs are captured + warned, not silently dropped -----

def test_unknown_ceid_is_captured_and_warned_once(tmp_path, caplog):
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    _UNKNOWN_CEID_WARNED.clear()

    # Pick a CEID that's not in any vendor mapping
    bogus_ceid = 999999

    with caplog.at_level(logging.WARNING, logger="eap_middleware.mapper"):
        ev = mapper.from_secs_event(
            machine, bogus_ceid,
            {"DATETIME": "2025-11-28 10:00:00.000000", "LOT_ID": "X"},
        )
        # A second occurrence of the same CEID must NOT add a second warning
        mapper.from_secs_event(
            machine, bogus_ceid,
            {"DATETIME": "2025-11-28 10:00:01.000000", "LOT_ID": "X"},
        )

    assert ev.event_type == "unknown"
    # CSV writer accepts "unknown" so the row is captured, not dropped
    assert "unknown" in CSV_EVENT_TYPES
    warnings = [r for r in caplog.records if "Unknown CEID" in r.getMessage()]
    assert len(warnings) == 1, "expected exactly one dedup'd warning"


# ----- Fix #3: V[] is decoded per-CEID via the vendor's DV layout -----

def test_davinci_v_array_decodes_lot_and_recipe_for_processing_started(tmp_path):
    """PM1/ProcessingStarted (CEID 3140002) carries V = [WaferID, LotID, RecipeName].
    Mapper extracts by position."""
    machine = _davinci_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    # The host parser would produce this kind of payload for real equipment
    # (note: no LotID/WaferID/RecipeName keys - only the raw V list)
    data = {
        "DATETIME": "2025-11-28 10:00:00.000000",
        "_v_raw": ["WAFER_42", "LOT_ABC", "Recipe_MC4_001"],
    }
    ev = mapper.from_secs_event(machine, 3140002, data)

    assert ev.event_type == "process_start"
    assert ev.wafer_id == "WAFER_42"
    assert ev.lot_id == "LOT_ABC"
    assert ev.recipe == "Recipe_MC4_001"


def test_spts_v_array_decodes_cassette_started_payload(tmp_path):
    """SPTS CassetteStarted (CEID 851) carries V = [CassetteID, PortID, LotID]
    per Section 7 of the SPTS manual."""
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    data = {
        "DATETIME": "2025-11-28 10:00:00.000000",
        "_v_raw": ["CASSETTE_7", 1, "LOT_QQ"],
    }
    ev = mapper.from_secs_event(machine, 851, data)

    assert ev.event_type == "lot_start"
    assert ev.lot_id == "LOT_QQ"
    assert ev.load_port == "1"  # PortID from V[1]


def test_profile_layout_wins_over_eap_plan_positional_label(tmp_path):
    """v2 audit fix: when _v_raw is present AND a profile DV layout covers
    the CEID, the layout-decoded names win over the EAP-plan positional
    labels written by the host parser. The simulator-era contract (EAP-plan
    keys always win) was masking real-equipment payloads where V[3] is, say,
    ResultFile rather than LOAD_PORT. The presence of `_v_raw` is the signal
    that data came from the host parser (positional labels suspect)."""
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    # Host-parser shape: _v_raw present, LOT_ID labeled positionally
    data = {
        "DATETIME": "2025-11-28 10:00:00.000000",
        "LOT_ID": "FROM_HOST_POSITIONAL_LABEL",  # host parser's V[5] guess
        "_v_raw": ["CASSETTE_X", 2, "FROM_V_ARRAY_PROFILE_DECODE"],
    }
    ev = mapper.from_secs_event(machine, 851, data)
    # Profile layout for 851 is (CassetteID, PortID, LotID) so V[2]=LotID
    assert ev.lot_id == "FROM_V_ARRAY_PROFILE_DECODE"


def test_keyed_payload_without_v_raw_still_honored(tmp_path):
    """When no _v_raw is present, the data dict was hand-curated by a direct
    caller (test fixture, legacy path). EAP-plan keys ARE legitimate here -
    drop them only when we have positional contamination to suspect."""
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    data = {
        "DATETIME": "2025-11-28 10:00:00.000000",
        "LOT_ID": "HAND_CURATED",  # legitimate test fixture key
        # NO _v_raw
    }
    ev = mapper.from_secs_event(machine, 851, data)
    assert ev.lot_id == "HAND_CURATED"


# ----- Fix #4: load_port is inferred from the CEID when V[] omits it -----

def test_davinci_lp2_carrier_arrived_sets_load_port_2_when_payload_lacks_it(tmp_path):
    machine = _davinci_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    # LP2/CarrierArrived has empty Valid Variables
    ev = mapper.from_secs_event(
        machine, 3170001, {"DATETIME": "2025-11-28 10:00:00.000000"},
    )
    assert ev.event_type == "loaded"
    assert ev.load_port == "2", "LP2/* CEIDs must imply load_port=2"


def test_concurrent_lots_on_different_load_ports_get_separate_csv_buckets(tmp_path):
    """With Fix #4 the CEIDs whose name encodes the port (LP1/*, LP2/*) carry
    distinct load_port values, so the per-lot CSV writer keys them into
    separate buckets - they can't accidentally merge into a single ('TOOL',
    'NA') bucket the way they did before the fix.

    Known limitation (NOT fixed here): PM-chamber events like
    PM1/ProcessingStarted don't encode a load port, so cross-port routing of
    chamber events requires future stateful tracking of PRJob -> LP."""
    machine = _davinci_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    writer = PerLotCsvWriter()

    # Two carriers arrive on different load ports, each with its own LotID
    # carried in the LP-named event so a buffer can actually be created.
    for ceid, raw, port, lot in [
        (3160001, "LP1/CarrierArrived", "1", "LOT_LP1"),
        (3170001, "LP2/CarrierArrived", "2", "LOT_LP2"),
        (3160002, "LP1/CarrierDeparted", "1", "LOT_LP1"),
        (3170002, "LP2/CarrierDeparted", "2", "LOT_LP2"),
    ]:
        ev = mapper.from_secs_event(
            machine, ceid,
            {
                "DATETIME": "2025-11-28 10:00:00.000000",
                "SECSGEM_RAW_EVENT": raw,
                "LotID": lot,
                "_v_raw": ["CARRIER_X"],
            },
        )
        # The implicit load_port from Fix #4 must keep these in their own buckets
        assert ev.load_port == port
        writer.append(machine, profile, ev)

    csvs = list((tmp_path / "local").glob("*.csv"))
    assert len(csvs) == 2, [c.name for c in csvs]


# ----- Fix #6: SPTS 16-byte centisecond clock parses correctly -----

def test_spts_16_byte_centisecond_clock_parses_to_datetime(tmp_path):
    """SPTS Section 12.4 Clock format yyyymmddhhmmsscc (cc=centiseconds)."""
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    # 2025-11-28 09:46:59.34 (34 centisec -> 340000 microsec)
    ev = mapper.from_secs_event(
        machine, 851,
        {"DATETIME": "2025112809465934", "_v_raw": ["C", 1, "L"]},
    )
    assert ev.timestamp.year == 2025
    assert ev.timestamp.month == 11
    assert ev.timestamp.day == 28
    assert ev.timestamp.hour == 9
    assert ev.timestamp.minute == 46
    assert ev.timestamp.second == 59
    assert ev.timestamp.microsecond == 340_000


def test_davinci_14_byte_clock_parses(tmp_path):
    machine = _davinci_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)

    ev = mapper.from_secs_event(
        machine, 3140002,
        {"DATETIME": "20251128094659", "_v_raw": ["W", "L", "R"]},
    )
    # The tool's wall time, unchanged, carrying the site zone. Comparing
    # against a naive literal would only pass while the field stayed naive,
    # and a naive CanonicalEvent.timestamp is the defect: see mapper._aware.
    assert ev.timestamp.tzinfo is not None
    assert ev.timestamp.replace(tzinfo=None) == datetime(2025, 11, 28, 9, 46, 59)
    # That instant's offset, not today's: a site on DST has two.
    assert ev.timestamp.utcoffset() == (
        datetime(2025, 11, 28, 9, 46, 59).astimezone().utcoffset()
    )


def test_unparseable_timestamp_falls_back_but_event_type_still_resolves(tmp_path):
    """Belt-and-suspenders: a garbage timestamp shouldn't break the rest of
    the mapping pipeline - we just lose timestamp fidelity."""
    machine = _spts_machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile)
    ev = mapper.from_secs_event(
        machine, 851,
        {"DATETIME": "not-a-date", "_v_raw": ["C", 1, "L"]},
    )
    assert ev.event_type == "lot_start"
    assert ev.timestamp is not None
