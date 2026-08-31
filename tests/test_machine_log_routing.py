"""What has to end up in one machine's own `middleware.log`.

The NexGen MG outage of 2026-08-19 was diagnosed from the *global*
`eap_middleware.log`, because the per-machine file could not show it. For
forty minutes TOOL_04's `middleware.log` contained nothing but

    gateway.host: [TOOL_04] Alarm SET / Alarm CLEARED
    eap_middleware.service: Reconnect watchdog: TOOL_04 is disconnected...

The records that would have identified the fault in seconds - secsgem's
`communication` wire trace showing no Select.req and no S1F13, the
subscription result, the CSV writes - were all dropped, because the filter
kept a record only when the endpoint id appeared verbatim in its message and
none of those sources put it there.
"""

from __future__ import annotations

import logging

from eap_middleware.logging_setup import _MachineFilter


def _record(name: str, message: str, level: int = logging.INFO, thread: str = "MainThread"):
    record = logging.LogRecord(name, level, __file__, 1, message, None, None)
    record.threadName = thread
    return record


def _filter(**kwargs) -> _MachineFilter:
    return _MachineFilter("TOOL_04", display_name="NEXGEN_MG_01", **kwargs)


def test_records_naming_the_endpoint_are_kept():
    keep = _filter()
    assert keep.filter(_record("gateway.host", "[TOOL_04] Communication established"))
    assert keep.filter(_record("eap_middleware.service", "Starting machine TOOL_04"))


def test_records_naming_the_machine_by_display_name_are_kept():
    """csv_store and the Linkstuffs publishers name the machine by its display
    name, so every "Wrote lot CSV ..." line used to be dropped from the log of
    the very machine that produced it."""
    keep = _filter()
    assert keep.filter(
        _record(
            "eap_middleware.csv_store",
            "Wrote lot CSV C:/data/NEXGEN_MG_01/NEXGEN_MG_01_Lot_1.csv (carrier_unloaded)",
        )
    )


def test_warnings_are_never_dropped_for_want_of_a_machine_name():
    """A subscription that failed, or a spool that filled, is worth having in
    front of you even when the logger could not say which machine it was."""
    keep = _filter()
    assert keep.filter(
        _record(
            "gateway.event_subscription",
            "Event subscription failed",
            level=logging.WARNING,
        )
    )
    assert keep.filter(
        _record("eap_middleware.outbox", "Outbox is full", level=logging.ERROR)
    )


def test_another_machines_records_stay_out():
    keep = _filter()
    assert not keep.filter(
        _record("gateway.host", "[TOOL_02] Communication established")
    )
    assert not keep.filter(
        _record("eap_middleware.csv_store", "Wrote lot CSV for DAVINCI200_MC4_HC1_01")
    )


def test_a_longer_endpoint_id_is_not_a_match():
    """Substring matching put every TOOL_1 record into TOOL_10's log too. In a
    22-machine install that silently interleaves two tools in one file."""
    keep = _MachineFilter("TOOL_1", display_name="TOOL_1_NAME")
    assert keep.filter(_record("gateway.host", "[TOOL_1] Communication established"))
    assert not keep.filter(_record("gateway.host", "[TOOL_10] Communication established"))
    assert not keep.filter(_record("gateway.host", "[TOOL_11] Alarm SET"))


def test_an_explicit_endpoint_attribute_wins_over_the_message_text():
    keep = _filter()
    record = _record("anything", "no machine named here at all")
    record.endpoint_id = "TOOL_04"
    assert keep.filter(record)


def test_simulator_thread_records_go_only_to_the_simulator_log():
    middleware = _filter()
    simulator = _filter(simulator=True)
    sim_record = _record(
        "simulator.equipment", "[SIM] -> S6F11 CEID=5", thread="Simulator-TOOL_04"
    )
    assert simulator.filter(sim_record)
    assert not middleware.filter(sim_record)


def test_a_simulator_warning_does_not_leak_into_the_middleware_log():
    """The WARNING passthrough must not undo the simulator/middleware split -
    otherwise the tool's own log and the host's log stop being separable."""
    middleware = _filter()
    warning = _record(
        "simulator.equipment",
        "Spool full; overwriting oldest",
        level=logging.WARNING,
        thread="Simulator-TOOL_04",
    )
    assert not middleware.filter(warning)
    assert _filter(simulator=True).filter(warning)
