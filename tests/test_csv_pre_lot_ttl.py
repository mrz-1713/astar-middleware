"""Pre-lot buffer TTL + hard-cap regression tests (v2 Track A)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import ProfileRegistry


def _machine(tmp_path) -> MachineConfig:
    return MachineConfig(
        endpoint_id="TOOL_TTL",
        display_name="SPTS_TTL",
        machine_profile="spts_fxp_omega",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _orphan_event(profile, machine, raw_event: str):
    """Build a pre-lot event with no lot_id - it goes to _pending_pre_lot."""
    return CanonicalMapper(profile).from_secs_event(
        machine, 0,
        {
            "DATETIME": "2025-11-28 09:46:59.345559",
            "SECSGEM_RAW_EVENT": raw_event,
            "LOAD_PORT": 2,
        },
    )


def test_pre_lot_entries_pruned_after_ttl(tmp_path):
    """Push a row, fast-forward the stored timestamp past TTL, push another
    row, verify the first one was pruned."""
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    writer = PerLotCsvWriter(pre_lot_ttl_sec=1.0)

    writer.append(machine, profile, _orphan_event(profile, machine, "Loaded"))
    key = (machine.endpoint_id, "2")
    assert key in writer._pending_pre_lot
    assert len(writer._pending_pre_lot[key]) == 1

    # Backdate the existing entry past the 1-second TTL, preserving the
    # (timestamp, row, journal-seq) tuple shape the writer stores.
    old_ts = datetime.now(timezone.utc) - timedelta(seconds=10)
    old_entry = writer._pending_pre_lot[key][0]
    writer._pending_pre_lot[key][0] = (old_ts, old_entry[1], old_entry[2])

    # Append a fresh row - pruning runs first, so only the fresh row remains
    writer.append(machine, profile, _orphan_event(profile, machine, "Clamped"))
    assert len(writer._pending_pre_lot[key]) == 1, "stale entry should be pruned"


def test_pre_lot_hard_cap_drops_oldest_and_warns(tmp_path, caplog):
    """Pushing more than the cap discards the oldest entry and logs WARN."""
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    writer = PerLotCsvWriter(
        pre_lot_ttl_sec=86400.0,  # disable TTL pruning for this test
        pre_lot_max_entries=5,
    )

    with caplog.at_level(logging.WARNING, logger="eap_middleware.csv_store"):
        for _ in range(7):
            writer.append(
                machine, profile, _orphan_event(profile, machine, "Loaded")
            )

    key = (machine.endpoint_id, "2")
    assert len(writer._pending_pre_lot[key]) == 5
    warnings = [r for r in caplog.records if "pre-lot cap" in r.getMessage()]
    assert len(warnings) == 2, "two over-cap events should each warn"


def test_pre_lot_buffer_drains_into_lot_when_lot_start_arrives(tmp_path):
    """Pre-lot rows that survive TTL still get promoted into the buffer
    when a real lot_start event arrives. Existing v1 behaviour preserved."""
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    writer = PerLotCsvWriter(pre_lot_ttl_sec=86400.0)
    mapper = CanonicalMapper(profile)

    # Two pre-lot orphan events
    writer.append(machine, profile, _orphan_event(profile, machine, "Loaded"))
    writer.append(machine, profile, _orphan_event(profile, machine, "Clamped"))

    # Now lot_start arrives with a real LotID
    lot_event = mapper.from_secs_event(
        machine, 851,
        {
            "DATETIME": "2025-11-28 09:47:10.000000",
            "SECSGEM_RAW_EVENT": "CassetteStarted",
            "LOAD_PORT": 2,
            "LOT_ID": "LOT-PROMOTE",
            "_v_raw": ["CASS-1", 2, "LOT-PROMOTE"],
        },
    )
    writer.append(machine, profile, lot_event)
    # Pending bucket emptied; buffer has the rows
    assert (machine.endpoint_id, "2") not in writer._pending_pre_lot
    buffer = writer._buffers[(machine.endpoint_id, "2")]
    assert len(buffer.rows) == 3  # 2 pre-lot + 1 lot_start
