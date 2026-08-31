"""End-to-end: NexGen MG simulator <-HSMS-> middleware -> per-lot CSV.

Mirrors tests/test_davinci_simulator_e2e.py. Required because the profile-to-CSV
seam cannot reach HSMS, and HSMS is where the install-day risk actually lives:
the ON-LINE request, the HSMS role, and whether a refused subscription band
takes the rest of the feed down with it.

Skipped automatically if secsgem isn't installed.
"""

from __future__ import annotations

import csv
import socket
import struct
import time

import pytest

pytest.importorskip("secsgem")

import secsgem.hsms

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.job_tracker import JobTracker
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import (
    MG_BAND_GEM300_SUBSTRATE,
    NEXGEN_MG_REPORTS,
    ProfileRegistry,
)
from eap_middleware.secs_runtime import SecsMachineSession
from simulator.nexgen_mg_simulator import NexGenMgSimulator


PROFILE_ID = "nexgen_mg_series"
SUBSCRIPTION = "output/nexgen_mg_series/EventSubscription.json"


def _free_port() -> int:
    for _ in range(20):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
            continue
    raise RuntimeError("Could not find a free, immediately-rebindable port")


class _Rig:
    """Simulator + middleware session wired to a real per-lot CSV writer."""

    def __init__(self, tmp_path, display="NEXGEN_MG_SIM", **simulator_kwargs):
        self.port = _free_port()
        self.display = display
        middleware_mode = simulator_kwargs.pop("middleware_hsms_mode", "active")
        simulator_mode = (
            secsgem.hsms.HsmsConnectMode.ACTIVE if middleware_mode == "passive"
            else secsgem.hsms.HsmsConnectMode.PASSIVE
        )
        self.simulator = NexGenMgSimulator(
            settings=secsgem.hsms.HsmsSettings(
                address="127.0.0.1",
                port=self.port,
                connect_mode=simulator_mode,
                session_id=0,
            ),
            tool_id="MG_SIM_TEST",
            step_interval_sec=0.02,
            **simulator_kwargs,
        )
        self.machine = MachineConfig(
            endpoint_id="TOOL_04",
            display_name=display,
            machine_profile=PROFILE_ID,
            host="127.0.0.1",
            port=self.port,
            secs_device_id=0,
            local_csv_path=str(tmp_path / "local"),
            network_csv_path=str(tmp_path / "network"),
            admin_config_path=str(tmp_path / "admin"),
            hsms_mode=middleware_mode,
            request_online=True,
        )
        self.profile = ProfileRegistry().get(PROFILE_ID)
        # The step family links no report, so the load port on those events
        # comes from the chamber binding the wafer-level reports leave behind.
        # Without a tracker the rig cannot exercise that path at all.
        self.tracker = JobTracker()
        self.mapper = CanonicalMapper(self.profile, tracker=self.tracker)
        self.writer = PerLotCsvWriter()
        self.tmp_path = tmp_path
        self.events = []
        self.mapped = []
        self.alarms = []
        self.session = SecsMachineSession(
            machine=self.machine,
            event_callback=self._on_event,
            alarm_callback=lambda _m, alarm: self.alarms.append(alarm),
            connect_callback=lambda *a, **k: None,
            disconnect_callback=lambda *a, **k: None,
            subscription_path=SUBSCRIPTION,
            events_enabled_svid=self.profile.health_events_enabled_svid,
        )

    def _on_event(self, machine, ceid, data):
        self.events.append((ceid, data))
        for event in self.mapper.from_secs_events(machine, ceid, data):
            self.mapped.append(event)
            self.writer.append(machine, self.profile, event)

    def run_until(self, predicate, timeout=40.0):
        self.simulator.enable()
        self.simulator.start_events()
        time.sleep(0.3)
        self.session.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self):
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        for shutdown in (self.session.stop, self.simulator.disable):
            try:
                shutdown()
            except Exception:
                pass
        self.writer.flush_all(reason="test_teardown")

    def seen_ceids(self):
        return {ceid for ceid, _ in self.events if ceid > 0}

    def csv_files(self):
        out = {}
        for path in sorted((self.tmp_path / "local").glob("*.csv")):
            with path.open(newline="", encoding="utf-8") as handle:
                out[path.name] = list(csv.reader(handle))
        return out


def test_mg_simulator_two_ports_two_modules_produce_separate_lot_files(tmp_path):
    """The condition that would break naive attribution, over real HSMS."""
    rig = _Rig(tmp_path, wafers_per_lot=2, fire_alarm=True)
    try:
        done = rig.run_until(
            lambda r: {134, 135}.issubset(r.seen_ceids())
        )
    finally:
        rig.stop()

    assert done, f"simulator never finished both lots; saw {sorted(rig.seen_ceids())}"
    # Both process modules reported, from both load ports.
    assert {212, 213, 312, 313}.issubset(rig.seen_ceids())

    files = rig.csv_files()
    assert len(files) == 2, list(files)
    by_port = {}
    for name, rows in files.items():
        body = rows[1:]
        ports = {row[3] for row in body}
        assert len(ports) == 1, (name, ports)
        by_port[ports.pop()] = body
    assert set(by_port) == {"1", "2"}

    for port, chamber in (("1", "PM1"), ("2", "PM2")):
        body = by_port[port]
        lots = {row[5] for row in body if row[5]}
        assert len(lots) == 1, (port, lots)
        assert {row[4] for row in body if row[4] != "NA"} == {chamber}
        tool_events = [row[1] for row in body]
        assert tool_events[0] == "Loaded"
        assert tool_events[-1] == "Unloaded"
        assert "Lot_Start" in tool_events and "Lot_End" in tool_events
        assert tool_events.count("Wfr_Start") == 2
        # WaferID comes from the substrate ID the simulator reports.
        wafer_ids = {row[6] for row in body if row[1] == "Wfr_Start"}
        assert all(wafer_id for wafer_id in wafer_ids), wafer_ids

    assert rig.alarms, "expected the S5F1 alarm path to be exercised"


def test_mg_simulator_refused_band_leaves_other_bands_reporting(tmp_path):
    """One refused band must degrade its family, not void the subscription."""
    rig = _Rig(tmp_path, wafers_per_lot=1, fire_alarm=False,
               refuse_band=MG_BAND_GEM300_SUBSTRATE)
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
        host = rig.session.host
        band_results = dict(getattr(host, "subscription_band_results", {}))
        enabled = host.verify_enabled_events(
            rig.profile.health_events_enabled_svid, [212, 850]
        )
    finally:
        rig.stop()

    assert done, f"lots did not complete; saw {sorted(rig.seen_ceids())}"
    assert band_results, "no per-band results were recorded"
    assert band_results.get(MG_BAND_GEM300_SUBSTRATE) is False, band_results
    surviving = {band: ok for band, ok in band_results.items() if band != MG_BAND_GEM300_SUBSTRATE}
    assert surviving and all(surviving.values()), band_results

    # The read-back reflects the refusal rather than the acknowledgement.
    assert enabled is not None
    assert 212 in enabled, "process-module band should still be enabled"
    assert 850 not in enabled, "refused GEM300 band must not read back as enabled"

    # And the feed genuinely survived: full lot files were still produced.
    assert len(rig.csv_files()) == 2, list(rig.csv_files())


def test_mg_simulator_lot_summary_and_recipe_selection_survive_the_wire(tmp_path):
    """The 35-slot lot-completion report is the biggest decode in the profile.

    Nothing else proves the per-lot chemistry block round-trips over real HSMS
    in the VID order the middleware asked for - the CSV contract has no column
    for any of it, so it only ever shows up in telemetry.
    """
    rig = _Rig(tmp_path, wafers_per_lot=1, fire_alarm=False)
    try:
        done = rig.run_until(lambda r: {5, 13}.issubset(r.seen_ceids()))
        summaries = [data for ceid, data in rig.events if ceid == 5]
        selections = [data for ceid, data in rig.events if ceid == 13]
    finally:
        rig.stop()

    assert done, f"lot summary never arrived; saw {sorted(rig.seen_ceids())}"

    summary = rig.mapper.from_secs_event(rig.machine, 5, summaries[0])
    values = summary.telemetry_values()
    assert summary.event_type == "process_end"
    assert summary.lot_id.startswith("MGLOT_")
    assert summary.load_port in ("1", "2")
    assert summary.recipe.startswith("MG_CLEAN_")
    # The chemistry tail decoded in order: the simulator sends 10.0, 11.0, ...
    layout = NEXGEN_MG_REPORTS[5]
    for index, (slot, _vid) in enumerate(layout[5:]):
        assert values[f"raw_{slot}"] == round(10.0 + index, 1), slot

    selection = rig.mapper.from_secs_event(rig.machine, 13, selections[0])
    assert selection.event_type == "recipe_selected"
    assert selection.recipe.startswith("MG_CLEAN_")
    assert selection.load_port in ("1", "2")


def test_mg_simulator_a_two_port_tool_keeps_the_ports_it_has(tmp_path):
    """Port count must not be a pre-install blocker.

    A two-port MG rejects the CEIDs for ports 3 and 4. Because each port owns
    its own band, that must cost exactly those two bands - not the lot-file
    lifecycle of the ports the tool actually has.
    """
    rig = _Rig(tmp_path, wafers_per_lot=1, fire_alarm=False,
               refuse_band="load_port_3")
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
        band_results = dict(getattr(rig.session.host, "subscription_band_results", {}))
    finally:
        rig.stop()

    assert done, f"ports 1 and 2 did not complete; saw {sorted(rig.seen_ceids())}"
    assert band_results.get("load_port_3") is False, band_results
    for band in ("load_port_1", "load_port_2", "load_port_4"):
        assert band_results.get(band) is True, band_results
    # Both real ports still produced a complete, closed lot file.
    files = rig.csv_files()
    assert len(files) == 2, list(files)
    for rows in files.values():
        assert rows[-1][1] == "Unloaded"


def test_mg_simulator_captures_the_cassette_slot_map(tmp_path):
    """Slot-map results, including cross- and double-slotting.

    The count is on portNCasMapped, but whether any slot is cross- or
    double-slotted only exists in the mapResultLastMap status variable, which
    the cassetteMapped report pulls in.
    """
    rig = _Rig(tmp_path, wafers_per_lot=3, fire_alarm=False)
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
        maps = [data for ceid, data in rig.events if ceid == 145]
    finally:
        rig.stop()

    assert done, f"lots did not complete; saw {sorted(rig.seen_ceids())}"
    assert maps, "cassetteMapped never arrived"
    decoded = [
        rig.mapper.from_secs_event(rig.machine, 145, data) for data in maps
    ]
    ports = {event.load_port for event in decoded}
    assert ports == {"1", "2"}, ports
    for event in decoded:
        # "SlotMapGem" (SVID 4306), not "SlotMap": the manual's status-variable
        # encoding, where 3 = CROSSSLOTTED. DVID 2093 "SlotMap" is the E87
        # carrier attribute, where 3 means CORRECTLY OCCUPIED instead.
        slot_map = event.raw_payload["SlotMapGem"]
        assert list(slot_map) == [1, 3, 1], slot_map  # slot 2 cross-slotted
        assert event.event_type == "mapped"


def test_mg_simulator_host_offline_needs_the_online_request(tmp_path):
    """A tool sitting in HOST OFF-LINE reports nothing until S1F17 lifts it."""
    rig = _Rig(tmp_path, wafers_per_lot=1, fire_alarm=False, start_offline=True)
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
        control_state_after = rig.simulator._control_state
    finally:
        rig.stop()

    assert done, (
        "no events arrived from a HOST OFF-LINE tool - the ON-LINE request "
        f"path is broken; saw {sorted(rig.seen_ceids())}"
    )
    assert control_state_after == 4, "S1F17 should have moved the tool ON-LINE"
    assert rig.csv_files()


def test_mg_simulator_works_with_the_middleware_hsms_passive(tmp_path):
    """The manual states no HSMS role, so both must be known to work."""
    rig = _Rig(tmp_path, wafers_per_lot=1, fire_alarm=False,
               middleware_hsms_mode="passive")
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
    finally:
        rig.stop()

    assert done, f"passive middleware never got the lot; saw {sorted(rig.seen_ceids())}"
    assert len(rig.csv_files()) == 2


def test_mg_simulator_cassette_tool_falls_back_to_slot_numbers(tmp_path):
    """Without GEM300 substrate IDs the WaferID column is the cassette slot."""
    rig = _Rig(tmp_path, wafers_per_lot=2, fire_alarm=False, substrate_ids=False)
    try:
        done = rig.run_until(lambda r: {134, 135}.issubset(r.seen_ceids()))
    finally:
        rig.stop()

    assert done, f"lots did not complete; saw {sorted(rig.seen_ceids())}"
    wafer_ids = {
        row[6]
        for rows in rig.csv_files().values()
        for row in rows[1:]
        if row[1] in ("Wfr_Start", "Wfr_End")
    }
    assert wafer_ids == {"1", "2"}, wafer_ids


def test_mg_simulator_walks_the_documented_process_states(tmp_path):
    """The startup and per-lot run-up reach the host over real HSMS.

    Before this the simulator only ever sat in IDLE or PROCESSING, so the
    init, setup and ready paths - and the four bare events that announce them
    - were never on the wire for the middleware to decode.
    """
    rig = _Rig(tmp_path, wafers_per_lot=1, load_ports=(1,))
    try:
        announced = {7, 100, 101, 102, 103}
        assert rig.run_until(lambda r: announced <= r.seen_ceids()), (
            "state-model events missing: "
            f"{sorted(announced - rig.seen_ceids())}"
        )
    finally:
        rig.stop()


def test_mg_step_events_carry_the_load_port_over_real_hsms(tmp_path):
    """F4 end to end: two lots in two chambers, over HSMS, with the real
    subscription. The step family arrives with an empty report, so its load
    port can only come from the chamber binding."""
    rig = _Rig(tmp_path, wafers_per_lot=2)
    try:
        # 223/323 are pm1/pm2MediumStepFinished - the events that link no report.
        assert rig.run_until(
            lambda r: {221, 321} <= r.seen_ceids()
            and any(e.ceid == 221 for e in r.mapped)
            and any(e.ceid == 321 for e in r.mapped)
        ), f"step events never arrived: {sorted(rig.seen_ceids())}"

        by_chamber = {}
        for event in rig.mapped:
            if event.chamber in ("PM1", "PM2") and event.load_port:
                by_chamber.setdefault(event.chamber, set()).add(event.load_port)

        # Each chamber ran exactly one load port's wafers. A chamber holding
        # two different ports means attribution crossed the lots.
        assert by_chamber, "no chamber event was attributed to a load port"
        for chamber, ports in by_chamber.items():
            assert len(ports) == 1, (chamber, ports)
        # ...and the two chambers did not both claim the same port.
        claimed = [next(iter(p)) for p in by_chamber.values()]
        assert len(set(claimed)) == len(claimed), by_chamber
    finally:
        rig.stop()


def test_mg_wafer_finished_reports_arrive_completely_over_real_hsms(tmp_path):
    """The per-wafer metric reports must arrive with every slot filled.

    213/313 are the largest per-wafer reports the MG publishes: the identity
    block plus every flow, temperature, chuck-speed and bevel-etch variable the
    chamber measures. The simulator used to emit them with the identity block
    alone - nine values against a seventy-four slot layout - so the metric
    slots were never on the wire and no end-to-end test could tell a complete
    report from a truncated one.

    That gap is what hid a real defect: PM2's medium temperatures (DVIDs
    1210-1218) were missing from the shipped subscription while PM1's mirrors
    were present, and every simulator test still passed.
    """
    rig = _Rig(tmp_path, wafers_per_lot=1)
    try:
        assert rig.run_until(
            lambda r: {213, 313} <= r.seen_ceids()
        ), f"both wafer-finished events never arrived: {sorted(rig.seen_ceids())}"

        profile = rig.mapper.profile
        for ceid in (213, 313):
            expected = len(profile.ceid_dv_layout[ceid])
            sent = {
                len(data.get("_v_raw") or ())
                for seen, data in rig.events if seen == ceid
            }
            assert sent == {expected}, (
                f"CEID {ceid} arrived with {sorted(sent)} values against a "
                f"{expected}-slot layout; the metric block is not on the wire, "
                "so its decode is untested"
            )
    finally:
        rig.stop()


def test_mg_pm2_wafer_report_carries_medium_temperatures_over_real_hsms(tmp_path):
    """PM2's medium temperatures must survive S2F33 -> S6F11 -> the mapper.

    DVIDs 1210-1218 are the nine rows in manual section 8.2 whose name column
    wraps onto the line below the VID, so a text-layer transcription drops
    exactly those and nothing else. They went missing from CEID 313 while PM1's
    mirrors (1010-1018) stayed on CEID 213, and the symptom was invisible from
    the host side: CEID 313 arrived, decoded, and produced a row - it was just
    nine columns shorter than the PM1 row for the same wafer.

    Asserting on the decoded slot names rather than on the subscription file is
    what makes this end to end: it fails if the VIDs are dropped anywhere
    between the report definition and the canonical event.
    """
    rig = _Rig(tmp_path, wafers_per_lot=1)
    try:
        assert rig.run_until(
            lambda r: {213, 313} <= r.seen_ceids()
        ), f"both wafer-finished events never arrived: {sorted(rig.seen_ceids())}"

        profile = rig.mapper.profile
        decoded = set()
        for ceid in (213, 313):
            layout = profile.ceid_dv_layout[ceid]
            for seen, data in rig.events:
                if seen != ceid:
                    continue
                values = data.get("_v_raw") or ()
                # Only slots that actually carried a value count as decoded.
                decoded.update(name for name, _ in zip(layout, values))

        expected = {
            f"pm{chamber}Med{medium}Temp{stat}Wafer"
            for chamber in (1, 2)
            for medium in (1, 2, 3)
            for stat in ("Avr", "Max", "Min")
        }
        assert expected <= decoded, (
            "medium-temperature slots missing from the decoded wafer reports: "
            f"{sorted(expected - decoded)}"
        )
    finally:
        rig.stop()
