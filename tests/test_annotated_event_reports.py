"""S6F13 (Annotated Event Report Send) must land on the S6F11 pipeline.

Which message a tool uses to deliver a collection event is a tool-side
setting, not a negotiation:

  * SPTS fxP Omega - equipment constant 4022 `EventReportMsg`
    (Omega manual Table 6): 67075 = S6F3, 67083 = S6F11, 67085 = S6F13.
  * NexGen MG - S2F33 carries a Boolean selecting annotated reports for a
    report definition (MG manual §6.2 S2F33, §6.5 S6F13).

secsgem 0.3.0 ships no S6F13/S6F14 classes, so a tool set to annotated
reports connected, had every S2F33/35/37 acknowledged, and then delivered
nothing the host could decode - a green link with a permanently empty feed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from gateway.host import GatewayHost, create_host_settings


def _host(events: List[Dict[str, Any]]) -> GatewayHost:
    settings = create_host_settings(
        host="127.0.0.1", port=15999, device_id=0, mode="active"
    )
    return GatewayHost(
        settings=settings,
        tool_id="ANNOTATED",
        on_event=lambda _tool, _ceid, data: events.append(data),
    )


def _packet(payload: bytes, system: int = 4242) -> Any:
    return SimpleNamespace(data=payload, header=SimpleNamespace(system=system))


def test_s6f13_round_trips_through_the_real_codec():
    """The declared _data_format must actually encode and decode a report."""
    events: List[Dict[str, Any]] = []
    host = _host(events)

    message = host.stream_function(6, 13)({
        "DATAID": 1,
        "CEID": 858,
        "RPT": [{"RPTID": 1000000858, "VLIST": [
            {"VID": 5111, "V": "LOT_A"},
            {"VID": 5113, "V": "PM3"},
            {"VID": 5114, "V": "W07"},
        ]}],
    })
    decoded = host._decode_packet_data(6, 13, message.encode())
    parsed = host._parse_annotated_event_data(decoded)

    assert parsed["ceid"] == 858
    assert parsed["_annotated"] is True
    # Flattened positionally, exactly as the mapper consumes an S6F11 report.
    assert parsed["_v_raw"] == ["LOT_A", "PM3", "W07"]
    assert parsed["_rptid"] == 1000000858
    # And the VIDs are preserved, which S6F11 cannot carry at all.
    assert parsed["_vid_values"] == {5111: "LOT_A", 5113: "PM3", 5114: "W07"}


def test_s6f13_is_acknowledged_and_reaches_the_event_callback():
    """ACKC6=0 only after the callback (which journals) returned cleanly -
    the same no-loss ordering S6F11 has."""
    events: List[Dict[str, Any]] = []
    host = _host(events)

    message = host.stream_function(6, 13)({
        "DATAID": 0,
        "CEID": 857,
        "RPT": [{"RPTID": 1000000857, "VLIST": [{"VID": 5113, "V": "PM1"}]}],
    })
    reply = host._handle_s6f13(host, _packet(message.encode(), system=99))

    assert reply.get() == 0, "a stored annotated report must be ACKC6=0"
    assert len(events) == 1
    delivered = events[0]
    assert delivered["ceid"] == 857
    assert delivered["_stream"] == 6 and delivered["_function"] == 13
    assert delivered["_system_bytes"] == 99, (
        "the retransmission id must be journaled for S6F13 too, or a resent "
        "annotated report would be stored twice"
    )


def test_s6f13_that_cannot_be_stored_is_refused():
    """A callback failure must become ACKC6=1 so the tool keeps the event,
    never a silent ACKC6=0."""
    def explode(_tool, _ceid, _data):
        raise RuntimeError("journal is down")

    settings = create_host_settings(
        host="127.0.0.1", port=15999, device_id=0, mode="active"
    )
    host = GatewayHost(settings=settings, tool_id="ANNOTATED", on_event=explode)
    message = host.stream_function(6, 13)({
        "DATAID": 0, "CEID": 857,
        "RPT": [{"RPTID": 1, "VLIST": [{"VID": 1, "V": "x"}]}],
    })

    reply = host._handle_s6f13(host, _packet(message.encode()))

    assert reply.get() == 1, "an unstored annotated report must be ACKC6=1"


def test_host_registers_the_s6f13_handler():
    """Registration is what makes the difference between ingesting the tool's
    events and silently dropping them."""
    host = _host([])
    assert getattr(host._callback_handler, "s06f13", None) is not None, (
        "S6F13 must be registered or a tool configured for annotated reports "
        "delivers nothing"
    )
    # And the codec has to know the functions, or the reply cannot be built.
    assert host.stream_function(6, 13) is not None
    assert host.stream_function(6, 14) is not None
