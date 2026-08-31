"""One equipment simulator for every machine profile.

The profile registry already carries each vendor's CEID numbers, per-CEID V[]
layouts and SVID tables, so a simulator does not need per-vendor code: pick the
CEID that the profile itself maps to each lifecycle step, then fill that CEID's
documented DV list. That covers spts_fxp_omega, davinci_200_mc4_hc1 and
nexgen_mg_series out of the box, and ptiq_secsgem (which documents named events
but no numbers) as soon as its CEIDs are supplied.

Everything is overridable, because real tools renumber:

    ProfileSimulator(settings, profile_id="ptiq_secsgem",
                     ceid_overrides={"lot_start": 4001, "lot_end": 4002},
                     svid_values={19: "PTIQ-TOOL-7"})

Run it standalone:

    python -m simulator.profile_simulator --profile spts_fxp_omega --port 5050
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import secsgem.hsms
import secsgem.secs.variables as secs_var

from eap_middleware.profiles import (
    MachineProfile,
    ProfileRegistry,
    profile_with_ceid_overrides,
    profile_with_subscription_file,
)
from .dv_telemetry import telemetry_value
from .secsgem_equipment import SecsGemEquipment
from .secs_data_types import StandardCEID

logger = logging.getLogger(__name__)


# One lot, in order. Each entry is a canonical event_type as produced by
# MachineProfile.resolve_event; WAFER_FLOW repeats per wafer inside the lot.
LOT_FLOW = ("mounted", "loaded", "clamped", "lot_start")
WAFER_FLOW = ("wafer_start", "process_start", "process_end", "wafer_end")
LOT_END_FLOW = ("lot_end", "unloaded", "unmounted")

# The general GEM/EAP-plan collection events, used when a profile publishes no
# numbers of its own (ptiq_secsgem ships its numbers per installation in the EIB
# model export). These are the same CEIDs as the shipped
# config/EventSubscription.json, so a machine pointed at that file maps them
# without any further configuration.
GENERAL_CEIDS: Dict[str, int] = {
    "mounted": StandardCEID.POD_ARRIVED,        # 1001
    "loaded": StandardCEID.POD_ARRIVED,         # 1001
    "lot_start": StandardCEID.LOT_STARTED,      # 1002
    "wafer_start": StandardCEID.WAFER_STARTED,  # 1003
    "process_start": StandardCEID.PROCESSING_STARTED,    # 100
    "process_end": StandardCEID.PROCESSING_COMPLETED,    # 101
    "wafer_end": StandardCEID.WAFER_COMPLETED,  # 1004
    "lot_end": StandardCEID.LOT_COMPLETED,      # 1005
    "unloaded": StandardCEID.POD_REMOVED,       # 1006
    "unmounted": StandardCEID.POD_REMOVED,      # 1006
    # No general clamp event: SEMI defines none, and the shipped subscription
    # file has none either.
}


# Per-profile realism: (MDLN, SOFTREV, ALID, ALTX). S1F2 is [MDLN, SOFTREV]
# per SEMI E5. Only tools whose real strings and alarm ids are documented are
# listed; every other profile answers with its own model name and ALID 1.
PROFILE_DEFAULTS: Dict[str, Tuple[str, str, int, str]] = {
    "davinci_200_mc4_hc1": (
        "DaVinci200",
        "DaVinci200 Version 4.9.3",
        5010001,
        "Aligner: Analog Input Channels in Manual Mode",
    ),
    "nexgen_mg_series": (
        "MG Series",
        "NWS MG 1.1.18",
        1001,
        "PM1: chuck N2 flow below limit",
    ),
}


def _typed_override(secs_type: str, value: Any) -> Any:
    constructors = {
        "A": secs_var.String,
        "ASCII": secs_var.String,
        "STRING": secs_var.String,
        "BOOLEAN": secs_var.Boolean,
        "U1": secs_var.U1,
        "U2": secs_var.U2,
        "U4": secs_var.U4,
        "U8": secs_var.U8,
        "I1": secs_var.I1,
        "I2": secs_var.I2,
        "I4": secs_var.I4,
        "I8": secs_var.I8,
        "F4": secs_var.F4,
        "F8": secs_var.F8,
    }
    constructor = constructors.get(secs_type.upper())
    return constructor(value) if constructor is not None else value


def resolve_flow_ceids(
    profile: MachineProfile,
    overrides: Optional[Mapping[str, int]] = None,
) -> Dict[str, int]:
    """canonical event_type -> the CEID this profile uses for it.

    The lowest matching CEID wins when a profile documents several (a tool with
    two load ports aliases both LP1 and LP2 events to `loaded`); the simulator
    only ever plays port 1.
    """
    chosen: Dict[str, int] = {}
    for event_type in LOT_FLOW + WAFER_FLOW + LOT_END_FLOW:
        candidates = sorted(
            ceid
            for ceid in profile.ceid_aliases
            if profile.resolve_event(ceid=ceid).event_type == event_type
        )
        if candidates:
            chosen[event_type] = candidates[0]
    for event_type, ceid in (overrides or {}).items():
        chosen[str(event_type)] = int(ceid)
    return chosen


class ProfileSimulator(SecsGemEquipment):
    """A GEM equipment that replays one lot using any profile's own CEIDs.

    Subclasses SecsGemEquipment purely to reuse its GEM plumbing (S1F1/S1F3/
    S1F11/S2F13/S5F3 handlers, S6F11 and S5F1 senders, the interruptible lot
    loop). Every DaVinci-specific number it carries is replaced below.
    """

    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        profile_id: str = "davinci_200_mc4_hc1",
        tool_id: str = "SIM_01",
        wafer_count: int = 3,
        step_interval_sec: float = 0.5,
        fire_alarm: bool = True,
        loop_lots: bool = False,
        lot_id_prefix: str = "LOT_SIM",
        ceid_overrides: Optional[Mapping[str, int]] = None,
        svid_values: Optional[Mapping[int, Any]] = None,
        svid_types: Optional[Mapping[int, str]] = None,
        dvid_values: Optional[Mapping[str, Any]] = None,
        dvid_types: Optional[Mapping[str, str]] = None,
        subscription_path: Optional[str] = None,
        alarm_id: int = 0,
        alarm_text: str = "",
        mdln: str = "",
        softrev: str = "",
        registry: Optional[ProfileRegistry] = None,
        replay_all: bool = False,
    ) -> None:
        super().__init__(
            settings=settings,
            tool_id=tool_id,
            wafer_count=wafer_count,
            step_interval_sec=step_interval_sec,
            fire_alarm=fire_alarm,
            loop_lots=loop_lots,
            lot_id_prefix=lot_id_prefix,
        )
        # Sweep every documented CEID instead of the canonical lot flow.
        self.replay_all = bool(replay_all)
        base = (registry or ProfileRegistry()).get(profile_id)
        # Same overlay the middleware applies, so a machine whose CEIDs come
        # from its subscription file is simulated with those same numbers.
        self.profile = profile_with_subscription_file(
            base, subscription_path or base.event_subscription_path
        )
        self.profile = profile_with_ceid_overrides(
            self.profile, ceid_overrides or {}
        )
        self.ceids = resolve_flow_ceids(self.profile, ceid_overrides)
        if not self.ceids:
            # The profile documents nothing at all (ptiq_secsgem) and nothing
            # was overridden. Fall back to placeholders so the simulator still
            # runs, and say so - the host will not map these.
            logger.info(
                "[%s] profile %s publishes no collection events - using the "
                "general GEM CEIDs %s (the same ones in "
                "config/EventSubscription.json). Override with ceid_overrides "
                "or a subscription file to use the tool's real numbers.",
                tool_id, profile_id, sorted(set(GENERAL_CEIDS.values())),
            )
            self.ceids = dict(GENERAL_CEIDS)
        else:
            # A partially-documented profile just doesn't send that step. A
            # tool with no clamp sensor sends no clamp event; inventing a CEID
            # for it would only produce events the host cannot map.
            skipped = [
                step
                for step in LOT_FLOW + WAFER_FLOW + LOT_END_FLOW
                if step not in self.ceids
            ]
            if skipped:
                logger.info(
                    "[%s] profile %s documents no %s event - skipping that step",
                    tool_id, profile_id, "/".join(skipped),
                )
        self.svid_overrides: Dict[int, Any] = {
            int(svid): value for svid, value in (svid_values or {}).items()
        }
        self.svid_override_types = {
            int(svid): str(secs_type).upper()
            for svid, secs_type in (svid_types or {}).items()
        }
        self.dvid_overrides: Dict[str, Any] = {
            str(name).lower().replace("/", "").replace("_", ""): value
            for name, value in (dvid_values or {}).items()
        }
        self.dvid_override_types = {
            str(name).lower().replace("/", "").replace("_", ""): str(
                secs_type
            ).upper()
            for name, secs_type in (dvid_types or {}).items()
        }
        self._last_ceid: Optional[int] = None
        self._svid_names = dict(self.profile.svids_by_name)
        defaults = PROFILE_DEFAULTS.get(
            profile_id,
            (
                self.profile.model,
                f"{self.profile.vendor} SIM 1.0.0",
                1,
                f"{self.profile.vendor} simulated alarm",
            ),
        )
        self.mdln = mdln or defaults[0]
        self.softrev = softrev or defaults[1]
        self.alarm_id = int(alarm_id) if alarm_id else defaults[2]
        self.alarm_text = alarm_text or defaults[3]

    def _remote_command_profile(self) -> str:
        return self.profile.profile_id

    # ----- S1F1/S1F3/S1F11 answer for this profile, not for DaVinci -----

    def _handle_s1f1_davinci(self, handler: Any, packet: Any) -> Any:
        from gateway.identity import SecsS01F02Extended

        # S1F2 is [MDLN, SOFTREV] per SEMI E5.
        return SecsS01F02Extended([self.mdln, self.softrev])

    def _davinci_svid_value(self, svid: int) -> Any:
        if svid in self.svid_overrides:
            return _typed_override(
                self.svid_override_types.get(svid, ""),
                self.svid_overrides[svid],
            )
        name = self._name_of_svid(svid)
        if name is None:
            return 0
        lowered = name.lower()
        if "clock" in lowered:
            return datetime.now().strftime("%y%m%d%H%M%S")
        if "recipe" in lowered and "name" in lowered:
            return self._current_recipe
        if "carrierid" in lowered.replace("/", "").replace("_", ""):
            return self._current_carrier_id
        if "lotid" in lowered.replace("/", "").replace("_", ""):
            return self._current_lot_id
        if "controlstate" in lowered.replace("/", "").replace("_", ""):
            return 5  # ONLINE/REMOTE
        if "processstate" in lowered.replace("/", "").replace("_", ""):
            return 2  # Idle
        if lowered.endswith("mdln") or lowered == "mdln":
            return self.profile.model
        if "softrev" in lowered:
            return "1.0.0"
        if "eventsenabled" in lowered.replace("/", "").replace("_", ""):
            # The list the tool believes is live, not a boolean. The host
            # reads this SVID back after S2F37 to confirm the subscription
            # took (NexGen MG manual 9.1.1.7/9.1.1.8), and a scalar 1 made it
            # report "242 of 243 requested collection events are not listed as
            # enabled" on every single run of the shipped simulator - a false
            # alarm loud enough to train an operator to ignore the log.
            return self._enabled_ceid_readback()
        # LastEventID: the event-liveness watchdog polls this to tell "the
        # tool is idle" from "the tool is firing events we never receive".
        # A constant 0 makes an idle tool and a silent subscription look the
        # same, which is the one distinction the check exists to make.
        if "lasteventid" in lowered.replace("/", "").replace("_", ""):
            return int(self._last_event_ceid)
        return 0

    def _enabled_ceid_readback(self) -> List[int]:
        """Every CEID this simulator would currently report on.

        Mirrors `_is_event_enabled`: before any S2F37 the tool reports on
        everything it documents, after one it reports on what was enabled.
        """
        documented = sorted(self.profile.ceid_aliases)
        if not self._event_reporting_configured:
            return documented
        if self._all_events_enabled:
            return [c for c in documented if c not in self._disabled_events]
        return sorted(self._enabled_events)

    def _name_of_svid(self, svid: int) -> Optional[str]:
        for name, value in self._svid_names.items():
            if value == svid:
                return name
        return None

    # ----- the profile-driven lot -----

    def _dv_value(self, dv_name: str, ctx: Mapping[str, Any]) -> Any:
        """A plausible value for one documented DV name.

        Matching is on the name because that is all a profile layout carries.
        `*List` names get a one-element list: the E90 substrate reports are
        parallel arrays and the mapper expands them row by row.
        """
        if dv_name.endswith("List"):
            inner = self._dv_value(dv_name[: -len("List")], ctx)
            return [inner]
        key = dv_name.lower().replace("/", "").replace("_", "")
        if key in self.dvid_overrides:
            return _typed_override(
                self.dvid_override_types.get(key, ""), self.dvid_overrides[key]
            )
        # Any slot whose name ends in "port" is a port number. The exact-match
        # list this replaces caught LoadPort/OutputPort/InputPort but not
        # UnloadPort or lastStartedWaferLoadPort, so pm1WaferFinished went out
        # claiming PortID=1 and UnloadPort=0 in the same report - a
        # self-contradiction the middleware faithfully carries downstream,
        # because 0 is not a port the tool has.
        if "portid" in key or key.endswith("port"):
            return ctx["port"]
        # "Cid" is how the MG variable table abbreviates CarrierID
        # (lastStartedWaferCid), which the "carrierid" test missed. The suffix
        # alone is NOT enough: E90 "SubstLocID"/"SubstSubstLocID" (a substrate
        # slot location, not a carrier) and SPTS "ECID" (an equipment constant
        # id) also end in "cid", and mislabelling them as carrier ids sent
        # location/constant ids downstream as carrier ids.
        if "carrierid" in key or (
            key.endswith("cid") and key != "ecid" and not key.endswith("locid")
        ):
            return ctx["carrier_id"]
        # Process-module number. 0 is not a module any MG has; this simulator
        # runs one module per lot, named by `module` in the lot context.
        if key.endswith("pmid") or key in ("pm", "moduleid"):
            return ctx.get("module", 1)
        if "lotid" in key or key == "substlotid":
            return ctx["lot_id"]
        # Exact match, not a prefix: SubstIDStatus/SubstIDStatusList is an E90
        # enum, not a substrate id, and feeding it a wafer name breaks the U1
        # encoding of the whole report.
        if "waferid" in key or key in ("substid", "subst"):
            return ctx["wafer_id"]
        if "recipe" in key or key in ("rcpid", "ppid"):
            return ctx["recipe"]
        if key == "clock" or key == "datetime":
            return datetime.now().strftime("%Y%m%d%H%M%S")
        if key == "eqid":
            return self.tool_id
        if "jobid" in key:
            return ctx["job_id"]
        if "slot" in key or "waferno" in key:
            return ctx["slot"]
        if key.endswith("date"):
            return datetime.now().strftime("%Y%m%d")
        if key.endswith("time"):
            return datetime.now().strftime("%H%M%S")
        if "location" in key or "stationid" in key or "locid" in key:
            return ctx["port"]
        # Not an identity slot: if the name says what it measures, send a
        # credible reading instead of 0. 52 of the 74 slots in the MG's
        # pm1WaferFinished report are process telemetry, and every one of them
        # used to go out as <U4 0> - so the path this middleware exists to
        # serve was never exercised end to end. See simulator/dv_telemetry.py.
        reading = telemetry_value(
            dv_name,
            (self.tool_id, ctx.get("lot_id"), ctx.get("wafer_id"), ctx.get("slot")),
        )
        if reading is not None:
            return reading
        return 0

    def _values_for(self, ceid: int, ctx: Mapping[str, Any]) -> List[Any]:
        layout = self.profile.ceid_dv_layout.get(ceid)
        if not layout:
            # No documented report for this CEID: a real tool sends the event
            # with an empty V[]. The mapper still classifies it by CEID.
            return []
        return [self._dv_value(name, ctx) for name in layout]

    def _emit_step(self, event_type: str, ctx: Mapping[str, Any]) -> bool:
        ceid = self.ceids.get(event_type)
        if ceid is None:
            return True  # this profile has no such event; not a failure
        if ceid == self._last_ceid:
            # One CEID can cover two canonical steps - the general GEM
            # POD_ARRIVED is both `loaded` and `mounted`. A tool fires it once.
            return True
        self._last_ceid = ceid
        return self._emit_event(ceid, self._values_for(ceid, ctx))

    def _run_replay_sweep(self, ctx: Mapping[str, Any]) -> bool:
        """Emit every CEID this profile documents, once, in CEID order.

        The lot script only fires the canonical lifecycle steps, which is a
        small fraction of what a profile documents - 10 of 243 for the MG,
        11 of 100 for the SPTS fxP Omega, 11 of 48 for the DaVinci. Reports
        outside that path therefore never reach the middleware's decoder until
        the tool is on the fab floor. This sweep closes that gap for whichever
        profile is loaded, with no per-profile code.

        Values come from `_values_for`, the same builder the lot script uses,
        so the sweep sends the profile's real DV names in the live lot context.

        Physically incoherent by design: state transitions fire out of order
        and mutually exclusive states both fire. It is a decode and
        subscription sweep, not a behaviour model.
        """
        from .event_replay import replay

        sent = replay(
            self.profile,
            self._emit_event,
            values_for=lambda ceid: self._values_for(ceid, ctx),
        )
        total = len(self.profile.ceid_aliases)
        logger.info(
            "[%s] %s replay sweep sent %s of %s documented CEIDs",
            self.tool_id, self.profile.profile_id, sent, total,
        )
        return sent == total

    def _run_one_lot(self) -> bool:
        self._lot_counter += 1
        lot_id = f"{self.lot_id_prefix}_{self._lot_counter:04d}"
        carrier_id = f"CARRIER_{self._lot_counter:04d}"
        recipe = "Recipe_Sim_v1"
        self._current_lot_id = lot_id
        self._current_recipe = recipe
        self._current_carrier_id = carrier_id
        self._last_ceid = None
        ctx: Dict[str, Any] = {
            "port": 1,
            # One process module, fed from port 1. Reports that name a module
            # (lastStartedWaferPmId) get this rather than 0, which is not a
            # module number any MG has.
            "module": 1,
            "lot_id": lot_id,
            "carrier_id": carrier_id,
            "recipe": recipe,
            "job_id": f"JOB_{self._lot_counter:04d}",
            "wafer_id": "",
            "slot": 0,
        }

        if self.replay_all:
            return self._run_replay_sweep(ctx) or self._abandon_lot(lot_id)

        for step in LOT_FLOW:
            if not self._emit_step(step, ctx):
                return self._abandon_lot(lot_id)

        for slot in range(1, self.wafer_count + 1):
            ctx["slot"] = slot
            ctx["wafer_id"] = f"W{self._lot_counter:04d}_{slot:02d}"
            for step in WAFER_FLOW:
                if not self._emit_step(step, ctx):
                    return self._abandon_lot(lot_id)
            if self.fire_alarm and slot == 1:
                if not self._emit_alarm(self.alarm_id, self.alarm_text, True):
                    return self._abandon_lot(lot_id)
                if not self._emit_alarm(self.alarm_id, self.alarm_text, False):
                    return self._abandon_lot(lot_id)

        ctx["wafer_id"] = ""
        ctx["slot"] = 0
        for step in LOT_END_FLOW:
            if not self._emit_step(step, ctx):
                return self._abandon_lot(lot_id)

        self._current_lot_id = ""
        self._current_carrier_id = ""
        logger.info(
            "[%s] %s lot %s done (%d wafers)",
            self.tool_id, self.profile.profile_id, lot_id, self.wafer_count,
        )
        return True


def nexgen_advanced_factory(**kwargs: Any) -> Any:
    """Adapt SimulatorRunner's common constructor to the specialized MG peer."""
    from .nexgen_mg_simulator import NexGenMgSimulator

    kwargs["wafers_per_lot"] = kwargs.pop("wafer_count")
    return NexGenMgSimulator(**kwargs)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Profile-driven SECS/GEM simulator")
    parser.add_argument("--profile", default="davinci_200_mc4_hc1",
                        choices=ProfileRegistry().list_profile_ids())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--hsms-mode", choices=("passive", "active"), default="passive")
    parser.add_argument("--tool-id", default="SIM_01")
    parser.add_argument("--wafers", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--no-alarm", action="store_true")
    parser.add_argument("--replay-all", action="store_true",
                        help="sweep every CEID this profile documents instead "
                             "of the canonical lot flow (decode coverage, not "
                             "a physically coherent lot)")
    parser.add_argument("--mdln", default="", help="S1F2 model name")
    parser.add_argument("--softrev", default="", help="S1F2 software revision")
    parser.add_argument("--subscription", default=None,
                        help="EventSubscription.json supplying this tool's CEIDs")
    parser.add_argument("--ceid", action="append", default=[],
                        metavar="EVENT_TYPE=CEID",
                        help="override one lifecycle CEID, repeatable")
    parser.add_argument("--svid", action="append", default=[],
                        metavar="SVID=VALUE",
                        help="override one SVID's reported value, repeatable")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ceid_overrides: Dict[str, int] = {}
    for item in args.ceid:
        name, _, value = item.partition("=")
        if not value.strip().isdigit():
            parser.error(f"--ceid expects EVENT_TYPE=NUMBER, got {item!r}")
        ceid_overrides[name.strip()] = int(value)
    svid_values: Dict[int, Any] = {}
    for item in args.svid:
        key, _, value = item.partition("=")
        if not key.strip().isdigit():
            parser.error(f"--svid expects SVID=VALUE, got {item!r}")
        svid_values[int(key)] = int(value) if value.strip().lstrip("-").isdigit() else value

    # Same timer defaults as the config-driven runner (resolved_hsms_timers),
    # so a standalone run against the middleware does not sit on secsgem's
    # library T7=8s while the host states T7=10s.
    from gateway.host import DEFAULT_HSMS_TIMERS

    settings = secsgem.hsms.HsmsSettings(
        address=args.host,
        port=args.port,
        connect_mode=(
            secsgem.hsms.HsmsConnectMode.PASSIVE
            if args.hsms_mode == "passive"
            else secsgem.hsms.HsmsConnectMode.ACTIVE
        ),
        session_id=args.device_id,
        **DEFAULT_HSMS_TIMERS,
    )
    simulator = ProfileSimulator(
        replay_all=args.replay_all,
        settings=settings,
        profile_id=args.profile,
        tool_id=args.tool_id,
        wafer_count=args.wafers,
        step_interval_sec=args.interval,
        fire_alarm=not args.no_alarm,
        loop_lots=args.loop,
        ceid_overrides=ceid_overrides,
        svid_values=svid_values,
        subscription_path=args.subscription,
        mdln=args.mdln,
        softrev=args.softrev,
    )
    logger.info(
        "%s simulator on %s:%s (%s) CEIDs=%s",
        args.profile, args.host, args.port, args.hsms_mode, simulator.ceids,
    )
    simulator.enable()
    simulator.start_events()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        simulator.disable()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
