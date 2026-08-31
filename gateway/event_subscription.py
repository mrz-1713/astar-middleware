"""
Event Subscription Manager for SECS/GEM Gateway

Implements SEMI E5 event subscription protocol:
- S2F33: Define Report
- S2F35: Link Event Report  
- S2F37: Enable/Disable Event Report

This module enables proper event subscription with real semiconductor equipment.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union, cast,
)

if TYPE_CHECKING:
    from .host import GatewayHost

logger = logging.getLogger(__name__)


@dataclass
class ReportDefinition:
    """Definition of a report (mapping of RPTID to DVIDs)."""
    rptid: int
    name: str
    dvids: List[int]
    description: str = ""
    # Optional subscription band (see EventSubscriptionManager.setup_subscriptions).
    # Empty means "the single unnamed band" - i.e. the original behaviour.
    band: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReportDefinition":
        return cls(
            rptid=data["rptid"],
            name=data["name"],
            dvids=data["dvids"],
            description=data.get("description", ""),
            band=data.get("band", ""),
        )


@dataclass
class EventDefinition:
    """Definition of a collection event (mapping of CEID to RPTIDs)."""
    ceid: int
    name: str
    rptids: List[int]
    enabled: bool = True
    description: str = ""
    band: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventDefinition":
        return cls(
            ceid=data["ceid"],
            name=data["name"],
            rptids=data["rptids"],
            enabled=data.get("enabled", True),
            description=data.get("description", ""),
            band=data.get("band", ""),
        )


@dataclass
class SubscriptionConfig:
    """Complete event subscription configuration."""
    reports: List[ReportDefinition] = field(default_factory=list)
    events: List[EventDefinition] = field(default_factory=list)
    dvid_names: Dict[int, str] = field(default_factory=dict)
    
    @classmethod
    def from_file(cls, config_path: Union[str, Path]) -> "SubscriptionConfig":
        """Load configuration from JSON file."""
        config_path = Path(config_path)
        
        if not config_path.exists():
            logger.warning(f"Config file not found: {config_path}")
            return cls()
        
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        reports = [ReportDefinition.from_dict(r) for r in data.get("reports", [])]
        events = [EventDefinition.from_dict(e) for e in data.get("events", [])]
        
        # Parse DVID names (keys may be strings in JSON)
        dvid_names = {}
        for k, v in data.get("dvid_names", {}).items():
            dvid_names[int(k)] = v
        
        return cls(reports=reports, events=events, dvid_names=dvid_names)


class EventSubscriptionManager:
    """
    Manages event subscription setup for SECS/GEM equipment.
    
    This class orchestrates the event subscription protocol:
    1. Define reports (S2F33) - which DVIDs go into which report
    2. Link event reports (S2F35) - which reports are sent for which events
    3. Enable events (S2F37) - start/stop event reporting
    
    Usage:
        manager = EventSubscriptionManager(host, config)
        success = await manager.setup_subscriptions()
    """
    
    def __init__(
        self,
        host: "GatewayHost",
        config: Optional[SubscriptionConfig] = None,
        config_path: Optional[Union[str, Path]] = None,
    ):
        """
        Initialize the subscription manager.
        
        Args:
            host: GatewayHost instance for sending messages
            config: Pre-loaded subscription configuration
            config_path: Path to configuration JSON file
        """
        self.host = host
        
        if config:
            self.config = config
        elif config_path:
            self.config = SubscriptionConfig.from_file(config_path)
        else:
            self.config = SubscriptionConfig()
        
        # Track subscription state
        self._reports_defined = False
        self._events_linked = False
        self._events_enabled = False
        # Per-band outcome, populated by setup_subscriptions(). Recorded
        # separately so partial success is observable: with one batch you can
        # only tell that "the subscription failed", not which families of
        # events the tool actually accepted.
        self.band_results: Dict[str, bool] = {}

        logger.info(
            f"EventSubscriptionManager initialized with "
            f"{len(self.config.reports)} reports, {len(self.config.events)} events"
        )
    
    def define_reports(self, reports: Optional[List[ReportDefinition]] = None) -> bool:
        """
        Send S2F33 to define all reports.
        
        S2F33 format:
        L,2
          DATAID (U4)
          L,n (reports)
            L,2
              RPTID (U4)
              L,m (VIDs)
                VID1 (U4)
                VID2 (U4)
                ...
        
        Returns:
            True if all reports were defined successfully
        """
        reports = self.config.reports if reports is None else reports
        if not reports:
            logger.warning("No reports configured to define")
            return True

        try:
            # Build report definitions
            report_list = []
            for report in reports:
                report_list.append({
                    "RPTID": report.rptid,
                    "VID": report.dvids
                })
            
            response = self._send_report_definitions(report_list)
            if response is not None:
                # Check DRACK (Define Report Acknowledge)
                drack = self._extract_ack(response)
                if drack == 0:
                    logger.info(f"Reports defined successfully: {[r.rptid for r in reports]}")
                    self._reports_defined = True
                    return True
                elif drack == 3:
                    logger.warning(
                        "S2F33 DRACK=3; deleting only configured middleware "
                        "report IDs before one verified redefine"
                    )
                    delete_list = [
                        {"RPTID": report.rptid, "VID": []}
                        for report in reports
                    ]
                    delete_response = self._send_report_definitions(delete_list)
                    if self._extract_ack(delete_response) != 0:
                        logger.error("Failed to delete colliding report definitions")
                        return False
                    retry_response = self._send_report_definitions(report_list)
                    if self._extract_ack(retry_response) != 0:
                        logger.error("Failed to redefine reports after collision cleanup")
                        return False
                    self._reports_defined = True
                    return True
                else:
                    logger.error(f"Define Report failed with DRACK={drack}")
                    return False
            else:
                logger.error("No response to S2F33")
                return False
                
        except Exception as e:
            logger.error(f"Error defining reports: {e}")
            return False

    def _send_report_definitions(self, definitions: List[Dict[str, Any]]) -> Any:
        return self.host.send_and_waitfor_response(
            self.host.stream_function(2, 33)(
                {"DATAID": 0, "DATA": definitions}
            )
        )
    
    def link_event_reports(self, events: Optional[List[EventDefinition]] = None) -> bool:
        """
        Send S2F35 to link events to reports.
        
        S2F35 format:
        L,2
          DATAID (U4)
          L,n (events)
            L,2
              CEID (U4)
              L,m (RPTIDs)
                RPTID1 (U4)
                RPTID2 (U4)
                ...
        
        Returns:
            True if all events were linked successfully
        """
        events = self.config.events if events is None else events
        if not events:
            logger.warning("No events configured to link")
            return True

        try:
            # Build event-report links. Events with NO report are skipped, not
            # sent with an empty RPTID list: SEMI E5 reads an empty list as
            # "delete the link for this CEID", which is how a subscription can
            # be acknowledged and still deliver nothing. Such CEIDs are still
            # enabled below, so the tool reports the event with no data.
            link_list = []
            for event in events:
                if not event.rptids:
                    logger.debug(
                        "CEID %s (%s) has no report; enabling without a link "
                        "rather than sending an empty RPTID list",
                        event.ceid, event.name,
                    )
                    continue
                link_list.append({
                    "CEID": event.ceid,
                    "RPTID": event.rptids
                })
            if not link_list:
                self._events_linked = True
                return True

            # Create S2F35 data structure
            s2f35_data = {
                "DATAID": 0,
                "DATA": link_list
            }
            
            # Send S2F35 and wait for S2F36 response
            response = self.host.send_and_waitfor_response(
                self.host.stream_function(2, 35)(s2f35_data)
            )
            
            if response is not None:
                # Check LRACK (Link Report Acknowledge)
                lrack = self._extract_ack(response)
                if lrack == 0:
                    logger.info(f"Events linked successfully: {[link['CEID'] for link in link_list]}")
                    self._events_linked = True
                    return True
                else:
                    logger.error(f"Link Event Report failed with LRACK={lrack}")
                    return False
            else:
                logger.error("No response to S2F35")
                return False
                
        except Exception as e:
            logger.error(f"Error linking event reports: {e}")
            return False
    
    def enable_events(
        self,
        enable: bool = True,
        events: Optional[List[EventDefinition]] = None,
    ) -> bool:
        """
        Send S2F37 to enable or disable event reporting.
        
        S2F37 format:
        L,2
          CEED (Boolean - enable/disable)
          L,n (CEIDs - empty list means all events)
            CEID1 (U4)
            CEID2 (U4)
            ...
        
        Args:
            enable: True to enable, False to disable
            
        Returns:
            True if events were enabled/disabled successfully
        """
        events = self.config.events if events is None else events
        try:
            # Get list of CEIDs to enable
            ceids = [e.ceid for e in events if e.enabled]
            if not ceids:
                # A zero-length CEID list means "every event on the tool".
                # That is never what an empty or all-disabled set asked for,
                # and on a banded subscription it would quietly re-enable the
                # families other bands just refused.
                logger.info("No enabled CEIDs in this set; skipping S2F37")
                return True

            # Create S2F37 data structure
            s2f37_data = {
                "CEED": enable,
                "CEID": ceids if ceids else []  # Empty list = all events
            }
            
            # Send S2F37 and wait for S2F38 response
            response = self.host.send_and_waitfor_response(
                self.host.stream_function(2, 37)(s2f37_data)
            )
            
            if response is not None:
                # Check ERACK (Enable Report Acknowledge)
                erack = self._extract_ack(response)
                if erack == 0:
                    state = "enabled" if enable else "disabled"
                    logger.info(f"Events {state} successfully: {ceids}")
                    self._events_enabled = enable
                    return True
                else:
                    logger.error(f"Enable/Disable Event failed with ERACK={erack}")
                    return False
            else:
                logger.error("No response to S2F37")
                return False
                
        except Exception as e:
            logger.error(f"Error enabling/disabling events: {e}")
            return False
    
    def _bands(self) -> List[Tuple[str, List[ReportDefinition], List[EventDefinition]]]:
        """Group the configuration into independent subscription bands.

        A config with no `band` fields yields exactly one unnamed band, so the
        message sequence is byte-for-byte what it was before banding existed.
        """
        order: List[str] = []
        for item in list(self.config.reports) + list(self.config.events):
            if item.band not in order:
                order.append(item.band)
        return [
            (
                band,
                [r for r in self.config.reports if r.band == band],
                [e for e in self.config.events if e.band == band],
            )
            for band in order
        ]

    def reset_all(self) -> bool:
        """Clear every report definition, link and enable on the equipment.

        The three SEMI E5 "delete all" forms, in the order the NexGen MG manual
        prescribes for its own lot start (§9.1 p.170, traces §9.1.1.2-.4):

            S2F37  CEED=false, zero-length CEID list  -> disable every event
            S2F35  zero-length DATA list              -> drop every CEID link
            S2F33  zero-length DATA list              -> delete every report

        Events are disabled first on purpose: deleting a report that a still-
        enabled CEID is linked to leaves a window in which the tool can fire
        that CEID against a definition that is being torn down.

        This exists for the commissioning case - a tool that has previously
        talked to a different host keeps that host's reports and links, and a
        CEID left linked to a report this middleware then redefines delivers a
        payload against a layout the mapper no longer expects. On a tool that
        has only ever talked to this middleware the whole sequence is a no-op.

        Returns True if the equipment accepted all three. A refusal is logged
        and reported but is NOT fatal: a tool with nothing to clear may answer
        non-zero, and the define/link/enable that follows is what actually
        matters.
        """
        steps = (
            ("S2F37 disable-all-events", 2, 37, {"CEED": False, "CEID": []}),
            ("S2F35 unlink-all-reports", 2, 35, {"DATAID": 0, "DATA": []}),
            ("S2F33 delete-all-reports", 2, 33, {"DATAID": 0, "DATA": []}),
        )
        ok = True
        for label, stream, function, body in steps:
            try:
                response = self.host.send_and_waitfor_response(
                    self.host.stream_function(stream, function)(body)
                )
            except Exception as exc:
                logger.warning(
                    "Subscription reset step %s raised: %s", label, exc
                )
                ok = False
                continue
            if response is None:
                logger.warning("Subscription reset step %s got no reply", label)
                ok = False
                continue
            ack = self._extract_ack(response)
            if ack == 0:
                logger.info("Subscription reset step %s accepted", label)
            else:
                # Not fatal - see the docstring. Recorded so a tool that
                # refuses the reset is visible rather than assumed clean.
                logger.warning(
                    "Subscription reset step %s refused (ack=%s); continuing "
                    "to define this middleware's own reports", label, ack,
                )
                ok = False
        return ok

    def setup_subscriptions(
        self,
        should_continue: Optional[Callable[[], bool]] = None,
        reset_first: bool = False,
    ) -> bool:
        """
        Execute the event subscription sequence, one band at a time.

        Per band:
        1. S2F33 - Define Reports
        2. S2F35 - Link Event Reports
        3. S2F37 - Enable Events

        S2F33 and S2F35 are all-or-nothing per message - equipment rejects the
        entire message when it detects any error, and S2F36 has a dedicated
        ack code for "at least one CEID does not exist". Sending every family
        in one batch therefore lets a single wrong or unimplemented constant
        void the whole subscription. Bands contain the blast radius: a refused
        band degrades that family's feed and leaves the rest reporting.

        Returns:
            True if at least one band was fully subscribed. Per-band outcomes
            are in `band_results`.
        """
        bands = self._bands()
        logger.info("Setting up event subscriptions in %d band(s)...", len(bands))
        self.band_results = {}
        abandoned = False

        if reset_first:
            # Before the first band, never between them: the reset is a
            # delete-ALL, so running it per band would wipe out every band
            # that had already been accepted.
            if should_continue is None or should_continue():
                logger.info(
                    "Clearing the tool's existing report definitions, links "
                    "and event enables before subscribing"
                )
                self.reset_all()

        for band, reports, events in bands:
            # Abandon between bands when the session that asked for this has
            # been stopped or superseded. Each band is three blocking SECS
            # round-trips and a profile can have 31 of them, so without this
            # a stop() issued mid-subscription could not take effect until all
            # ~93 transactions had finished - the worker outlived its join,
            # and a machine being torn down went on configuring the tool it
            # was disconnecting from.
            if should_continue is not None and not should_continue():
                logger.info(
                    "Subscription abandoned before band %r: the session is no "
                    "longer current", band or "all",
                )
                abandoned = True
                break
            # Keyed by the band's own name so a band actually called "all"
            # cannot collide with the unnamed band; only the log says "all".
            label = band or "all"
            ok = (
                self.define_reports(reports)
                and self.link_event_reports(events)
                and self.enable_events(True, events)
            )
            self.band_results[band] = ok
            if ok:
                logger.info(
                    "Subscription band %r accepted (%d reports, %d events)",
                    label, len(reports), len(events),
                )
            else:
                logger.error(
                    "Subscription band %r was REFUSED (%d reports, %d events); "
                    "remaining bands continue independently",
                    label, len(reports), len(events),
                )

        accepted = [name for name, ok in self.band_results.items() if ok]
        refused = [name for name, ok in self.band_results.items() if not ok]
        if abandoned:
            # Not an error: the session was superseded on purpose. But it must
            # not say "completed successfully" either - that would claim every
            # remaining band was applied when they were never attempted.
            logger.info(
                "Event subscription abandoned part-way (session superseded): "
                "%d of %d band(s) applied",
                len(self.band_results), len(bands),
            )
        elif refused:
            logger.error(
                "Event subscription partially applied - accepted: %s | refused: %s",
                accepted or "none", refused,
            )
        else:
            logger.info("Event subscriptions setup completed successfully")
        # The three legacy flags describe the subscription as a whole; a band
        # that took means events really are enabled on the tool.
        subscribed = bool(accepted)
        self._reports_defined = self._events_linked = self._events_enabled = subscribed
        return subscribed

    def requested_ceids(self) -> List[int]:
        """CEIDs this configuration asked the tool to enable."""
        return [event.ceid for event in self.config.events if event.enabled]

    def disable_all_events(self) -> bool:
        """Disable all event reporting."""
        return self.enable_events(enable=False)
    
    def _extract_ack(self, response: Any) -> int:
        """Extract the acknowledgment code (DRACK/LRACK/ERACK) from a reply.

        `response` is the raw secsgem ``Message`` returned by
        ``send_and_waitfor_response``. It MUST be decoded with the
        stream-function codec before the ack code can be read - reading
        ``response.data`` directly yields the leading SECS-II format byte
        (e.g. 0x21 for an ASCII item, 0x21=33), NOT the ack value. That
        misread made every S2F33/S2F35/S2F37 round-trip look like a failure
        on real equipment even though the tool replied DRACK/LRACK/ERACK=0,
        so the middleware never actually enabled any collection events.
        """
        if response is None:
            return -1
        # Defensive: allow an already-decoded int (used by unit tests).
        if isinstance(response, int):
            return response
        # Decode the reply message into its stream-function structure, then
        # collapse to the scalar ack value. S2F34/36/38 are single-item
        # functions (< DRACK > / < LRACK > / < ERACK >) so .get() returns the
        # code directly; we still unwrap lists defensively.
        try:
            decoded = self.host.settings.streams_functions.decode(response)
            value = decoded.get()
        except Exception as exc:  # pragma: no cover - codec failure is fatal anyway
            logger.error("Failed to decode subscription ack response: %s", exc)
            return -1
        while isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, bool):
            return int(cast(Any, value))
        if isinstance(value, (bytes, bytearray)):
            return value[0] if value else -1
        try:
            return int(cast(Any, value))
        except (TypeError, ValueError):
            return -1
    
    @property
    def is_subscribed(self) -> bool:
        """Check if event subscriptions are fully set up."""
        return self._reports_defined and self._events_linked and self._events_enabled
    
    def get_status(self) -> Dict[str, Any]:
        """Get subscription status."""
        return {
            "reports_defined": self._reports_defined,
            "events_linked": self._events_linked,
            "events_enabled": self._events_enabled,
            "is_subscribed": self.is_subscribed,
            "report_count": len(self.config.reports),
            "event_count": len(self.config.events),
            "band_results": dict(self.band_results),
        }
