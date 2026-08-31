"""Two things a simulator capture has to be able to tell you.

Both were wrong in the NexGen MG rig capture of 2026-08-20:

1. Every one of 640 messages logged as `->/spool S6F11 CEID=N`, on a link
   where all 640 were in fact sent and acknowledged with S6F12. The label
   covered both outcomes, so a healthy run was indistinguishable from a tool
   that had spooled everything because the host was gone.

2. Report 1000000213 (pm1WaferFinished) went out with 52 of its 74 slots as
   `<U4 0>` - every N2/medium/DI/HPC/BEM flow, every medium temperature,
   chuck speed and total process time. Those are exactly the values that only
   ever reach a dashboard through the telemetry payload, so the rig produced a
   green end-to-end run in which every process value published was zero.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import secsgem.hsms
import secsgem.secs
from secsgem.gem.communication_state_machine import CommunicationState

from simulator.dv_telemetry import telemetry_value
from simulator.equipment import EquipmentSimulator


def _equipment() -> EquipmentSimulator:
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=65002,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    return EquipmentSimulator(settings, tool_id="SIM_01")


# ----- 1. sent and spooled must not share a log line -----

def test_a_delivered_message_is_logged_as_sent(caplog):
    equipment = _equipment()
    equipment.communication_state._current_state = SimpleNamespace(
        state=CommunicationState.COMMUNICATING
    )
    equipment.send_and_waitfor_response = lambda message: object()

    with caplog.at_level(logging.INFO, logger="simulator.equipment"):
        assert equipment._send_or_spool("S6F11 CEID=5", object()) is True

    messages = [record.getMessage() for record in caplog.records]
    assert "[SIM_01] -> S6F11 CEID=5" in messages
    assert not any("Spooled" in message for message in messages)
    assert equipment.spool_count() == 0


def test_a_spooled_message_is_logged_as_spooled_not_sent(caplog):
    equipment = _equipment()  # never communicating -> must spool

    with caplog.at_level(logging.INFO, logger="simulator.equipment"):
        assert equipment._send_or_spool("S6F11 CEID=5", object()) is True

    messages = [record.getMessage() for record in caplog.records]
    assert "[SIM_01] Spooled S6F11 CEID=5" in messages
    assert not any(message.startswith("[SIM_01] -> ") for message in messages)
    assert equipment.spool_count() == 1


def test_an_unacknowledged_message_is_spooled_and_never_reported_as_sent(caplog):
    equipment = _equipment()
    equipment.communication_state._current_state = SimpleNamespace(
        state=CommunicationState.COMMUNICATING
    )
    equipment.send_and_waitfor_response = lambda message: None  # T3 expiry

    with caplog.at_level(logging.INFO, logger="simulator.equipment"):
        assert equipment._send_or_spool("S5F1 ALID=1001", object()) is True

    messages = [record.getMessage() for record in caplog.records]
    assert not any(message.startswith("[SIM_01] -> ") for message in messages)
    assert equipment.spool_count() == 1


# ----- 2. process telemetry must be a reading, not a zero -----

def _mg_wafer_report_slots():
    with open("output/nexgen_mg_series/EventSubscription.json") as handle:
        subscription = json.load(handle)
    reports = {report["rptid"]: report for report in subscription["reports"]}
    return reports[1000000213]["_slots"]


def test_every_process_variable_in_the_mg_wafer_report_gets_a_reading():
    """Every slot the manual defines as a measurement must carry one.

    Identity slots (WaferID, LotID, PortID, the lastStarted*/lastFinished*
    block) are deliberately left to `_dv_value`'s own handling, which fills
    them from the running lot.
    """
    seed = ("SIM_01", "LOT_SIM_0069", "W0069_03", 3)
    slots = _mg_wafer_report_slots()
    measurements = [
        slot for slot in slots
        if any(word in slot for word in ("Flow", "Temp", "Speed", "ProcessTime"))
    ]
    assert len(measurements) == 52, "the MG wafer report shape changed"

    missing = [slot for slot in measurements if telemetry_value(slot, seed) is None]
    assert missing == [], f"process variables still sent as zero: {missing}"
    assert all(telemetry_value(slot, seed) != 0 for slot in measurements)


def test_identity_slots_are_left_to_the_lot_context():
    """telemetry_value must not answer for these: a wafer's carrier ID is not
    a number to invent, and `_dv_value` already fills it from the lot."""
    seed = ("SIM_01", "LOT_SIM_0069", "W0069_03", 3)
    for slot in ("WaferID", "LotID", "RecipeName", "PortID", "CarrierID", "JobID"):
        assert telemetry_value(slot, seed) is None


def test_min_average_and_max_of_one_measurement_agree():
    """A tool never reports a minimum above its maximum. Independent random
    values would, and would quietly discredit any downstream rule that checks
    the ordering."""
    seed = ("SIM_01", "LOT_SIM_0069", "W0069_03", 3)
    for base in (
        "pm1N2ChuckFlow", "pm1Med1Temp", "pm1ChuckSpeed", "pm1BemMed4Flow",
    ):
        low = telemetry_value(f"{base}MinWafer", seed)
        average = telemetry_value(f"{base}AvrWafer", seed) or telemetry_value(
            f"{base}AvrgWafer", seed
        )
        high = telemetry_value(f"{base}MaxWafer", seed)
        assert low <= average <= high, f"{base}: {low} / {average} / {high}"


def test_readings_are_reproducible_and_vary_per_wafer():
    """Reproducible so a test can assert on them; different per wafer so a
    lot's worth of events is not one value repeated."""
    first = telemetry_value("pm1Med1TempAvrWafer", ("SIM_01", "LOT_A", "W1", 1))
    again = telemetry_value("pm1Med1TempAvrWafer", ("SIM_01", "LOT_A", "W1", 1))
    other = telemetry_value("pm1Med1TempAvrWafer", ("SIM_01", "LOT_A", "W2", 2))
    assert first == again
    assert first != other


def test_a_quantity_word_inside_another_word_is_not_a_match():
    """`rpm` sits inside lastStartedWafe(rPmI)d. A substring match read that
    as a chuck speed and answered 1308 for a process-module number that can
    only be 1 or 2."""
    assert telemetry_value("lastStartedWaferPmId", ("SIM_01",)) is None
    assert telemetry_value("lastFinishedWaferPmId", ("SIM_01",)) is None


def test_units_are_credible_for_the_kind_of_quantity():
    seed = ("SIM_01", "LOT_A", "W1", 1)
    temperature = telemetry_value("pm1Med1TempAvrWafer", seed)
    flow = telemetry_value("pm1N2ChuckFlowAvrWafer", seed)
    speed = telemetry_value("pm1ChuckSpeedAvrWafer", seed)
    seconds = telemetry_value("pm1TotalProcessTimeWafer", seed)

    assert 18.0 <= temperature <= 65.0
    assert 0.5 <= flow <= 12.0
    assert 200 <= speed <= 2400 and isinstance(speed, int)
    assert 20.0 <= seconds <= 180.0


def test_an_unknown_name_still_falls_back_rather_than_inventing_a_number():
    assert telemetry_value("SomethingNobodyDocumented", ("SIM_01",)) is None
    assert telemetry_value("", ("SIM_01",)) is None


# ----- 3. the status variables the host reads back must mean something -----

def _profile_simulator():
    from simulator.profile_simulator import ProfileSimulator

    return ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=65003,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        profile_id="nexgen_mg_series",
        tool_id="SIM_01",
    )


def test_events_enabled_readback_is_the_ceid_list_not_a_boolean():
    """The host reads this SVID back after S2F37 to confirm the subscription
    took (NexGen MG manual 9.1.1.7/9.1.1.8). Answering `1` made it report
    "242 of 243 requested collection events are not listed as enabled" on
    every run of the shipped simulator - a false alarm loud enough to train an
    operator to ignore the log."""
    equipment = _profile_simulator()
    svid = equipment._svid_names["EventsEnabled"]

    readback = equipment._davinci_svid_value(svid)

    assert isinstance(readback, list)
    assert len(readback) == len(equipment.profile.ceid_aliases)
    assert readback == sorted(readback)


def test_events_enabled_readback_follows_what_the_host_actually_enabled():
    equipment = _profile_simulator()
    svid = equipment._svid_names["EventsEnabled"]
    equipment._event_reporting_configured = True
    equipment._all_events_enabled = False
    equipment._enabled_events = {5, 4, 213}

    assert equipment._davinci_svid_value(svid) == [4, 5, 213]


def test_last_event_id_advances_so_a_silent_subscription_is_detectable():
    """The event-liveness watchdog tells "the tool is idle" from "the tool is
    firing events we never receive" by watching LastEventID move. A constant 0
    makes those two look identical, so the check could never fire against the
    simulator the rig runs."""
    equipment = _profile_simulator()
    svid = equipment._svid_names["LastEventID"]
    assert equipment._davinci_svid_value(svid) == 0

    # Not host-enabled: the tool still fires the event internally, which is
    # exactly the case the watchdog has to catch.
    equipment._event_reporting_configured = True
    equipment._all_events_enabled = False
    equipment._enabled_events = set()
    equipment._send_raw_s6f11(213, [])

    assert equipment._davinci_svid_value(svid) == 213


# ----- 4. the RPTID must be the one the host defined -----

def test_report_id_follows_the_offset_every_profile_and_decoder_uses():
    """`1003000000 + ceid % 1000000` is the same number as
    RPTID_CEID_OFFSET + ceid only for DaVinci, whose CEIDs sit in the 3xxxxxx
    range. For the NexGen MG it produced 1003000213 where the host had defined
    1000000213, so the host never matched the report it asked for and fell
    back to "the first report in the message" - leaving the RPTID-keyed decode
    path that real hardware uses unexercised."""
    import json
    import glob

    from eap_middleware.mapper import RPTID_CEID_OFFSET

    for path in glob.glob("output/*/EventSubscription.json"):
        with open(path) as handle:
            subscription = json.load(handle)
        for event in subscription["events"]:
            for rptid in event.get("rptids", []):
                assert rptid == RPTID_CEID_OFFSET + event["ceid"], (
                    f"{path}: CEID {event['ceid']} report {rptid}"
                )


def test_the_simulator_sends_that_same_report_id():
    from simulator.secsgem_equipment import RPTID_CEID_OFFSET

    equipment = _profile_simulator()
    sent = {}
    equipment._send_or_spool = lambda label, message: sent.update(
        message=message
    ) or True

    equipment._send_raw_s6f11(213, ["W1"])

    reports = sent["message"].data["RPT"]
    assert int(reports[0]["RPTID"].get()) == RPTID_CEID_OFFSET + 213


# ----- 5. ALCD must carry a SEMI E5 category -----

def test_alarms_carry_a_real_semi_e5_category_not_zero():
    """Every alarm on the rig reached the middleware as `Code=0`. Zero is not
    a category SEMI E5 defines, and the alarm limiter keys on categories 1 and
    2 to decide what must never be shed - so the simulator could not produce
    an alarm that path would protect."""
    from simulator.secsgem_equipment import (
        ALARM_CLASS_PARAMETER_CONTROL_WARNING,
        SecsGemEquipment,
    )

    equipment = SecsGemEquipment(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=65004,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        tool_id="SIM_01",
    )
    sent = []
    equipment._send_or_spool = lambda label, message: sent.append(message) or True

    equipment._send_s5f1_alarm(1001, "PM1: chuck N2 flow below limit", is_set=True)
    equipment._send_s5f1_alarm(1001, "PM1: chuck N2 flow below limit", is_set=False)

    alcd_set = int(sent[0].data[0].get())
    alcd_clear = int(sent[1].data[0].get())

    assert alcd_set & 0x80, "bit 7 must mark the alarm as set"
    assert not alcd_clear & 0x80, "bit 7 must be clear on the clear"
    assert alcd_set & 0x7F == ALARM_CLASS_PARAMETER_CONTROL_WARNING
    assert alcd_clear & 0x7F == ALARM_CLASS_PARAMETER_CONTROL_WARNING


def test_a_safety_category_alarm_survives_the_rate_limiter():
    """The end the category exists for: categories 1 and 2 are never shed."""
    from eap_middleware.alarms import AlarmRateLimiter
    from simulator.secsgem_equipment import ALARM_CLASS_EQUIPMENT_SAFETY

    limiter = AlarmRateLimiter(max_per_window=1, window_sec=60)
    assert limiter.admit("TOOL_04", alarm={"alid": 1, "is_set": True, "alcd": 3})
    # Ordinary alarm past the limit is shed...
    assert not limiter.admit("TOOL_04", alarm={"alid": 2, "is_set": True, "alcd": 3})
    # ...an equipment-safety alarm is not.
    assert limiter.admit(
        "TOOL_04",
        alarm={"alid": 3, "is_set": True, "alcd": ALARM_CLASS_EQUIPMENT_SAFETY},
    )


# ----- 6. a report must not contradict itself -----

def test_every_port_slot_in_one_report_names_the_same_port():
    """pm1WaferFinished went out with PortID=1 and UnloadPort=0 in the same
    message. 0 is not a port the tool has, and the middleware carries the
    contradiction downstream verbatim - it has no way to know which slot to
    believe."""
    from simulator.profile_simulator import ProfileSimulator

    equipment = ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1", port=65005,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE, session_id=0,
        ),
        profile_id="nexgen_mg_series", tool_id="SIM_01",
    )
    ctx = {
        "port": 1, "module": 1, "lot_id": "LOT_A", "carrier_id": "CAR_A",
        "recipe": "RCP", "job_id": "JOB_A", "wafer_id": "W1", "slot": 3,
    }
    for slot in (
        "PortID", "UnloadPort", "lastStartedWaferLoadPort",
        "lastStartedWaferUnloadPort",
    ):
        assert equipment._dv_value(slot, ctx) == 1, slot
    # Carrier and process-module identity follow the same lot context.
    assert equipment._dv_value("lastStartedWaferCid", ctx) == "CAR_A"
    assert equipment._dv_value("lastStartedWaferPmId", ctx) == 1
    assert equipment._dv_value("lastFinishedWaferPmId", ctx) == 1


def test_the_mg_wafer_report_leaves_no_slot_at_zero():
    """The end-to-end statement: every one of pm1WaferFinished's 74 slots
    carries something a dashboard can use. 58 of them used to be 0."""
    from simulator.profile_simulator import ProfileSimulator

    equipment = ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1", port=65006,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE, session_id=0,
        ),
        profile_id="nexgen_mg_series", tool_id="SIM_01",
    )
    ctx = {
        "port": 1, "module": 1, "lot_id": "LOT_A", "carrier_id": "CAR_A",
        "recipe": "RCP", "job_id": "JOB_A", "wafer_id": "W1", "slot": 3,
    }
    values = [equipment._dv_value(s, ctx) for s in _mg_wafer_report_slots()]
    assert len(values) == 74
    assert [s for s, v in zip(_mg_wafer_report_slots(), values) if v == 0] == []
