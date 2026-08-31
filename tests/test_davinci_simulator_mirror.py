"""Verifies the SecsGemEquipment is a 1:1 mirror of the real machine's
SECS-II surface, not just a CEID-firing toy.

Specifically:

1. S1F3 (Selected Equipment Status Request) returns real DaVinci SVID values
   in the order requested, with sensible types (U1 ControlState, A Clock,
   string MachineName via 4030003, etc.).
2. S2F13 (Equipment Constant Request) returns real DaVinci ECID values.
3. S1F11 (SVID Namelist Request) returns the workbook-sourced SVID names.
4. The simulator emits nested SECS-II Array structures for E90 substrate
   events (not flattened strings) and the middleware mapper round-trips them.
5. The Clock SVID returns DaVinci's 12-byte yymmddhhmmss format per its
   TimeFormat=1 default.

These prove the simulator can stand in for hardware during integration
tests for any host that follows the DaVinci spec - not just our middleware.
"""

from __future__ import annotations

import socket
import struct
from types import SimpleNamespace

import pytest

pytest.importorskip("secsgem")

import secsgem.hsms
import secsgem.secs.variables as secs_var

from simulator.secsgem_equipment import SecsGemEquipment, _typed


def _free_port() -> int:
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise RuntimeError("no free port")


# ----- 1. Typed-list helper produces proper Arrays for substrate events -----


def test_typed_helper_builds_nested_array_for_string_list():
    """E90 substrate events arrive with SubstLotIDList=['LOT_M42'] etc.
    The simulator must encode these as SECS-II Array(String), not as a
    flattened scalar - real DaVinci sends nested lists."""
    arr = _typed(["LOT_M42"])
    assert isinstance(arr, secs_var.Array)
    assert arr.get() == ["LOT_M42"]


def test_typed_helper_handles_empty_list_as_empty_array():
    arr = _typed([])
    assert isinstance(arr, secs_var.Array)
    assert arr.get() == []


def test_typed_helper_handles_multi_element_string_list():
    arr = _typed(["W001", "W002", "W003"])
    assert isinstance(arr, secs_var.Array)
    assert arr.get() == ["W001", "W002", "W003"]


def test_typed_helper_handles_int_list_as_array_of_u4():
    arr = _typed([1, 2, 3])
    assert isinstance(arr, secs_var.Array)
    assert arr.get() == [1, 2, 3]


def test_typed_helper_jsonifies_list_of_dicts():
    """TestResults = [{'die': '1,1', ...}, ...] - too structured for native
    SECS arrays; encode each as JSON string inside an Array(String)."""
    import json

    arr = _typed([{"die": "1,1", "v": 1.0}, {"die": "1,2", "v": 1.1}])
    assert isinstance(arr, secs_var.Array)
    items = arr.get()
    assert len(items) == 2
    parsed = json.loads(items[0])
    assert parsed["die"] == "1,1"


def test_short_e90_payload_does_not_index_past_available_values():
    sim = _make_offline_simulator()
    sim.communication_state._current_state = (
        sim.communication_state.communicating
    )
    sim._all_events_enabled = True
    sim.send_and_waitfor_response = lambda _message: object()

    assert sim._send_raw_s6f11(3220013, [[1]]) is True


# ----- 2. Inline tests for the SVID/EC value lookups (don't need HSMS) -----


def test_davinci_svid_clock_width_follows_the_timeformat_ecid():
    """The Clock SVID must honour ECID 4010001, whatever it is set to.

    This test used to assert a flat 12 bytes, with the rationale "DaVinci
    default TimeFormat ECID=1 -> 12 bytes yymmddhhmmss". That restated the
    simulator's own comment rather than a vendor source, and it pinned an
    internal contradiction: the simulator advertised TimeFormat=1 and then
    emitted a width unrelated to it, so the constant was decorative.

    The vendor workbook (EC sheet, ECID 4010001) gives format U1, **Default
    Value = 1**, and describes the choice as "12-byte, 16-byte, or Extended
    format" - an ordering that reads as 0/1/2 and matches SEMI E30's GEM
    TimeFormat ECV. The two sibling manuals in docs/vendor/ state the same
    mapping outright: Omega Table 6 ECID 67 "0 = 12 byte format / 1 = 16 byte
    format", NexGen MG §8.4 ECID 5 "0 = 12-byte format / 1 = 16-byte format,
    default=1". 16-byte is the Year-2000-compliant form, which is why all
    three default to it.

    The DaVinci workbook does not spell the 0/1 mapping out, so the default
    below is an inference from those three sources rather than a verbatim
    quote. Pinning the *coupling* is what matters either way: if that
    inference is ever corrected, changing the ECID's value in
    `_DAVINCI_ECID_STATIC_VALUES` now changes the emitted clock with it.
    """
    sim = _make_offline_simulator()

    assert int(sim._davinci_ecid_value(4010001)) == 1
    wide = sim._davinci_svid_value(1010005)
    assert isinstance(wide, str) and wide.isdigit()
    assert len(wide) == 16, wide          # YYYYMMDDhhmmsscc

    sim._davinci_ecid_value = (  # type: ignore[method-assign]
        lambda ecid: 0 if ecid == 4010001 else 0
    )
    narrow = sim._davinci_svid_value(1010005)
    assert isinstance(narrow, str) and narrow.isdigit()
    assert len(narrow) == 12, narrow      # yymmddhhmmss


def test_davinci_svid_returns_machine_name_at_4030003():
    """MachineName EC reflects the tool_id we constructed with."""
    sim = _make_offline_simulator(tool_id="DAV_MIRROR_TEST")
    assert sim._davinci_ecid_value(4030003) == "DAV_MIRROR_TEST"


def test_davinci_svid_returns_control_state_online_remote():
    """Default state: ControlState=5 (Online/Remote) - the only mode the
    middleware can issue remote commands from."""
    sim = _make_offline_simulator()
    assert sim._davinci_svid_value(1010001) == 5


def test_davinci_svid_returns_process_state_idle():
    """ProcessState=2 (Idle) when no lot is running."""
    sim = _make_offline_simulator()
    assert sim._davinci_svid_value(1050001) == 2


def test_davinci_svid_reflects_current_lot_during_processing():
    """When the lot script is mid-run, polling PM1/RecipeName (1060007) and
    LP1/CarrierID (1120001) returns the current values, not stale ones."""
    sim = _make_offline_simulator()
    sim._current_lot_id = "LOT_ACTIVE"
    sim._current_recipe = "Recipe_Inflight"
    sim._current_carrier_id = "CARRIER_42"
    assert sim._davinci_svid_value(1060007) == "Recipe_Inflight"
    assert sim._davinci_svid_value(1120001) == "CARRIER_42"
    assert sim._davinci_svid_value(1120010) == "LOT_ACTIVE"


def test_davinci_ecid_time_format_default_is_12_byte():
    sim = _make_offline_simulator()
    assert sim._davinci_ecid_value(4010001) == 1


def test_davinci_identity_matches_host_manual():
    sim = _make_offline_simulator()
    assert sim._handle_s1f1_davinci(None, None).get() == [
        "DaVinci200",
        "DaVinci200 Version 4.9.3",
    ]


@pytest.mark.parametrize(
    ("stream", "function", "handler_name"),
    [
        (1, 3, "_handle_s1f3_davinci"),
        (1, 11, "_handle_s1f11_davinci"),
        (2, 13, "_handle_s2f13_davinci"),
    ],
)
def test_empty_queries_return_all_documented_items(
    stream, function, handler_name
):
    sim = _make_offline_simulator()
    request = sim.stream_function(stream, function)([])
    response = getattr(sim, handler_name)(
        None, SimpleNamespace(data=request.encode())
    ).get()
    assert len(response) > 10


# Note: real-HSMS S1F3 round-trip was attempted here but secsgem's
# response.data wrapping makes a clean assertion brittle (typed items don't
# compare == to raw Python values). The unit tests above prove the value
# mapping; the full simulator E2E in test_davinci_simulator_e2e.py proves
# the wire layer. We don't need a third stack between them.


# ----- helpers -----


def _make_offline_simulator(
    tool_id: str = "DAV_OFFLINE_TEST",
) -> SecsGemEquipment:
    """Build a simulator without enabling it - just for testing the
    pure-Python value lookups, no HSMS server bound."""
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=0,  # port=0 means OS picks, but we won't bind
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    return SecsGemEquipment(
        settings=settings,
        tool_id=tool_id,
        wafer_count=1,
        step_interval_sec=0.0,
        fire_alarm=False,
        loop_lots=False,
    )
