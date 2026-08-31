"""Wire-level regression tests for simulator subscription and spool handling."""

from types import SimpleNamespace

import secsgem.hsms
import secsgem.secs

from simulator.equipment import EquipmentSimulator


def _equipment() -> EquipmentSimulator:
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=65001,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    # The handler is never enabled, so this opens no listener or background
    # thread. Direct handler calls are enough to exercise raw SECS body decode.
    return EquipmentSimulator(settings, tool_id="WIRE_TEST")


def _packet(message):
    return SimpleNamespace(data=message.encode())


def _ack_value(response) -> int:
    return int(response.data.get())


def test_raw_s2f33_s2f35_s2f37_bodies_build_real_subscription_state():
    equipment = _equipment()

    define = secsgem.secs.functions.SecsS02F33({
        "DATAID": 0,
        "DATA": [{"RPTID": 1001, "VID": [11, 12]}],
    })
    assert _ack_value(equipment._handle_s2f33(None, _packet(define))) == 0
    assert equipment._report_definitions == {1001: [11, 12]}

    link = secsgem.secs.functions.SecsS02F35({
        "DATAID": 0,
        "DATA": [{"CEID": 55, "RPTID": [1001]}],
    })
    assert _ack_value(equipment._handle_s2f35(None, _packet(link))) == 0
    assert equipment._event_links == {55: [1001]}

    enable = secsgem.secs.functions.SecsS02F37({"CEED": True, "CEID": [55]})
    assert _ack_value(equipment._handle_s2f37(None, _packet(enable))) == 0
    assert equipment._is_event_enabled(55)
    assert not equipment._is_event_enabled(56)


def test_disable_all_does_not_fall_back_to_legacy_enable_everything():
    equipment = _equipment()
    disable = secsgem.secs.functions.SecsS02F37({"CEED": False, "CEID": []})
    assert _ack_value(equipment._handle_s2f37(None, _packet(disable))) == 0
    assert equipment._event_reporting_configured
    assert not equipment._is_event_enabled(1)
    assert not equipment._is_event_enabled(999999)


def test_enable_all_then_disable_one_tracks_an_explicit_exclusion():
    equipment = _equipment()
    enable_all = secsgem.secs.functions.SecsS02F37({"CEED": True, "CEID": []})
    disable_one = secsgem.secs.functions.SecsS02F37({"CEED": False, "CEID": [55]})
    equipment._handle_s2f37(None, _packet(enable_all))
    equipment._handle_s2f37(None, _packet(disable_one))
    assert not equipment._is_event_enabled(55)
    assert equipment._is_event_enabled(56)


def test_empty_report_definition_deletes_all_reports_and_links():
    equipment = _equipment()
    equipment._report_definitions = {1001: [11]}
    equipment._event_links = {55: [1001]}
    delete_all = secsgem.secs.functions.SecsS02F33({"DATAID": 0, "DATA": []})
    assert _ack_value(equipment._handle_s2f33(None, _packet(delete_all))) == 0
    assert equipment._report_definitions == {}
    assert equipment._event_links == {}


def test_s6f23_transmit_and_purge_operate_on_the_real_spool_queue():
    equipment = _equipment()
    equipment._queue_spooled("first", object())
    equipment._queue_spooled("second", object())

    scheduled = []
    equipment._schedule_spool_drain = lambda: scheduled.append(True)  # type: ignore[method-assign]
    transmit = secsgem.secs.functions.SecsS06F23(
        secsgem.secs.data_items.RSDC.TRANSMIT
    )
    assert _ack_value(equipment._handle_s6f23(None, _packet(transmit))) == 0
    assert scheduled == [True]
    assert equipment.spool_count() == 2

    purge = secsgem.secs.functions.SecsS06F23(secsgem.secs.data_items.RSDC.PURGE)
    assert _ack_value(equipment._handle_s6f23(None, _packet(purge))) == 0
    assert equipment.spool_count() == 0

    assert _ack_value(equipment._handle_s6f23(None, _packet(transmit))) == 2


def test_offline_primary_is_queued_instead_of_discarded():
    equipment = _equipment()
    message = secsgem.secs.functions.SecsS06F11({
        "DATAID": 0,
        "CEID": 55,
        "RPT": [],
    })
    assert equipment._send_or_spool("S6F11 CEID=55", message)
    assert equipment.spool_count() == 1



# ----- the NexGen advanced simulator must obey the same E5 rules -----
#
# It replaces the base class's S2F33/35/37 handlers with its own (they add
# per-band refusal), so every subscription rule the base simulator gets right
# has to be re-proved against it - and the delete-all rule was one it did not.


def _nexgen():
    from simulator.nexgen_mg_simulator import NexGenMgSimulator

    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=65002,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    return NexGenMgSimulator(settings, tool_id="MG_WIRE_TEST")


def test_nexgen_empty_report_definition_deletes_all_reports_and_links():
    """S2F33 with a zero-length list clears everything, per SEMI E5.

    This is step 2 of the MG manual's own lot-start sequence (9.1/9.2: "Host
    deletes all existing report definitions", then "... all existing report
    links"). The handler used to answer DRACK=0 and keep every definition,
    so a rig would have signed off a reset the real tool actually performs.
    """
    equipment = _nexgen()
    equipment._report_definitions = {1000000004: [11, 12], 1000000005: [13]}
    equipment._event_links = {4: [1000000004], 5: [1000000005]}

    delete_all = secsgem.secs.functions.SecsS02F33({"DATAID": 0, "DATA": []})
    assert _ack_value(equipment._handle_s2f33(None, _packet(delete_all))) == 0
    assert equipment._report_definitions == {}
    assert equipment._event_links == {}


def test_nexgen_deleting_one_report_drops_the_links_that_used_it():
    """A CEID left pointing at a deleted RPTID would report an undefined
    report. Deleting a definition takes its links with it."""
    equipment = _nexgen()
    equipment._report_definitions = {1000000004: [11], 1000000005: [13]}
    equipment._event_links = {4: [1000000004], 9: [1000000004, 1000000005]}

    delete_one = secsgem.secs.functions.SecsS02F33({
        "DATAID": 0,
        "DATA": [{"RPTID": 1000000004, "VID": []}],
    })
    assert _ack_value(equipment._handle_s2f33(None, _packet(delete_one))) == 0
    assert equipment._report_definitions == {1000000005: [13]}
    # CEID 4 had only that report and is gone; CEID 9 keeps its other one.
    assert equipment._event_links == {9: [1000000005]}


# ----- the spool must be a queue, not a one-way door -----

import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

import pytest  # noqa: E402

from secsgem.gem.communication_state_machine import CommunicationState  # noqa: E402


_PATCHED_COMM_STATE: list[type] = []


def _with_patchable_comm_state(equipment, state):
    """communication_state is a read-only property on the handler.

    It has to be patched on the class, because that is where the property
    lives - which means it outlives the test unless it is put back. It used
    not to be, so every EquipmentSimulator built later in the same pytest
    process inherited a SimpleNamespace communication_state and blew up in
    _on_protocol_disconnected. Nothing caught it only because this file
    happens to collect after the e2e files that would have tripped over it.
    """
    cls = type(equipment)
    _PATCHED_COMM_STATE.append(cls)
    cls.communication_state = property(lambda self: state)
    return equipment


@pytest.fixture(autouse=True)
def _restore_comm_state():
    yield
    while _PATCHED_COMM_STATE:
        cls = _PATCHED_COMM_STATE.pop()
        # Deleting the override restores the inherited property rather than
        # pinning a copy of it onto the subclass.
        if "communication_state" in cls.__dict__:
            delattr(cls, "communication_state")


def test_events_spooled_while_down_are_delivered_once_communication_returns():
    """The bug that produced a rig delivering nothing for six lots.

    `_send_or_spool` refuses to send while a backlog exists - correctly, so a
    spooled stream keeps its order. But the ONLY thing that emptied the
    backlog was an S6F23 from the host, which the middleware sends only when
    `drain_spool_on_connect: true` - and that was false on every shipped
    machine at the time. So one event spooled before the host connected made
    every later event spool too, forever, on a perfectly healthy link.

    Both halves are now closed: the simulator drains its own spool on
    reconnect (what this test pins), and the two shipped machines whose
    manuals document spooling - SPTS fxP and DaVinci - set
    `drain_spool_on_connect: true`, which is the half that matters on real
    equipment. The MG stays false; its manual documents spooling as
    unsupported.
    """
    equipment = _equipment()
    box = types.SimpleNamespace(current=CommunicationState.NOT_COMMUNICATING)
    _with_patchable_comm_state(equipment, box)
    sent = []
    equipment.send_and_waitfor_response = lambda m: (sent.append(m), object())[1]
    equipment._running = True

    equipment._send_or_spool("S6F11 CEID=4", object())
    equipment._send_or_spool("S6F11 CEID=5", object())
    assert equipment.spool_count() == 2 and not sent

    # Host connects. This is what used to change nothing at all.
    box.current = CommunicationState.COMMUNICATING
    equipment._on_state_communicating(None)

    # The drain runs on its own worker, which clears its own handle the
    # instant it finds the spool empty - so with an in-process send the handle
    # can already be None by the time we look. Wait on the outcome instead;
    # asserting on the handle is a race, not a guarantee.
    worker = equipment._spool_drain_worker
    if worker is not None:
        worker.join(timeout=10)
    deadline = time.monotonic() + 10
    while equipment.spool_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert equipment.spool_count() == 0, "spool never drained on reconnect"
    assert len(sent) == 2, "held events were not retransmitted"


def test_a_transient_retransmit_failure_does_not_silence_the_tool_forever():
    """One failed retransmit used to end the drain permanently.

    With the backlog test in _send_or_spool that is terminal: the queue never
    empties, so every later event joins it and the tool goes quiet for the
    rest of its run.
    """
    equipment = _equipment()
    box = types.SimpleNamespace(current=CommunicationState.COMMUNICATING)
    _with_patchable_comm_state(equipment, box)
    equipment._running = True

    attempts = {"n": 0}

    def flaky(_message):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None          # unacknowledged, once
        return object()

    equipment.send_and_waitfor_response = flaky
    equipment._queue_spooled("S6F11 CEID=4", object())

    equipment._schedule_spool_drain()
    equipment._spool_drain_worker.join(timeout=10)

    assert attempts["n"] >= 2, "the drain gave up after one failure"
    assert equipment.spool_count() == 0


def test_only_one_drain_worker_runs_at_a_time():
    """Two workers popping the same queue would interleave the order the
    spool exists to preserve. There are now two callers (S6F23 and entry to
    COMMUNICATING), so the guard is load-bearing."""
    equipment = _equipment()
    box = types.SimpleNamespace(current=CommunicationState.COMMUNICATING)
    _with_patchable_comm_state(equipment, box)
    equipment._running = True
    release = threading.Event()
    equipment.send_and_waitfor_response = lambda m: (release.wait(5), object())[1]
    equipment._queue_spooled("S6F11 CEID=4", object())

    equipment._schedule_spool_drain()
    first = equipment._spool_drain_worker
    equipment._schedule_spool_drain()
    assert equipment._spool_drain_worker is first, "a second drain worker started"
    release.set()
    first.join(timeout=10)


# ----- the lot must not start against a half-finished subscription -----

def test_subscription_settle_window_outlasts_a_gap_between_bands():
    """A pause between two subscription bands is not "the host has finished".

    The MG host subscribes in 31 bands of S2F33/S2F35/S2F37, so the enabled
    set grows in a burst per band. The settle check used to accept two
    consecutive equal polls - 100ms - as complete, so any inter-band gap
    longer than that started the lot against a partial subscription. Every
    CEID the host had not reached yet was then correctly and silently
    dropped, and the run looked like it emitted initialisation and setup and
    then stalled with no lot events at all.
    """
    from simulator.nexgen_mg_simulator import SUBSCRIPTION_QUIET_SEC

    poll = 0.05
    assert SUBSCRIPTION_QUIET_SEC >= 20 * poll, (
        "the settle window must span many polls, not two, or an inter-band "
        "pause reads as the end of the subscription"
    )


def test_a_growing_subscription_is_not_treated_as_settled():
    """Behavioural: while the count keeps rising, the wait keeps waiting."""
    import threading
    import time

    from simulator.nexgen_mg_simulator import NexGenMgSimulator

    equipment = NexGenMgSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1", port=65003,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE, session_id=0,
        ),
        tool_id="MG_SETTLE",
    )
    equipment._running = True

    def subscribe_in_bands() -> None:
        # Five bands, 0.3s apart: each gap is far longer than the old 100ms
        # window and far shorter than the whole sequence.
        for band in range(5):
            time.sleep(0.3)
            equipment._enabled_events.update(range(band * 10, band * 10 + 10))

    worker = threading.Thread(target=subscribe_in_bands, daemon=True)
    worker.start()
    started = time.monotonic()
    equipment._wait_for_subscription(timeout=10.0)
    elapsed = time.monotonic() - started
    worker.join(timeout=5)

    assert len(equipment._enabled_events) == 50, (
        "returned before every band had landed; saw "
        f"{len(equipment._enabled_events)} events"
    )
    assert elapsed < 9.0, "the wait should end on quiet, not on the timeout"
