"""Banded subscription: one refused family must not void the others.

These exercise EventSubscriptionManager directly against a fake host that
records the S2F33/35/37 traffic, because the property being protected is about
which messages are sent and what happens when one is rejected - not about any
particular vendor's constants.
"""

from __future__ import annotations

from typing import Any, Dict, List

from gateway.event_subscription import (
    EventDefinition,
    EventSubscriptionManager,
    ReportDefinition,
    SubscriptionConfig,
)


class _FakeHost:
    """Records outgoing subscription messages and replies with canned acks."""

    def __init__(self, acks: Dict[int, List[int]] = None):
        self.sent: List[Dict[str, Any]] = []
        # {function: [ack, ack, ...]} consumed in order; missing -> 0.
        self.acks = acks or {}

    def stream_function(self, stream: int, function: int):
        def build(payload):
            return {"stream": stream, "function": function, "payload": payload}
        return build

    def send_and_waitfor_response(self, message):
        self.sent.append(message)
        queue = self.acks.get(message["function"])
        return queue.pop(0) if queue else 0

    def messages(self, function: int) -> List[Dict[str, Any]]:
        return [m for m in self.sent if m["function"] == function]


def _banded_config() -> SubscriptionConfig:
    return SubscriptionConfig(
        reports=[
            ReportDefinition(rptid=1, name="a", dvids=[10], band="alpha"),
            ReportDefinition(rptid=2, name="b", dvids=[20], band="beta"),
            ReportDefinition(rptid=3, name="c", dvids=[30], band="beta"),
        ],
        events=[
            EventDefinition(ceid=100, name="A", rptids=[1], band="alpha"),
            EventDefinition(ceid=200, name="B", rptids=[2], band="beta"),
            EventDefinition(ceid=300, name="C", rptids=[3], band="beta"),
        ],
    )


def test_bands_are_issued_as_separate_messages():
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions() is True
    assert manager.band_results == {"alpha": True, "beta": True}
    # One S2F33 / S2F35 / S2F37 per band, not one batch for everything.
    assert len(host.messages(33)) == 2
    assert len(host.messages(35)) == 2
    assert len(host.messages(37)) == 2
    alpha_enable, beta_enable = host.messages(37)
    assert alpha_enable["payload"]["CEID"] == [100]
    assert beta_enable["payload"]["CEID"] == [200, 300]


def test_a_refused_band_leaves_the_others_subscribed():
    # First S2F33 (band alpha) is rejected with DRACK=4.
    host = _FakeHost(acks={34: [4], 33: [4]})
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions() is True, "beta should still be live"
    assert manager.band_results == {"alpha": False, "beta": True}
    # alpha never got past S2F33; beta completed the full sequence.
    assert len(host.messages(35)) == 1
    assert host.messages(37)[0]["payload"]["CEID"] == [200, 300]
    assert manager.get_status()["band_results"] == {"alpha": False, "beta": True}


def test_every_band_refused_reports_failure():
    host = _FakeHost(acks={33: [4, 4]})
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions() is False
    assert manager.band_results == {"alpha": False, "beta": False}
    assert manager.is_subscribed is False


def test_unbanded_config_sends_exactly_one_batch_as_before():
    config = SubscriptionConfig(
        reports=[ReportDefinition(rptid=1, name="a", dvids=[10])],
        events=[EventDefinition(ceid=100, name="A", rptids=[1])],
    )
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=config)

    assert manager.setup_subscriptions() is True
    assert manager.band_results == {"": True}, "unnamed band keeps an empty key"
    assert len(host.messages(33)) == 1
    assert len(host.messages(35)) == 1
    assert len(host.messages(37)) == 1


def test_events_without_a_report_are_enabled_but_never_linked():
    """An empty RPTID list means 'delete this link' in SEMI E5 - the exact
    move that acknowledges a subscription and then delivers nothing."""
    config = SubscriptionConfig(
        reports=[ReportDefinition(rptid=1, name="a", dvids=[10])],
        events=[
            EventDefinition(ceid=100, name="withreport", rptids=[1]),
            EventDefinition(ceid=101, name="noreport", rptids=[]),
        ],
    )
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=config)

    assert manager.setup_subscriptions() is True
    linked = host.messages(35)[0]["payload"]["DATA"]
    assert [entry["CEID"] for entry in linked] == [100]
    # ...but it IS enabled, so the tool still reports the event.
    assert host.messages(37)[0]["payload"]["CEID"] == [100, 101]


def test_a_band_of_only_unreportable_events_still_enables_them():
    config = SubscriptionConfig(
        events=[
            EventDefinition(ceid=130, name="CasPlaced", rptids=[], band="lp"),
            EventDefinition(ceid=134, name="CasRemoved", rptids=[], band="lp"),
        ],
    )
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=config)

    assert manager.setup_subscriptions() is True
    assert host.messages(35) == [], "nothing to link"
    assert host.messages(37)[0]["payload"]["CEID"] == [130, 134]


def test_all_disabled_events_never_send_the_enable_everything_form():
    """S2F37 with an empty CEID list means 'every event on the tool'."""
    config = SubscriptionConfig(
        events=[EventDefinition(ceid=100, name="A", rptids=[], enabled=False)],
    )
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=config)

    manager.setup_subscriptions()
    assert host.messages(37) == []


def test_requested_ceids_lists_what_the_read_back_should_confirm():
    manager = EventSubscriptionManager(_FakeHost(), config=_banded_config())
    assert manager.requested_ceids() == [100, 200, 300]


# ----- every shipped subscription must actually be banded -----
#
# The mechanism above is worthless if the files that reach real tools do not
# use it. The DaVinci subscription shipped with no bands at all: one rejected
# VID took its whole feed - all 54 events - and left the machine connected and
# silent. SPTS was a half-measure, its 96 events carrying 7 band labels while
# all 43 of its reports sat in one unnamed band, so the S2F35 leg was
# contained and the S2F33 leg was not.

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_OUTPUT = Path(__file__).resolve().parent.parent / "output"
SHIPPED = [
    "davinci200_mc4_hc1/EventSubscription.json",
    "spts_fxp_omega/EventSubscription.json",
    "nexgen_mg_series/EventSubscription.json",
]


def _load(relative: str) -> Dict[str, Any]:
    return json.loads((_OUTPUT / relative).read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", SHIPPED)
def test_every_report_and_event_carries_a_band(relative):
    data = _load(relative)
    unbanded_events = [e["ceid"] for e in data["events"] if not e.get("band")]
    unbanded_reports = [r["rptid"] for r in data["reports"] if not r.get("band")]
    assert not unbanded_events, (
        f"{relative}: events with no band: {unbanded_events[:8]} - an unbanded "
        "event joins the single default band, so one bad constant anywhere in "
        "it voids every event it shares that band with"
    )
    assert not unbanded_reports, (
        f"{relative}: reports with no band: {unbanded_reports[:8]}"
    )


@pytest.mark.parametrize("relative", SHIPPED)
def test_a_report_shares_the_band_of_the_event_that_links_it(relative):
    """S2F35 must not reference a report defined in a different band.

    Bands are sent as independent define/link/enable sequences. A report
    defined in band A but linked from band B is linked before it exists when
    B runs first, and the tool answers LRACK=3 (report does not exist).
    """
    data = _load(relative)
    band_of_report = {int(r["rptid"]): r.get("band") for r in data["reports"]}
    mismatches = [
        (event["ceid"], rptid, event.get("band"), band_of_report.get(int(rptid)))
        for event in data["events"]
        for rptid in (event.get("rptids") or [])
        if band_of_report.get(int(rptid)) != event.get("band")
    ]
    assert not mismatches, (
        f"{relative}: report/event band mismatches (ceid, rptid, event band, "
        f"report band): {mismatches[:5]}"
    )


@pytest.mark.parametrize("relative", SHIPPED)
def test_no_single_band_can_take_down_the_whole_feed(relative):
    """The property the banding exists for, measured on the shipped file.

    Rejecting one VID must never cost every event. Before banding, the
    DaVinci lost 54 of 54 - it was connected and reporting nothing.
    """
    config = SubscriptionConfig.from_file(_OUTPUT / relative)
    enabled = [event for event in config.events if event.enabled]
    assert enabled, f"{relative}: nothing enabled"

    largest = max(
        (
            len([
                event for event in enabled
                if event.band == report.band
            ])
            for report in config.reports if report.dvids
        ),
        default=0,
    )
    assert largest < len(enabled), (
        f"{relative}: one refused band costs all {len(enabled)} events; the "
        "subscription is effectively unbanded"
    )


# ----- a stopped session must abandon its subscription mid-flight -----


class _RecordingHost(_FakeHost):
    """A _FakeHost that also records which RPTIDs S2F33 actually defined.

    The band-abort tests assert on the wire, not on `band_results`: a break
    that happened one band too late still leaves the right number of results
    if the loop also recorded the abandoned band, so the question that
    matters is which reports the tool was actually told about.
    """

    @property
    def defined_rptids(self) -> List[int]:
        return [
            entry["RPTID"]
            for message in self.messages(33)
            for entry in message["payload"]["DATA"]
        ]


def test_subscription_abandons_between_bands_when_the_session_stops():
    """31 bands is ~93 blocking round trips; a stop cannot wait for them.

    Without an abort check, stop() issued during subscription could not take
    effect until every remaining define/link/enable had completed. The worker
    outlived its 10s join and a machine being torn down went on configuring
    the tool it was disconnecting from - and across a test file that runs ten
    such rigs, those workers accumulated until unrelated tests started
    failing.
    """
    reports = [
        ReportDefinition(rptid=1000 + n, name=f"r{n}", dvids=[n], band=f"b{n}")
        for n in range(6)
    ]
    events = [
        EventDefinition(ceid=n, name=f"e{n}", rptids=[1000 + n], band=f"b{n}")
        for n in range(6)
    ]
    host = _FakeHost()
    manager = EventSubscriptionManager(
        host, config=SubscriptionConfig(reports=reports, events=events)
    )

    # "Current" for the first two bands, then the session is torn down.
    seen: List[bool] = []

    def should_continue() -> bool:
        seen.append(True)
        return len(seen) <= 2

    manager.setup_subscriptions(should_continue)

    assert len(manager.band_results) == 2, (
        "subscription did not stop at the band boundary; it ran "
        f"{len(manager.band_results)} bands after the session was retired"
    )
    defined = [
        report["RPTID"]
        for message in host.messages(33)
        for report in message["payload"]["DATA"]
    ]
    assert defined == [1000, 1001], (
        f"reports were defined after the stop: {defined}"
    )


def test_subscription_without_a_predicate_still_runs_every_band():
    """The check is opt-in; direct callers keep the original behaviour."""
    reports = [
        ReportDefinition(rptid=1000 + n, name=f"r{n}", dvids=[n], band=f"b{n}")
        for n in range(4)
    ]
    events = [
        EventDefinition(ceid=n, name=f"e{n}", rptids=[1000 + n], band=f"b{n}")
        for n in range(4)
    ]
    manager = EventSubscriptionManager(
        _FakeHost(), config=SubscriptionConfig(reports=reports, events=events)
    )
    assert manager.setup_subscriptions() is True
    assert len(manager.band_results) == 4


# ----- vendor-prescribed opening reset -----

def test_no_reset_is_sent_by_default():
    """The delete-all sequence changes a commissioned tool's message flow, so
    nothing may send it unless the machine opts in."""
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions() is True

    for function in (33, 35, 37):
        empty = [
            m for m in host.messages(function)
            if not (m["payload"].get("DATA") or m["payload"].get("CEID"))
        ]
        assert not empty, (
            f"S2F{function} was sent with an empty list without opting in; "
            "that is the SEMI E5 delete-all form"
        )


def test_reset_first_clears_the_tool_before_defining_anything():
    """The MG manual's own lot-start sequence (§9.1 p.170) opens with
    disable-all-events, unlink-all-reports, delete-all-reports - in that
    order - before any definition is sent."""
    host = _FakeHost()
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions(reset_first=True) is True

    # The first three messages are the reset, in the documented order.
    opening = [(m["function"], m["payload"]) for m in host.sent[:3]]
    assert [f for f, _ in opening] == [37, 35, 33], (
        f"reset must be S2F37 -> S2F35 -> S2F33; got {[f for f, _ in opening]}"
    )
    disable, unlink, delete = (payload for _, payload in opening)
    assert disable["CEED"] is False and disable["CEID"] == [], (
        "S2F37 must carry CEED=false with a zero-length CEID list"
    )
    assert unlink["DATA"] == [], "S2F35 must carry a zero-length DATA list"
    assert delete["DATA"] == [], "S2F33 must carry a zero-length DATA list"

    # Disabling events comes before deleting the reports they point at.
    assert host.sent[0]["function"] == 37

    # The real subscription still runs afterwards, band by band.
    assert manager.band_results == {"alpha": True, "beta": True}
    populated_33 = [m for m in host.messages(33) if m["payload"]["DATA"]]
    assert len(populated_33) == 2, "both bands must still be defined"


def test_reset_refusal_does_not_abort_the_subscription():
    """A tool with nothing to clear may answer non-zero. That must not stop
    the middleware from defining its own reports."""
    host = _FakeHost(acks={37: [1], 35: [1], 33: [1]})
    manager = EventSubscriptionManager(host, config=_banded_config())

    assert manager.setup_subscriptions(reset_first=True) is True
    assert manager.band_results == {"alpha": True, "beta": True}
