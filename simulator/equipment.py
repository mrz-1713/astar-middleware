"""
SECS/GEM Equipment Simulator

Implements a simulated semiconductor equipment using the secsgem library.
Supports GEM (SEMI E30) standard over HSMS (SEMI E37) communication.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, cast

import secsgem.gem
import secsgem.hsms
import secsgem.secs
import secsgem.secs.variables as secs_var
from secsgem.gem.communication_state_machine import CommunicationState

from .data_generator import DataGenerator, ProcessState
from .secs_data_types import SecsDataTypes, ProductionDataBuilder
from gateway.secsgem_compat import (
    install_secsgem_030_thread_cleanup,
    prepare_secsgem_030_passive_shutdown,
)


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Spool retransmit backoff. A drain that hits a transient failure retries
# rather than giving up: the backlog test in _send_or_spool means an
# abandoned drain silences the tool permanently.
SPOOL_RETRY_BASE_SEC = 0.5
SPOOL_RETRY_MAX_SEC = 30.0


class EquipmentSimulator(secsgem.gem.GemEquipmentHandler):
    """
    SECS/GEM Equipment Simulator for testing EAP Gateway connections.
    
    This class simulates a semiconductor tool that:
    - Communicates via HSMS (High-Speed SECS Message Services)
    - Implements GEM state machine (Control State, Communication State)
    - Reports Collection Events (CEIDs) with Data Variables (DVVALs)
    - Generates realistic tool log data for testing
    
    Attributes:
        tool_id: Unique equipment identifier
        data_generator: Generator for test data
        event_interval: Seconds between automated event generation
    """
    
    # Standard GEM Collection Event IDs
    CEID_PROCESS_STARTED = 1001
    CEID_PROCESS_COMPLETED = 1002
    CEID_WAFER_IN = 1003
    CEID_WAFER_OUT = 1004
    CEID_ALARM_SET = 1005
    CEID_ALARM_CLEAR = 1006
    
    # Standard GEM Data Variable IDs
    DVID_CLOCK = 1
    DVID_EQID = 2
    DVID_LOTID = 3
    DVID_WAFERID = 4
    DVID_RCPID = 5
    DVID_PPSTATE = 6
    DVID_SLOT = 7
    DVID_DIE_X = 8
    DVID_DIE_Y = 9
    DVID_TEST_VALUE = 10
    DVID_BIN_CODE = 11
    DVID_PASS_FAIL = 12
    
    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        tool_id: str = "TOOL_001",
        event_interval: float = 5.0,
        yield_rate: float = 0.95
    ):
        """
        Initialize the equipment simulator.
        
        Args:
            settings: HSMS connection settings
            tool_id: Equipment identifier
            event_interval: Seconds between event generation
            yield_rate: Simulated yield rate (0.0 to 1.0)
        """
        install_secsgem_030_thread_cleanup()
        super().__init__(settings)
        
        self.tool_id = tool_id
        self.event_interval = event_interval
        self.data_generator = DataGenerator(
            tool_id=tool_id,
            yield_rate=yield_rate
        )
        
        # Internal state
        self._running = False
        self._event_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._current_data: Dict[str, Any] = {}
        self._process_state = ProcessState.IDLE
        
        # Event subscription state (for S2F33/S2F35/S2F37)
        self._report_definitions: Dict[int, List[int]] = {}  # RPTID -> [DVIDs]
        self._event_links: Dict[int, List[int]] = {}  # CEID -> [RPTIDs]
        self._enabled_events: Set[int] = set()  # Set of enabled CEIDs
        self._disabled_events: Set[int] = set()
        self._all_events_enabled: bool = False  # If True, all events are enabled
        self._event_reporting_configured: bool = False

        # Equipment-side spool. DaVinci advertises this GEM capability; queued
        # primaries survive a transient disconnect inside the simulator and are
        # retransmitted in order after the host sends S6F23/RSDC=TRANSMIT.
        # NexGen overrides `_spooling_enabled` because its manual says spooling
        # is unsupported.
        self._spooling_enabled = True
        self._spool_limit = 1000
        self._spool_lock = threading.Lock()
        # The single in-flight drain worker, if any. Two workers popping
        # the same queue would interleave the retransmission and destroy
        # the ordering the spool exists to preserve.
        self._spool_drain_worker: Optional[threading.Thread] = None
        self._primary_send_lock = threading.Lock()
        self._spooled_messages: List[Tuple[str, Any]] = []
        self._spool_total = 0
        
        # Event callbacks for external handlers
        self._event_callbacks: List[Callable[[int, Dict[str, Any]], None]] = []
        
        # Register SECS-II stream/function handlers
        self._register_handlers()

        # secsgem 0.3.x reconnects the TCP layer automatically, but its GEM
        # handler does not reset the communication state when the socket drops.
        # Without this reset, a reconnect can be TCP/HSMS-selected while the
        # equipment state machine remains stuck in its previous state.
        protocol_events: Any = self.protocol.events
        protocol_events.disconnected += self._on_protocol_disconnected
        
        logger.info(f"Equipment simulator '{tool_id}' initialized")

    def _on_message_received(self, data: Dict[str, Any]) -> None:
        """Reject SECS data messages addressed to a different session ID."""
        message = data.get("message")
        header = getattr(message, "header", None)
        actual = getattr(header, "session_id", None)
        expected = self.settings.session_id
        if header is None or actual != expected:
            logger.error(
                "[%s] Ignoring SECS message for device/session ID %s; expected %s",
                self.tool_id,
                actual,
                expected,
            )
            return
        if (
            self.communication_state.current == CommunicationState.COMMUNICATING
            and header.stream == 1
            and header.function == 14
        ):
            logger.debug("[%s] Ignoring late S1F14 after simultaneous establish", self.tool_id)
            return
        super()._on_message_received(data)

    def _on_protocol_disconnected(self, _data: Dict[str, Any]) -> None:
        """Reset GEM communication state so the next HSMS Select can recover."""
        try:
            from secsgem.gem.communication_state_machine import CommunicationState

            # secsgem 0.3.0 leaves send_and_waitfor_response callers blocked
            # until T3 after a socket disappears. The application is pinned to
            # that version, so wake its outstanding queues with the same None
            # sentinel returned on a normal response timeout. This lets the lot
            # thread abandon immediately instead of hanging for 45 seconds.
            response_queues = getattr(self.protocol, "_response_queues", {})
            for response_queue in list(response_queues.values()):
                try:
                    response_queue.put_nowait(None)
                except Exception:
                    logger.debug(
                        "[%s] Could not release a pending SECS response waiter",
                        self.tool_id,
                        exc_info=True,
                    )

            if self.communication_state.current != CommunicationState.DISABLED:
                self.communication_state.disable()
                self.communication_state.enable()
        except Exception:
            logger.exception("[%s] Failed to reset GEM state after disconnect", self.tool_id)
    
    def _register_handlers(self) -> None:
        """Register handlers for incoming SECS-II messages."""
        # S1F1: Are You There
        self.register_stream_function(1, 1, self._handle_s1f1)
        
        # S1F3: Selected Equipment Status Request
        self.register_stream_function(1, 3, self._handle_s1f3)
        
        # S1F13: Establish Communications Request
        self.register_stream_function(1, 13, self._handle_s1f13)
        
        # S2F13: Equipment Constant Request
        self.register_stream_function(2, 13, self._handle_s2f13)
        
        # S2F33: Define Report
        self.register_stream_function(2, 33, self._handle_s2f33)
        
        # S2F35: Link Event Report
        self.register_stream_function(2, 35, self._handle_s2f35)
        
        # S2F37: Enable/Disable Event Report
        self.register_stream_function(2, 37, self._handle_s2f37)
        
        # S2F41: Host Command Send
        self.register_stream_function(2, 41, self._handle_s2f41)
        
        # S6F15: Event Report Request
        self.register_stream_function(6, 15, self._handle_s6f15)

        # S6F23: Request Spooled Data
        self.register_stream_function(6, 23, self._handle_s6f23)

    def _decoded_body(self, stream: int, function: int, packet: Any) -> Any:
        """Return the incoming SECS body as plain Python values."""
        data = packet.data
        if isinstance(data, bytes):
            message = self.stream_function(stream, function)()
            message.decode(data)
            data = message.data
        if hasattr(data, "get"):
            try:
                return data.get()
            except Exception:  # pragma: no cover - defensive library boundary
                pass
        return data

    @classmethod
    def _plain_secs(cls, value: Any) -> Any:
        """Recursively unwrap secsgem scalars/lists without stringifying bytes."""
        getter = getattr(value, "get", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # pragma: no cover - defensive library boundary
                pass
        if isinstance(value, dict):
            return {str(key): cls._plain_secs(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._plain_secs(item) for item in value]
        return value
    
    def _handle_s1f1(
        self, handler: Any, packet: Any
    ) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S1F1 - Are You There request.
        
        Returns S1F2 with equipment model and software revision.
        """
        logger.debug(f"[{self.tool_id}] Received S1F1 - Are You There")
        return self.stream_function(1, 2)([
            self.tool_id,           # MDLN - Equipment Model
            "1.0.0"                 # SOFTREV - Software Revision
        ])
    
    def _handle_s1f3(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S1F3 - Selected Equipment Status Request.
        
        Returns S1F4 with requested status variable values.
        """
        logger.debug(f"[{self.tool_id}] Received S1F3 - Status Request")
        
        # Get requested SVIDs from the message. packet.data is raw bytes -
        # decode it, or every "SVID" below is really a byte value.
        requested_svids = self._decode_s1f3_svids(packet.data)
        
        # Build response with current values
        response_values = []
        for svid in (requested_svids if requested_svids else [1, 2, 3]):
            response_values.append(self._get_status_variable(svid))
        
        return self.stream_function(1, 4)(response_values)
    
    def _decode_s1f3_svids(self, data: Any) -> List[Any]:
        """The SVID list out of raw S1F3 bytes."""
        if not data:
            return []
        if isinstance(data, bytes):
            try:
                message = self.stream_function(1, 3)()
                message.decode(data)
                data = message.data
            except Exception as exc:
                logger.error(
                    "[%s] Failed to decode S1F3 bytes: %s", self.tool_id, exc
                )
                return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        values: List[Any] = []
        for item in data:
            getter = getattr(item, "get", None)
            values.append(getter() if callable(getter) else item)
        return values

    def _handle_s1f13(
        self, handler: Any, packet: Any
    ) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S1F13 - Establish Communications Request.
        
        Returns S1F14 acknowledging communication establishment.
        """
        logger.info(f"[{self.tool_id}] Received S1F13 - Establish Communications")
        
        # Return COMMACK = 0 (accepted)
        return self.stream_function(1, 14)([
            0,                      # COMMACK - Accepted
            [
                self.tool_id,       # MDLN
                "1.0.0"             # SOFTREV
            ]
        ])
    
    def _handle_s2f13(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S2F13 - Equipment Constant Request.
        
        Returns S2F14 with requested equipment constant values.
        """
        logger.debug(f"[{self.tool_id}] Received S2F13 - EC Request")
        
        decoded = self._plain_secs(self._decoded_body(2, 13, packet))
        requested_ecids = decoded if isinstance(decoded, list) else []
        response_values = []
        
        for ecid in requested_ecids:
            response_values.append(self._get_equipment_constant(ecid))
        
        return self.stream_function(2, 14)(response_values)
    
    def _handle_s2f33(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S2F33 - Define Report.
        
        S2F33 allows the host to define which Data Variables (DVIDs)
        are included in each Report ID (RPTID).
        
        Format:
        L,2
          DATAID (U4)
          L,n (reports)
            L,2
              RPTID (U4)
              L,m (VIDs)
                VID1, VID2, ...
                
        Response S2F34: DRACK 0=OK, 3=RPTID exists, 4=VID invalid.
        """
        logger.info(f"[{self.tool_id}] Received S2F33 - Define Report")
        
        try:
            data = self._decoded_body(2, 33, packet)
            report_list = data.get("DATA", []) if isinstance(data, dict) else []
            if not report_list:
                self._report_definitions.clear()
                self._event_links.clear()
                return self.stream_function(2, 34)(0)
            parsed = []
            for report in report_list:
                if isinstance(report, dict):
                    rptid, vids = report.get("RPTID", 0), report.get("VID", [])
                elif isinstance(report, (list, tuple)) and len(report) >= 2:
                    rptid, vids = report[0], report[1]
                else:
                    continue
                rptid = rptid.get() if hasattr(rptid, "get") else rptid
                values = [value.get() if hasattr(value, "get") else value for value in vids]
                parsed.append((int(rptid), values))
            if any(rptid in self._report_definitions and vids for rptid, vids in parsed):
                return self.stream_function(2, 34)(3)
            for rptid, vids in parsed:
                if not vids:
                    self._report_definitions.pop(rptid, None)
                    for ceid, linked in list(self._event_links.items()):
                        self._event_links[ceid] = [item for item in linked if item != rptid]
                else:
                    self._report_definitions[rptid] = vids
                logger.info("[%s] Defined Report %s with VIDs: %s", self.tool_id, rptid, vids)
            return self.stream_function(2, 34)(0)
        except Exception as e:
            logger.error(f"[{self.tool_id}] Error in S2F33: {e}")
            return self.stream_function(2, 34)(4)
    
    def _handle_s2f35(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S2F35 - Link Event Report.
        
        S2F35 links Collection Events (CEIDs) to Report IDs (RPTIDs),
        defining which reports are sent when an event occurs.
        
        Format:
        L,2
          DATAID (U4)
          L,n (events)
            L,2
              CEID (U4)
              L,m (RPTIDs)
                RPTID1, RPTID2, ...
                
        Response S2F36:
          LRACK (Binary) - 0=OK, 1=denied, 2=CEID invalid, 3=RPTID invalid
        """
        logger.info(f"[{self.tool_id}] Received S2F35 - Link Event Report")
        
        lrack = 0  # Default: accepted
        
        try:
            data = self._decoded_body(2, 35, packet)
            if data:
                event_list = data.get("DATA", []) if isinstance(data, dict) else []
                if not event_list:
                    self._event_links.clear()
                    return self.stream_function(2, 36)(0)
                parsed_links = []
                for event in event_list:
                    if isinstance(event, dict):
                        ceid = event.get("CEID", 0)
                        rptids = event.get("RPTID", [])
                    elif isinstance(event, (list, tuple)) and len(event) >= 2:
                        ceid = event[0]
                        rptids = list(event[1]) if event[1] else []
                    else:
                        continue
                    
                    # Extract value if SECS variable
                    if hasattr(ceid, 'get'):
                        ceid = ceid.get()
                    parsed_rptids = [
                        r.get() if hasattr(r, 'get') else r for r in rptids
                    ]
                    parsed_links.append((ceid, parsed_rptids))
                if any(
                    rptid not in self._report_definitions
                    for _ceid, rptids in parsed_links
                    for rptid in rptids
                ):
                    return self.stream_function(2, 36)(3)
                for ceid, rptids in parsed_links:
                    if rptids:
                        self._event_links[int(ceid)] = [int(item) for item in rptids]
                    else:
                        self._event_links.pop(int(ceid), None)
                    logger.info(f"[{self.tool_id}] Linked Event {ceid} to Reports: {rptids}")
                
        except Exception as e:
            logger.error(f"[{self.tool_id}] Error in S2F35: {e}")
            lrack = 3  # RPTID error
        
        # Return S2F36 with LRACK
        return self.stream_function(2, 36)(lrack)
    
    def _handle_s2f37(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S2F37 - Enable/Disable Event Report.
        
        S2F37 enables or disables event reporting for specified CEIDs.
        
        Format:
        L,2
          CEED (Boolean) - True=enable, False=disable
          L,n (CEIDs) - empty list means all events
            CEID1, CEID2, ...
            
        Response S2F38:
          ERACK (Binary) - 0=OK, 1=denied, 2=CEID invalid
        """
        logger.info(f"[{self.tool_id}] Received S2F37 - Enable/Disable Event")
        
        erack = 0  # Default: accepted
        
        try:
            data = self._decoded_body(2, 37, packet)
            ceed: Any = False  # Enable/disable flag
            ceids: List[Any] = []    # List of CEIDs
            
            if data:
                # Handle different data formats
                if isinstance(data, dict):
                    ceed = data.get("CEED", False)
                    ceids = data.get("CEID", [])
                elif isinstance(data, (list, tuple)) and len(data) >= 2:
                    ceed = data[0]
                    ceids = list(data[1]) if data[1] else []
                
                # Extract value if SECS variable
                if hasattr(ceed, 'get'):
                    ceed = ceed.get()
                
                # Extract CEID values
                ceids = [c.get() if hasattr(c, 'get') else c for c in ceids]
                
                ceids = [int(ceid) for ceid in ceids]
                self._event_reporting_configured = True
                if ceed:
                    # Enable events
                    if not ceids:
                        # Empty list = enable all events
                        self._all_events_enabled = True
                        self._enabled_events.clear()
                        self._disabled_events.clear()
                        logger.info(f"[{self.tool_id}] All events enabled")
                    else:
                        self._enabled_events.update(ceids)
                        self._disabled_events.difference_update(ceids)
                        logger.info(f"[{self.tool_id}] Enabled events: {ceids}")
                else:
                    # Disable events
                    if not ceids:
                        # Empty list = disable all events
                        self._all_events_enabled = False
                        self._enabled_events.clear()
                        self._disabled_events.clear()
                        logger.info(f"[{self.tool_id}] All events disabled")
                    else:
                        if self._all_events_enabled:
                            self._disabled_events.update(ceids)
                        else:
                            self._enabled_events.difference_update(ceids)
                        logger.info(f"[{self.tool_id}] Disabled events: {ceids}")
                
        except Exception as e:
            logger.error(f"[{self.tool_id}] Error in S2F37: {e}")
            erack = 2  # CEID error
        
        # Return S2F38 with ERACK
        return self.stream_function(2, 38)(erack)
    
    def _handle_s2f41(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S2F41 - Host Command Send.
        
        Executes remote commands from the host.
        """
        logger.info(f"[{self.tool_id}] Received S2F41 - Host Command")
        
        try:
            decoded = self._plain_secs(self._decoded_body(2, 41, packet))
            if not isinstance(decoded, dict):
                return self.stream_function(2, 42)([1, []])
            command = str(decoded.get("RCMD", "")).strip().upper()
            params = decoded.get("PARAMS", []) or []
            if not isinstance(params, list):
                return self.stream_function(2, 42)([3, []])
            hcack = self._accept_remote_command(command, params)
            return self.stream_function(2, 42)([hcack, []])
        except Exception as exc:
            logger.error("[%s] S2F41 handler failed: %s", self.tool_id, exc)
            return self.stream_function(2, 42)([2, []])
    
    def _handle_s6f15(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S6F15 - Event Report Request.
        
        Returns S6F16 with event report data.
        """
        logger.debug(f"[{self.tool_id}] Received S6F15 - Event Report Request")
        
        ceid = packet.data if packet.data else 0
        event_data = self._generate_event_report(ceid)
        
        return self.stream_function(6, 16)(event_data)

    def _handle_s6f23(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """S6F23 Request Spooled Data -> S6F24 RSDA.

        The response is returned before transmission begins; otherwise the
        handler would wait for S6F12/S5F2 while the protocol thread is still
        blocked trying to send S6F24. The drain worker preserves queue order
        and leaves the first unacknowledged primary in place for a later retry.
        """
        try:
            command = self._decoded_body(6, 23, packet)
            command = command.get() if hasattr(command, "get") else command
            command = int(command)
        except (TypeError, ValueError, AttributeError):
            return self.stream_function(6, 24)(1)

        if command == self.settings.data_items.RSDC.PURGE:
            with self._spool_lock:
                self._spooled_messages.clear()
            return self.stream_function(6, 24)(0)
        if command != self.settings.data_items.RSDC.TRANSMIT:
            return self.stream_function(6, 24)(1)
        with self._spool_lock:
            has_data = bool(self._spooled_messages)
        if not self._spooling_enabled or not has_data:
            return self.stream_function(6, 24)(2)
        self._schedule_spool_drain()
        return self.stream_function(6, 24)(0)

    def _schedule_spool_drain(self) -> None:
        """Start the drain worker, unless one is already running.

        Guarded because there are now two callers - the host's S6F23 and
        entry to COMMUNICATING - and two workers popping the same queue would
        interleave the retransmission, which defeats the ordering the spool
        exists to preserve.
        """
        with self._spool_lock:
            if self._spool_drain_worker is not None and self._spool_drain_worker.is_alive():
                return
            worker = threading.Thread(
                target=self._drain_spooled_messages,
                name=f"SpoolDrain-{self.tool_id}",
                daemon=True,
            )
            self._spool_drain_worker = worker
        worker.start()

    def _queue_spooled(self, label: str, message: Any) -> bool:
        if not self._spooling_enabled:
            return False
        was_empty = False
        with self._spool_lock:
            if len(self._spooled_messages) >= self._spool_limit:
                # DaVinci's OverWriteSpool default is enabled. Mirror that
                # documented behaviour: discard the oldest item, never a
                # random or newest one, and surface the event in the log.
                dropped, _message = self._spooled_messages.pop(0)
                logger.error("[%s] Spool full; overwriting oldest %s", self.tool_id, dropped)
            was_empty = not self._spooled_messages
            self._spooled_messages.append((label, message))
            self._spool_total += 1
        logger.info("[%s] Spooled %s", self.tool_id, label)
        # The first message into an empty spool must (re)start the drain.
        # Without this, a drain worker that exited because the spool was empty
        # is never restarted for a message that arrives while COMMUNICATING,
        # and the backlog flag in _send_or_spool strands every later event.
        if was_empty:
            self._schedule_spool_drain()
        return True

    def _send_or_spool(self, label: str, message: Any) -> bool:
        """Send one primary, or durably retain its order in the tool spool.

        This method owns the per-message log line, because it is the only
        place that knows which of the two happened. Callers used to log
        "->/spool <label>" for both outcomes, so a five-minute capture of a
        perfectly healthy link read as 640 spooled messages and there was no
        way to tell a delivered event from a retained one.
        """
        with self._spool_lock:
            backlog = bool(self._spooled_messages)
        if backlog or self.communication_state.current != CommunicationState.COMMUNICATING:
            return self._queue_spooled(label, message)
        try:
            with self._primary_send_lock:
                response = self.send_and_waitfor_response(message)
        except Exception as exc:
            logger.warning("[%s] %s send failed; spooling: %s", self.tool_id, label, exc)
            return self._queue_spooled(label, message)
        if response is None:
            logger.warning("[%s] %s unacknowledged; spooling", self.tool_id, label)
            return self._queue_spooled(label, message)
        logger.info("[%s] -> %s", self.tool_id, label)
        return True

    def _drain_spooled_messages(self) -> None:
        """Retransmit the spool in order, retrying while the link holds.

        One transient failure used to end the drain for good. Together with
        the backlog test in `_send_or_spool` - which refuses to send anything
        new while a backlog exists, so the stream stays ordered - that made a
        single unacknowledged retransmit permanent: the queue never emptied,
        so every later event joined it, and the tool went silent for the rest
        of its run. Retrying while COMMUNICATING keeps the one-off case
        (a host mid-restart) from becoming terminal, and dropping out when
        communication ends is safe because entry to COMMUNICATING schedules
        the drain again.
        """
        failures = 0
        while (
            self._running
            and self.communication_state.current == CommunicationState.COMMUNICATING
        ):
            with self._spool_lock:
                if not self._spooled_messages:
                    # Clear the handle atomically with the emptiness check. A
                    # _schedule_spool_drain (reconnect) or _queue_spooled
                    # (empty->non-empty) that runs after this instant sees a
                    # None handle and starts a fresh worker; one that ran
                    # before sees this worker and skips. Either way a freshly
                    # spooled message is never stranded behind a worker that
                    # has decided to exit but not yet died.
                    self._spool_drain_worker = None
                    if failures:
                        logger.info("[%s] Spool drained", self.tool_id)
                    return
                label, message = self._spooled_messages[0]
            problem: Optional[str] = None
            try:
                with self._primary_send_lock:
                    response = self.send_and_waitfor_response(message)
            except Exception as exc:
                problem = str(exc)
                response = None
            else:
                if response is None:
                    problem = "not acknowledged"
            if problem is not None:
                failures += 1
                delay = min(SPOOL_RETRY_MAX_SEC, SPOOL_RETRY_BASE_SEC * (2 ** min(failures - 1, 6)))
                logger.warning(
                    "[%s] Spool retransmit %s failed (%s); %d message(s) still "
                    "held, retrying in %.1fs",
                    self.tool_id, label, problem, self.spool_count(), delay,
                )
                if self._stop_event.wait(delay):
                    return
                continue
            failures = 0
            with self._spool_lock:
                if self._spooled_messages and self._spooled_messages[0][1] is message:
                    self._spooled_messages.pop(0)
                remaining = len(self._spooled_messages)
            logger.info(
                "[%s] -> %s (from spool, %d still held)",
                self.tool_id, label, remaining,
            )

    def spool_count(self) -> int:
        with self._spool_lock:
            return len(self._spooled_messages)
    
    def _get_status_variable(self, svid: int) -> Any:
        """Get current value of a status variable."""
        status_map = {
            1: datetime.now().strftime("%Y%m%d%H%M%S"),  # Clock
            2: self.tool_id,                              # Equipment ID
            3: self._process_state.value,                 # Process State
        }
        return status_map.get(svid, "")
    
    def _get_equipment_constant(self, ecid: int) -> Any:
        """Get value of an equipment constant."""
        ec_map = {
            1: self.tool_id,           # Equipment ID
            2: self.event_interval,    # Event interval
            3: 25,                     # Max wafers per lot
        }
        return ec_map.get(ecid, 0)
    
    def _remote_command_profile(self) -> str:
        return "generic"

    def _accept_remote_command(self, command: str, params: List[Any]) -> int:
        """Validate, apply, and report one conservative remote command.

        HCACK: 0 completed, 1 unknown command, 2 cannot perform/wrong state,
        3 invalid parameter, 4 accepted asynchronous work.
        """
        allowed = {"START", "STOP", "PAUSE", "RESUME"}
        if command not in allowed:
            logger.warning("[%s] Unknown command: %s", self.tool_id, command)
            return 1
        if params:
            logger.warning("[%s] %s has unsupported parameters", self.tool_id, command)
            return 3
        transitions = {
            "START": ({ProcessState.IDLE}, ProcessState.EXECUTING),
            "STOP": ({ProcessState.EXECUTING, ProcessState.PAUSED}, ProcessState.IDLE),
            "PAUSE": ({ProcessState.EXECUTING}, ProcessState.PAUSED),
            "RESUME": ({ProcessState.PAUSED}, ProcessState.EXECUTING),
        }
        valid_states, target = transitions[command]
        if self._process_state not in valid_states:
            logger.warning(
                "[%s] %s refused in process state %s",
                self.tool_id,
                command,
                self._process_state.value,
            )
            return 2
        self._process_state = target
        logger.info("[%s] %s accepted -> %s", self.tool_id, command, target.value)
        return 4 if self._remote_command_profile() == "spts_fxp_omega" else 0

    def _execute_command(self, command: str) -> None:
        """Backward-compatible direct command helper used by older callers."""
        self._accept_remote_command(str(command).strip().upper(), [])
    
    def _is_event_enabled(self, ceid: int) -> bool:
        """
        Check if a collection event is enabled for reporting.
        
        Events are enabled when:
        1. _all_events_enabled is True (S2F37 with empty CEID list)
        2. OR the specific CEID is in _enabled_events
        3. OR no S2F37 has been received yet (backward compatibility)
        
        Args:
            ceid: Collection Event ID to check
            
        Returns:
            True if event should be sent
        """
        if not self._event_reporting_configured:
            return True
        
        # Check if all events are enabled
        if self._all_events_enabled:
            return ceid not in self._disabled_events
        
        # Check if specific event is enabled
        return ceid in self._enabled_events
    
    def _generate_event_report(self, ceid: int) -> List[Any]:
        """
        Generate event report data for a collection event with proper SECS-II types.
        
        Returns a list structure matching SEMI E5 S6F16 format.
        """
        # Generate fresh data
        self._current_data = self.data_generator.generate_event_data()
        
        # Build report with proper SECS-II data types
        return [
            SecsDataTypes.u4(ceid),
            secs_var.List([
                secs_var.List([SecsDataTypes.u4(self.DVID_CLOCK), SecsDataTypes.ascii(self._current_data.get("CLOCK", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_EQID), SecsDataTypes.ascii(self._current_data.get("EQID", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_LOTID), SecsDataTypes.ascii(self._current_data.get("LOT_ID", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_WAFERID), SecsDataTypes.ascii(self._current_data.get("WAFER_ID", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_RCPID), SecsDataTypes.ascii(self._current_data.get("RECIPE", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_PPSTATE), SecsDataTypes.ascii(self._current_data.get("PPSTATE", ""))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_SLOT), SecsDataTypes.u1(self._current_data.get("SLOT", 0))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_DIE_X), SecsDataTypes.u2(self._current_data.get("DIE_X", 0))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_DIE_Y), SecsDataTypes.u2(self._current_data.get("DIE_Y", 0))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_TEST_VALUE), SecsDataTypes.f4(self._current_data.get("TEST_VALUE", 0.0))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_BIN_CODE), SecsDataTypes.u1(self._current_data.get("BIN_CODE", 0))]),
                secs_var.List([SecsDataTypes.u4(self.DVID_PASS_FAIL), SecsDataTypes.ascii(self._current_data.get("PASS_FAIL", ""))]),
            ])
        ]
    
    def add_event_callback(self, callback: Callable[[int, Dict[str, Any]], None]) -> None:
        """
        Add a callback function for event notifications.
        
        Args:
            callback: Function taking (ceid, data_dict) as arguments
        """
        self._event_callbacks.append(callback)
    
    def send_event(self, ceid: int) -> None:
        """
        Send a collection event to the host.
        
        Args:
            ceid: Collection Event ID
        """
        # Check if event is enabled (S2F37 subscription)
        if not self._is_event_enabled(ceid):
            logger.debug(f"[{self.tool_id}] Event {ceid} not enabled, skipping")
            return
        
        # Generate event data
        self._current_data = self.data_generator.generate_event_data()
        self._current_data["CEID"] = ceid
        
        # Get tool event for CEID mapping
        tool_event = self._current_data.get("TOOL_EVENT", "ProcessComplete")
        
        try:
            # Build S6F11 message with AUTHENTIC SECS-II data types
            # Using proper SEMI E5 SECS-II encoding for production compliance
            # Structure: L,3 [DATAID(U4), CEID(U4), L,n[L,2[RPTID(U4), L,m[V...]]]]
            
            # Create SECS-II typed variables for the report
            report_variables = ProductionDataBuilder.build_process_report(
                datetime_str=self._current_data.get("DATETIME", ""),
                tool_event=self._current_data.get("TOOL_EVENT", ""),
                tool_name=self._current_data.get("EAP_TOOLNAME", self.tool_id),
                load_port=self._current_data.get("LOAD_PORT", 1),
                chamber=self._current_data.get("CHAMBER", ""),
                lot_id=self._current_data.get("LOT_ID", ""),
                lot_start_time=self._current_data.get("LOT_START_TIME", ""),
                wafer_qty=self._current_data.get("WAFER_QTY", 0),
                wafer_id=self._current_data.get("WAFER_ID", ""),
                recipe=self._current_data.get("RECIPE", ""),
                slot=self._current_data.get("SLOT", 1),
                ppstate=self._current_data.get("PPSTATE", "")
            )
            
            # Build the complete S6F11 structure with proper types
            s6f11_data = {
                "DATAID": SecsDataTypes.u4(0),      # U4: Data ID
                "CEID": SecsDataTypes.u4(ceid),    # U4: Collection Event ID
                "RPT": [{
                    "RPTID": SecsDataTypes.u4(1),  # U4: Report ID
                    "V": report_variables           # List of typed variables
                }]
            }
            
            # Send the event report
            message = self.stream_function(6, 11)(s6f11_data)
            if not self._send_or_spool(
                f"S6F11 {tool_event} (CEID={ceid})", message
            ):
                logger.warning("[%s] Could not send or spool CEID %s", self.tool_id, ceid)
                return
            
            # Notify callbacks
            for callback in self._event_callbacks:
                try:
                    callback(ceid, self._current_data)
                except Exception as e:
                    logger.error(f"Event callback error: {e}")
                    
        except Exception as e:
            logger.error(f"[{self.tool_id}] Failed to send event: {e}")
            import traceback
            traceback.print_exc()
    
    def _event_loop(self) -> None:
        """Background thread for automatic event generation."""
        logger.info(f"[{self.tool_id}] Event generation started")
        
        while self._running:
            try:
                # Wait for connection
                if self.communication_state.current == CommunicationState.COMMUNICATING:
                    # Send process completed event
                    self.send_event(self.CEID_PROCESS_COMPLETED)
                
                # Wait for next interval
                time.sleep(self.event_interval)
                
            except Exception as e:
                logger.error(f"[{self.tool_id}] Event loop error: {e}")
                time.sleep(1)
        
        logger.info(f"[{self.tool_id}] Event generation stopped")
    
    def start_events(self) -> None:
        """Start automatic event generation."""
        if self._running:
            logger.warning(f"[{self.tool_id}] Events already running")
            return
        
        self._stop_event.clear()
        self._running = True
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name=f"EventThread-{self.tool_id}",
            daemon=True
        )
        self._event_thread.start()
    
    def stop_events(self) -> None:
        """Stop automatic event generation."""
        self._running = False
        self._stop_event.set()
        if self._event_thread and self._event_thread is not threading.current_thread():
            self._event_thread.join(timeout=5)
        self._event_thread = None
    
    def enable(self) -> None:
        """Enable the equipment and start communication."""
        super().enable()
        settings = cast(secsgem.hsms.HsmsSettings, self.settings)
        logger.info(f"[{self.tool_id}] Equipment enabled on port {settings.port}")

    def _on_state_communicating(self, _: Any) -> None:
        """Communication is up: deliver anything held while it was down.

        Without this the spool was a one-way door. `_send_or_spool` refuses to
        send while a backlog exists - correctly, because a spooled stream has
        to stay in order - and the ONLY thing that emptied the backlog was an
        S6F23 from the host. The middleware sends S6F23 only when
        `drain_spool_on_connect: true`, which is false on every shipped
        machine, so one event spooled before the host connected made every
        later event spool too, forever, on a perfectly healthy link. A rig left
        overnight logged thousands of `Spooled S6F11` lines and delivered
        nothing, while HSMS linktests flowed normally in both directions and
        the middleware sat there reporting a good connection.

        Draining here is what real GEM equipment does: SEMI E5 has the tool
        transmit its spool once communications are re-established unless the
        host has purged it. The host's S6F23 remains supported and is now an
        explicit re-request rather than the only way out.
        """
        try:
            super()._on_state_communicating(_)
        except Exception:  # pragma: no cover - base impl is trivial
            logger.debug("base _on_state_communicating raised", exc_info=True)
        with self._spool_lock:
            backlog = len(self._spooled_messages)
        if backlog:
            logger.info(
                "[%s] Communication established with %d spooled message(s) "
                "held; transmitting them in order before any new event",
                self.tool_id, backlog,
            )
            self._schedule_spool_drain()
    
    def disable(self) -> None:
        """Disable the equipment and stop communication."""
        # Signal the event loop first, close the protocol to unblock any
        # outstanding SECS wait, then perform the bounded thread join.
        self._running = False
        self._stop_event.set()
        try:
            self._prepare_passive_listener_shutdown()
            super().disable()
        finally:
            self.stop_events()
        logger.info(f"[{self.tool_id}] Equipment disabled")

    def _prepare_passive_listener_shutdown(self) -> None:
        """Avoid a secsgem 0.3.0 passive-listener shutdown deadlock.

        Its server connection can wait forever if disable() closes a socket
        while the listener thread is blocked in select(). Signal the thread
        and let its bounded select call return naturally before the library's
        shutdown sequence. Marking the connection disabled first prevents its
        disconnect callback from opening a replacement listener.
        """
        prepare_secsgem_030_passive_shutdown(self, self.tool_id)


def create_equipment_settings(
    port: int,
    device_id: int = 0,
    address: str = "127.0.0.1"
) -> secsgem.hsms.HsmsSettings:
    """
    Create HSMS settings for an equipment simulator.
    
    Args:
        port: TCP port to listen on
        device_id: SECS device ID (0-32767), used as session_id
        address: IP address to bind to
        
    Returns:
        HsmsSettings configured for passive (equipment) mode
    """
    return secsgem.hsms.HsmsSettings(
        address=address,
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=device_id
    )
