"""
SECS/GEM Host Handler for EAP Gateway

Implements a GEM host handler for connecting to semiconductor equipment.
Receives and processes collection events (CEIDs) and data variables.
"""

import logging
import socket
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, cast

import secsgem.gem
import secsgem.hsms
import secsgem.secs
from secsgem.gem.communication_state_machine import CommunicationState

from . import e40
from .annotated_reports import SecsS06F13, SecsS06F14
from .identity import SecsS01F02Extended
from .secsgem_compat import (
    install_secsgem_030_thread_cleanup,
    prepare_secsgem_030_passive_shutdown,
)


logger = logging.getLogger(__name__)

# Profile convention: each CEID's own report is numbered CEID + this offset.
RPTID_CEID_OFFSET = 1000000000

# S2F42 HCACK codes that mean the command was accepted. 0 is "will be
# performed"; 4 is "will be performed, completion signalled later by an event"
# and is what both the NexGen MG (manual 5.2 + the traces in 9.1.1) and the
# SPTS fxP Omega (manual 15.2) actually return for their documented commands.
HCACK_ACKNOWLEDGED = 0
HCACK_WILL_PERFORM = 4
HCACK_ACCEPTED = frozenset({HCACK_ACKNOWLEDGED, HCACK_WILL_PERFORM})



def _as_int(value: Any) -> Optional[int]:
    """int(value) or None - RPTIDs come off the wire and may be anything."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class GatewayHost(secsgem.gem.GemHostHandler):
    """
    SECS/GEM Host handler for connecting to equipment.

    This class acts as the host side of the SECS/GEM communication,
    connecting to equipment simulators or real machines to:
    - Establish and maintain HSMS connections
    - Receive collection event reports (S6F11)
    - Request status variables and equipment constants
    - Process incoming data for the gateway pipeline

    Attributes:
        tool_id: Equipment identifier for this connection
        on_event: Callback function for received events
    """

    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        tool_id: str = "UNKNOWN",
        on_event: Optional[Callable[[str, int, Dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[str], None]] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        dv_name_by_id: Optional[Dict[int, str]] = None,
    ):
        """
        Initialize the gateway host handler.

        Args:
            settings: HSMS connection settings
            tool_id: Equipment identifier
            on_event: Callback for events (tool_id, ceid, data_dict)
            on_connect: Callback for connection established (tool_id)
            on_disconnect: Callback for connection lost (tool_id)
            dv_name_by_id: DVID -> name map used to label the VID/V pairs in E40
                Process Job notifications (S16F9). Defaults to empty.
        """
        install_secsgem_030_thread_cleanup()
        super().__init__(settings)

        # DaVinci's documented SOFTREV is 24 characters, exceeding the
        # legacy MDLN codec's 20-character limit while remaining valid ASCII.
        self.settings.streams_functions.update(SecsS01F02Extended)

        self.tool_id = tool_id
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._dv_name_by_id = dv_name_by_id or {}

        # Track connection state
        self._connected = False
        # Set by retire(): this host has been taken out of service and must
        # never again touch the pipeline or answer the tool. See retire().
        self._retired = False
        self._last_event_time: Optional[datetime] = None
        # {band name: accepted?} from the last subscribe_to_events() call.
        self.subscription_band_results: Dict[str, bool] = {}
        self.enabled_event_readback: Optional[List[int]] = None
        self.missing_enabled_events: List[int] = []

        # Alarm callback for S5F1
        self._on_alarm: Optional[Callable[[str, Dict[str, Any]], None]] = None

        # Register SECS-II message handlers
        self.register_stream_function(6, 11, self._handle_s6f11)
        self.register_stream_function(5, 1, self._handle_s5f1)  # Alarm Report

        # S6F13 (Annotated Event Report Send) is the same collection event as
        # S6F11 with each value preceded by its VID. Which of the two a tool
        # sends is a tool-side setting, not a negotiation: the SPTS fxP Omega
        # picks it with equipment constant 4022 (EventReportMsg: 67075=S6F3,
        # 67083=S6F11, 67085=S6F13) and the NexGen MG selects annotated
        # reports per report definition with S2F33's Boolean. secsgem 0.3.0
        # has no S6F13/S6F14 classes, so a tool set to annotated reports used
        # to connect, get every subscription acknowledged, and then deliver
        # nothing this host could decode. Register both so that tool reports
        # through the same pipeline instead of into a silent void.
        try:
            for fn in (SecsS06F13, SecsS06F14):
                self.settings.streams_functions.update(fn)
            self.register_stream_function(6, 13, self._handle_s6f13)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "[%s] could not register the S6F13 annotated-report handler",
                tool_id,
            )

        # E40 event-report style: if the DaVinci HostInterface is configured for
        # E40 instead of E30, collection events arrive as Process Job
        # notifications on Stream 16 (S16F9/S16F7) rather than S6F11. secsgem
        # 0.3.0 has no classes for these, so register the custom ones and handle
        # them, mapping onto the same canonical pipeline. Harmless no-op in E30
        # mode (these messages never arrive there).
        try:
            for fn in (e40.SecsS16F09, e40.SecsS16F07, e40.SecsS16F10, e40.SecsS16F08):
                self.settings.streams_functions.update(fn)
            self.register_stream_function(16, 9, self._handle_s16f9)
            self.register_stream_function(16, 7, self._handle_s16f7)
        except Exception:  # pragma: no cover - defensive
            logger.exception("[%s] could not register E40 (S16) handlers", tool_id)

        # secsgem 0.3.x surfaces connection loss through the protocol event
        # bus, NOT through handler.on_connection_closed (that hook is from the
        # 0.1.x API and is never invoked here). Without wiring this, the GEM
        # communication state machine stays in COMMUNICATING forever after a
        # peer drop, so is_connected never flips False and the reconnect
        # watchdog never fires. Subscribe so a dropped tool is detected.
        try:
            events = cast(Any, self.protocol.events)
            events.disconnected += self._on_protocol_disconnected
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "[%s] could not subscribe to protocol 'disconnected' event",
                tool_id,
            )

        logger.info(f"Gateway host for '{tool_id}' initialized")

    def _on_message_received(self, data: Dict[str, Any]) -> None:
        """Reject SECS data messages addressed to a different session ID."""
        if self._retired:
            # A retired host's socket may still be open (see retire()). Do not
            # decode, do not acknowledge, do not call back: an event this host
            # accepted would be journaled under a session the service has
            # already torn down, and an S6F12 sent from here would tell the
            # tool an event was delivered that nothing is going to act on.
            # Staying silent makes the tool spool and retransmit to the
            # connection the service actually owns.
            logger.debug(
                "[%s] Dropping SECS message on a retired host", self.tool_id
            )
            return
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

    def disable(self) -> None:
        """Disable the host without triggering secsgem's passive close race."""
        prepare_secsgem_030_passive_shutdown(self, self.tool_id)
        super().disable()

    def retire(self) -> None:
        """Take this host out of service permanently and free its socket.

        Every reconnect replaces `SecsMachineSession.host` with a new
        GatewayHost. If the outgoing one keeps its TCP connection, the result
        is an orphan: it is no longer reachable through the session, so
        nothing subscribes or polls on it, yet secsgem's threads keep it alive
        and it goes on acknowledging S6F11/S5F1 and feeding the callbacks.
        Worse, HSMS equipment serves exactly one peer, so the orphan holds the
        only slot and every later host connects to a closed door - the
        watchdog then restarts forever while telemetry flows through a
        connection the service believes does not exist. That is precisely the
        state a NexGen MG rig was found in: 40 minutes of "TOOL_04 is
        disconnected, restarting session" while S6F11/S6F12 kept flowing on
        an unbroken system-byte sequence.

        Retiring is therefore unconditional and must not depend on secsgem's
        cooperation: callbacks are detached first (so a message already in the
        dispatcher queue cannot reach the pipeline), then `disable()` gets its
        chance, and if that raises or leaves the socket open the socket is
        closed directly.
        """
        self._retired = True
        # Detach first: disable() can block, and anything already queued in
        # the protocol dispatcher would otherwise still be delivered.
        self._on_event = None
        self._on_alarm = None
        self._on_connect = None
        self._on_disconnect = None
        self._connected = False
        # Synchronous on purpose. An earlier attempt ran this on a worker
        # thread with a timeout so a wedged secsgem disable() could not hang
        # stop() - but that lets _force_close_socket() below run while
        # disable() is still tearing the connection down, and secsgem 0.3.0
        # keeps module-level state that is not safe for that. Across a full
        # test run the lingering workers deadlocked each other inside
        # secsgem. A bounded stop is not worth a corrupted shutdown: the
        # service-level budget (service.STOP_TIMEOUT_SEC) caps everything
        # else, and this call is left to complete in its own order.
        try:
            self.disable()
        except Exception:
            logger.warning(
                "[%s] secsgem disable() failed while retiring the host; "
                "closing its socket directly",
                self.tool_id,
                exc_info=True,
            )
        if self._force_close_socket():
            logger.warning(
                "[%s] Retired host still held its HSMS socket after "
                "disable(); closed it so the tool's connection slot is free",
                self.tool_id,
            )

    def _force_close_socket(self) -> bool:
        """Close the underlying TCP socket. True if it was still open.

        secsgem 0.3.0's `TcpConnection.disconnect()` returns without closing
        anything when its receiver thread is not running, and
        `TcpClientConnection.disable()` is a no-op when `enabled` is already
        False. Either path leaves a live socket behind, which is exactly the
        orphan retire() exists to prevent, so verify rather than assume.
        """
        try:
            connection = getattr(self.protocol, "_connection", None)
            sock = getattr(connection, "_sock", None)
            if sock is None or getattr(sock, "fileno", lambda: -1)() == -1:
                return False
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already half-closed; the close below is what matters
            sock.close()
            if connection is not None:
                connection._connected = False
            return True
        except Exception:
            logger.debug(
                "[%s] Forced socket close failed", self.tool_id, exc_info=True
            )
            return False

    def _on_state_communicating(self, _: Dict[str, Any]) -> None:
        """Called when communication state changes to COMMUNICATING."""
        # Call the base implementation so secsgem's "handler_communicating"
        # event fires and any waitfor_communicating() waiters are released.
        try:
            super()._on_state_communicating(_)
        except Exception:  # pragma: no cover - base impl is trivial
            logger.debug("base _on_state_communicating raised", exc_info=True)

        self._connected = True
        logger.info(f"[{self.tool_id}] Communication established")

        if self._on_connect:
            try:
                self._on_connect(self.tool_id)
            except Exception as e:
                logger.error(f"Connect callback error: {e}")

    def _handle_s6f11(
        self,
        handler: Any,
        packet: Any
    ) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S6F11 - Event Report Send from equipment.

        This is the primary message for receiving data from equipment.
        Equipment sends this when a collection event is triggered.

        Args:
            handler: The handler that received the message
            packet: The SECS packet containing event data

        Returns:
            S6F12 acknowledgment message

        ACKC6=0 is only returned once the callback has come back cleanly, and
        the callback's first act is to write the message to the durable ingress
        journal. That ordering is the whole no-loss guarantee: the tool is told
        "received" strictly after the message is on disk, so a crash between
        the two cannot make the equipment discard an event we no longer hold.
        Anything that stops us storing it becomes ACKC6=1, which asks the tool
        to send it again.
        """
        logger.debug(f"[{self.tool_id}] Received S6F11 - Event Report")

        try:
            # The packet.data is raw bytes - need to decode it

            data = self._decode_packet_data(6, 11, packet.data)
            logger.debug(f"[{self.tool_id}] S6F11 decoded type: {type(data)}")

            # Parse the event data
            event_data = self._parse_event_data(data)
            event_data["_system_bytes"] = self._system_bytes(packet)
            event_data["_stream"], event_data["_function"] = 6, 11

            # Update tracking
            self._last_event_time = datetime.now()

            # Notify callback if registered
            if self._on_event and event_data:
                ceid = event_data.get("ceid", 0)
                self._on_event(self.tool_id, ceid, event_data)
                # INFO, not DEBUG. Every collection event the tool delivers
                # is recorded here, at the point it has been durably accepted
                # (the callback journals it) and is about to be acknowledged -
                # so the log answers "did the tool send it and did we take it?"
                # for ordinary events, not only for alarms. Reports are
                # summarised (RPTID + value count) rather than dumped; the
                # full payload is in the ingress journal, and the middleware
                # log has to stay readable at production rates.
                logger.info(
                    "[%s] <- S6F11 CEID=%s (%s) [ACKC6=0]",
                    self.tool_id,
                    ceid,
                    self._describe_reports(event_data),
                )
            else:
                # No callback (e.g. the connectivity probe): the event is
                # acknowledged but NOT stored, so claiming it was "durably
                # accepted" would be a lie.
                logger.debug(
                    "[%s] <- S6F11 CEID=%s received without an event callback; "
                    "acknowledged but not stored",
                    self.tool_id,
                    event_data.get("ceid") if event_data else None,
                )
            return self.stream_function(6, 12)(0)
        except Exception as e:
            logger.error(
                "[%s] Refusing S6F11 (ACKC6=1) - not durably accepted: %s",
                self.tool_id, e,
            )
            return self.stream_function(6, 12)(1)

    @staticmethod
    def _describe_reports(event_data: Dict[str, Any]) -> str:
        """One-line summary of an S6F11's reports for the log.

        Shape only - RPTID and how many values each carried. The values
        themselves can run to 172 VIDs on a single SPTS report, and the
        journal already holds every byte, so printing them here would cost
        the log its readability without adding a fact that is not recoverable.
        """
        reports = event_data.get("_reports_raw") or []
        if not reports:
            return "no report"
        parts = [
            f"RPTID {report.get('rptid')}x{len(report.get('values') or [])}"
            for report in reports
        ]
        return ", ".join(parts)

    @staticmethod
    def _system_bytes(packet: Any) -> Optional[int]:
        """The transaction id the equipment reuses when it retransmits.

        SEMI E5 resends an unacknowledged primary message under its original
        system bytes, so this is what distinguishes "the tool is sending that
        event again because we were slow to acknowledge" from "the tool fired a
        second event". The ingress journal keys on it to suppress the first
        case without collapsing the second. None when the transport does not
        expose it, which the journal treats as "cannot tell" and keeps both.
        """
        try:
            return int(getattr(packet.header, "system"))
        except (AttributeError, TypeError, ValueError):
            return None

    def _parse_event_data(self, data: Any) -> Dict[str, Any]:
        """Parse S6F11 while preserving every RPTID/V[] pair."""
        result: Dict[str, Any] = {
            "tool_id": self.tool_id,
            "received_at": datetime.now().astimezone().isoformat()
        }
        if not data:
            return result
        logger.debug(f"[{self.tool_id}] S6F11 raw: {data}")

        def get_value(item: Any) -> Any:
            if hasattr(item, "get"):
                return item.get()
            return item

        if len(data) < 2:
            raise ValueError("S6F11 must contain DATAID and CEID")
        result["dataid"] = get_value(data[0])
        result["ceid"] = get_value(data[1])
        reports: List[Dict[str, Any]] = []
        if len(data) >= 3 and data[2]:
            for report in data[2]:
                if not hasattr(report, "__len__") or len(report) < 2:
                    raise ValueError("S6F11 report must contain RPTID and V list")
                values = report[1]
                reports.append({
                    "rptid": get_value(report[0]),
                    "values": [get_value(value) for value in values],
                })
        result["_reports_raw"] = reports
        if reports:
            expected_rptid = RPTID_CEID_OFFSET + int(result["ceid"])
            selected = next(
                (
                    report for report in reports
                    if _as_int(report["rptid"]) == expected_rptid
                ),
                reports[0],
            )
            result["_rptid"] = selected["rptid"]
            result["_v_raw"] = list(selected["values"])
        return result

    def _handle_s6f13(
        self,
        handler: Any,
        packet: Any
    ) -> secsgem.secs.SecsStreamFunction:
        """Handle S6F13 - Annotated Event Report Send.

        Same collection event as S6F11, with each value preceded by its VID.
        A tool sends this instead of S6F11 when it is configured to (SPTS
        equipment constant 4022, NexGen S2F33's annotated Boolean), so this
        must land on the same pipeline rather than being a special case
        downstream: everything after the parse is byte-for-byte the S6F11
        path, including the ordering that makes ACKC6=0 mean "durably stored".
        """
        logger.debug(f"[{self.tool_id}] Received S6F13 - Annotated Event Report")

        try:
            data = self._decode_packet_data(6, 13, packet.data)
            event_data = self._parse_annotated_event_data(data)
            event_data["_system_bytes"] = self._system_bytes(packet)
            event_data["_stream"], event_data["_function"] = 6, 13

            self._last_event_time = datetime.now()

            if self._on_event and event_data:
                ceid = event_data.get("ceid", 0)
                self._on_event(self.tool_id, ceid, event_data)
                logger.info(
                    "[%s] <- S6F13 CEID=%s (%s) [ACKC6=0]",
                    self.tool_id,
                    ceid,
                    self._describe_reports(event_data),
                )
            else:
                logger.debug(
                    "[%s] <- S6F13 CEID=%s received without an event callback; "
                    "acknowledged but not stored",
                    self.tool_id,
                    event_data.get("ceid") if event_data else None,
                )
            return self.stream_function(6, 14)(0)
        except Exception as e:
            logger.error(
                "[%s] Refusing S6F13 (ACKC6=1) - not durably accepted: %s",
                self.tool_id, e,
            )
            return self.stream_function(6, 14)(1)

    def _parse_annotated_event_data(self, data: Any) -> Dict[str, Any]:
        """Parse S6F13, flattening each report's VID/V pairs.

        The mapper decodes reports positionally (`ceid_dv_layout` /
        `rptid_dv_layout` are ordered DV-name lists), so the annotated pairs
        are flattened into the same `values` list S6F11 produces - the report
        layout is the same either way, S6F13 simply restates each VID.

        The pairs are kept as `_vid_values` as well. They are strictly more
        information than S6F11 carries, and they make the one failure the
        positional path cannot detect - a tool sending a report's values in a
        different order than the layout expects - diagnosable after the fact
        from the journal.
        """
        result: Dict[str, Any] = {
            "tool_id": self.tool_id,
            "received_at": datetime.now().astimezone().isoformat(),
            "_annotated": True,
        }
        if not data:
            return result
        logger.debug(f"[{self.tool_id}] S6F13 raw: {data}")

        def get_value(item: Any) -> Any:
            if hasattr(item, "get"):
                return item.get()
            return item

        if len(data) < 2:
            raise ValueError("S6F13 must contain DATAID and CEID")
        result["dataid"] = get_value(data[0])
        result["ceid"] = get_value(data[1])
        reports: List[Dict[str, Any]] = []
        if len(data) >= 3 and data[2]:
            for report in data[2]:
                if not hasattr(report, "__len__") or len(report) < 2:
                    raise ValueError(
                        "S6F13 report must contain RPTID and a VID/V list"
                    )
                values: List[Any] = []
                pairs: List[Dict[str, Any]] = []
                for pair in report[1]:
                    if not hasattr(pair, "__len__") or len(pair) < 2:
                        raise ValueError(
                            "S6F13 report entry must be a VID/V pair"
                        )
                    vid = get_value(pair[0])
                    value = get_value(pair[1])
                    values.append(value)
                    pairs.append({"vid": vid, "value": value})
                reports.append({
                    "rptid": get_value(report[0]),
                    "values": values,
                    "pairs": pairs,
                })
        result["_reports_raw"] = reports
        if reports:
            expected_rptid = RPTID_CEID_OFFSET + int(result["ceid"])
            selected = next(
                (
                    report for report in reports
                    if _as_int(report["rptid"]) == expected_rptid
                ),
                reports[0],
            )
            result["_rptid"] = selected["rptid"]
            result["_v_raw"] = list(selected["values"])
            result["_vid_values"] = {
                pair["vid"]: pair["value"] for pair in selected["pairs"]
            }
        return result

    # _parse_v_array / _parse_reports / _parse_dvvals used to live here.
    # They were unreachable and, worse, wrong: each carried a hardcoded
    # positional field list (CLOCK, EQID, LOTID, ...) and a DVID->name map
    # (1=CLOCK, 2=EQID, ...) invented for the very first loopback
    # simulator. No shipped profile numbers its variables that way, so
    # anything wired back into them would have relabelled real equipment
    # data silently. Report decoding belongs to the profile layouts in
    # eap_middleware.mapper, which is where _parse_event_data's
    # _reports_raw is consumed.

    def on_connection_established(self, handler: Any = None) -> None:
        """Legacy secsgem 0.1.x hook - never invoked by secsgem 0.3.x (the TCP
        layer drives the protocol event bus instead). Kept as a harmless no-op;
        _on_connect actually fires from _on_state_communicating when GEM reaches
        COMMUNICATING."""
        logger.debug(f"[{self.tool_id}] TCP connection established")

    def on_connection_closed(self, _: Any = None) -> None:
        """Legacy secsgem 0.1.x hook. secsgem 0.3.x reports disconnects through
        the protocol 'disconnected' event (see _on_protocol_disconnected), so
        this is normally not called - we still route it through the shared
        handler in case a future secsgem revision invokes it."""
        self._handle_disconnect()

    def _on_protocol_disconnected(self, _data: Any) -> None:
        """Protocol event-bus callback for HSMS/TCP disconnection (the live
        path on secsgem 0.3.x)."""
        self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """Idempotently react to a connection loss: drop the GEM communication
        state out of COMMUNICATING (so is_connected reflects reality and the
        reconnect watchdog can act) and notify the service exactly once."""
        was_connected = self._connected
        self._connected = False

        # Force the GEM communication state machine back to NOT_COMMUNICATING.
        # secsgem only does this from the dead on_connection_closed hook, so
        # without this is_connected would stay True after a peer drop.
        try:
            if self.communication_state.current == CommunicationState.COMMUNICATING:
                self.communication_state.communicationfail()
        except Exception:
            logger.debug(
                f"[{self.tool_id}] comm-state failover skipped", exc_info=True
            )

        if was_connected:
            logger.info(f"[{self.tool_id}] Connection closed")
            if self._on_disconnect:
                try:
                    self._on_disconnect(self.tool_id)
                except Exception as e:
                    logger.error(f"Disconnect callback error: {e}")

    def request_status(self, svids: Optional[List[int]] = None) -> Dict[int, Any]:
        """
        Request status variables from equipment (S1F3).

        Args:
            svids: List of status variable IDs to request.
                  If None, requests all standard SVs.

        Returns:
            Dictionary of SVID -> value
        """
        requested_svids = [] if svids is None else list(svids)

        try:
            response = self.send_and_waitfor_response(
                self.stream_function(1, 3)(requested_svids)
            )

            if response is None:
                return {}
            # response is a raw secsgem Message; it must be decoded with the
            # stream-function codec before the values can be read. Reading
            # response.data directly yields the raw SECS-II body bytes, which
            # would zip SVIDs against individual header/encoding bytes and
            # publish garbage SVID telemetry every poll interval.
            decoded = self.settings.streams_functions.decode(response)
            values = decoded.get()
            if values is None:
                return {}
            if not isinstance(values, (list, tuple)):
                values = [values]
            # S1F4 returns the values positionally in the same order as the
            # requested SVIDs. Map them back, tolerating a short/long list.
            if requested_svids:
                return {
                    svid: values[i]
                    for i, svid in enumerate(requested_svids)
                    if i < len(values)
                }
            # No requested list means S1F3 asked for everything, and S1F4
            # carries no SVID numbers - positional indices are NOT SVIDs.
            logger.warning(
                "[%s] request_status called with no SVID list; cannot label "
                "the %d returned values", self.tool_id, len(values),
            )
            return {}

        except Exception as e:
            logger.error(f"[{self.tool_id}] Status request failed: {e}")

        return {}

    def execute_remote_command(
        self,
        command: str,
        params: Optional[List[Any]] = None,
    ) -> bool:
        """
        Send a remote command to the equipment (S2F41).

        Args:
            command: Command name (e.g., "START", "STOP")
            params: Optional command parameters

        Returns:
            True if command was acknowledged, False otherwise
        """
        if params is None:
            params = []

        try:
            response = self.send_and_waitfor_response(
                self.stream_function(2, 41)([command, params])
            )

            if response is not None:
                # S2F42 is L,2 { HCACK, L params }; decode and read HCACK.
                # (Unused on the read-only runtime, but kept correct.)
                #
                # 4 is a SUCCESS code, not a failure. NexGen MG manual section
                # 5.2 - revised in v1.1.17 expressly to spell this out - reads
                # "4 = Acknowledge, command will be performed with completion
                # signaled later by an event", and the manual's own lot-start
                # traces show the tool answering S2F42 <B 04> to PPSELECT, MAP
                # and START (sections 9.1.1.11, 9.1.1.12, 9.1.1.14). The Omega
                # manual says the same in prose (section 15.2): "remote
                # commands will be interpreted as 'request action to be
                # initiated' rather than 'do action'. The SPTS fxP equipment
                # will then respond via S2,F42 with HCACK = 4". Accepting only
                # 0 would report every documented remote command as failed.
                hcack = self._decode_ack(response)
                if hcack in HCACK_ACCEPTED:
                    if hcack == HCACK_WILL_PERFORM:
                        logger.info(
                            "[%s] %s accepted (HCACK=4): completion will be "
                            "signalled by a collection event",
                            self.tool_id, command,
                        )
                    return True
                logger.warning(
                    "[%s] %s refused with HCACK=%s", self.tool_id, command, hcack
                )
                return False

        except Exception as e:
            logger.error(f"[{self.tool_id}] Remote command failed: {e}")

        return False

    def subscribe_to_events(
        self,
        config_path: Optional[str] = None,
        events_enabled_svid: Optional[int] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        reset_first: bool = False,
    ) -> bool:
        """
        Subscribe to collection events from equipment.

        This method sets up event subscription by:
        1. S2F33 - Define which DVIDs belong to which reports
        2. S2F35 - Link reports to collection events
        3. S2F37 - Enable event reporting

        Args:
            config_path: Path to EventSubscription.json config file
            events_enabled_svid: SVID reporting the tool's own list of enabled
                collection events. When given, it is read back after
                subscribing and compared with what was requested - the
                acknowledgement alone is not trusted.
            reset_first: send the SEMI E5 delete-all sequence (S2F37/S2F35/
                S2F33 with zero-length lists) before defining anything, so a
                tool carrying a previous host's report configuration starts
                clean. See EventSubscriptionManager.reset_all().

        Returns:
            True if subscription was successful
        """
        from pathlib import Path

        from .event_subscription import EventSubscriptionManager, SubscriptionConfig

        try:
            # Resolve the subscription file. Profiles use repo-relative paths
            # (e.g. "output/davinci200_mc4_hc1/EventSubscription.json"), but the
            # Windows service may run with a different current working directory,
            # so a bare relative path can silently miss. Anchor to the project
            # root (the parent of this gateway package) as a fallback.
            if config_path:
                resolved = Path(config_path)
            else:
                resolved = Path("config") / "EventSubscription.json"

            if not resolved.is_absolute() and not resolved.exists():
                project_root = Path(__file__).resolve().parent.parent
                anchored = project_root / resolved
                if anchored.exists():
                    resolved = anchored

            if not resolved.exists():
                # Fail loudly: loading an empty config would make
                # setup_subscriptions() trivially "succeed" while defining zero
                # reports - the tool would then report no usable events. Surface
                # this so the operator fixes the deploy/path instead of running
                # blind.
                logger.error(
                    "[%s] Event subscription file not found: %s (cwd=%s). "
                    "No reports will be defined; aborting subscription.",
                    self.tool_id, config_path or resolved, Path.cwd(),
                )
                return False

            config = SubscriptionConfig.from_file(resolved)
            if not config.reports and not config.events:
                logger.error(
                    "[%s] Event subscription file %s loaded but contains no "
                    "reports/events; aborting to avoid an empty subscription.",
                    self.tool_id, resolved,
                )
                return False

            # Create subscription manager and setup
            manager = EventSubscriptionManager(self, config=config)
            success = manager.setup_subscriptions(
                should_continue, reset_first=reset_first
            )
            self.subscription_band_results = dict(manager.band_results)

            if success:
                logger.info(f"[{self.tool_id}] Event subscription completed")
            else:
                logger.error(f"[{self.tool_id}] Event subscription failed")

            if events_enabled_svid is not None:
                self.verify_enabled_events(
                    events_enabled_svid, manager.requested_ceids()
                )
            return success

        except Exception as e:
            logger.error(f"[{self.tool_id}] Event subscription error: {e}")
            return False

    def verify_enabled_events(
        self,
        events_enabled_svid: int,
        requested_ceids: List[int],
    ) -> Optional[List[int]]:
        """Read the tool's own enabled-collection-event list and diff it.

        An S2F36/S2F38 ack says the tool accepted the message, not that the
        events are live. This asks the equipment what it thinks is enabled and
        logs anything that was requested but is missing, so a refused band
        shows up as a concrete list of CEIDs instead of a silent empty feed.

        Returns the CEIDs the tool reports as enabled, or None if it could not
        be read (never fatal - this is a diagnostic, not a gate).
        """
        self.enabled_event_readback = None
        self.missing_enabled_events = []
        try:
            values = self.request_status([events_enabled_svid])
        except Exception as exc:
            logger.warning(
                "[%s] Could not read back enabled events (SVID %s): %s",
                self.tool_id, events_enabled_svid, exc,
            )
            return None
        raw = values.get(events_enabled_svid)
        if raw is None:
            logger.warning(
                "[%s] Tool returned no value for enabled-events SVID %s; "
                "cannot confirm the subscription took",
                self.tool_id, events_enabled_svid,
            )
            return None
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        enabled: List[int] = []
        for item in raw:
            try:
                enabled.append(int(item))
            except (TypeError, ValueError):
                continue
        if raw and not enabled:
            # Every item was unparsable. Reporting all CEIDs as disabled
            # would send a field engineer after the wrong problem.
            logger.warning(
                "[%s] Could not parse the tool's enabled-events readback "
                "(%r); cannot confirm the subscription took",
                self.tool_id, raw,
            )
            return None
        missing = sorted(set(requested_ceids) - set(enabled))
        self.enabled_event_readback = enabled
        self.missing_enabled_events = missing
        if missing:
            # WARNING, not ERROR. A read-back that does not match the request
            # is documented behaviour on at least one supported tool: the
            # NexGen MG manual's own lot-start capture enables CEIDs
            # 4,5,13,130,131,140,141 in section 9.1.1.7 and then reads SVID 12
            # back as 4,5,13,143,144,140,141 in section 9.1.1.8. At ERROR this
            # sends a field engineer after a fault the vendor prints in its own
            # example. It is a diagnostic either way - it never gates the
            # subscription.
            logger.warning(
                "[%s] %d of %d requested collection events are not listed as "
                "enabled by the tool: %s. Some equipment reports a different "
                "set than it was given (NexGen MG manual 9.1.1.7 vs 9.1.1.8), "
                "so this is only conclusive alongside an empty event feed.",
                self.tool_id, len(missing), len(requested_ceids), missing,
            )
        else:
            logger.info(
                "[%s] Verified %d collection events enabled on the tool",
                self.tool_id, len(requested_ceids),
            )
        return enabled

    def _handle_s5f1(
        self,
        handler: Any,
        packet: Any
    ) -> secsgem.secs.SecsStreamFunction:
        """
        Handle S5F1 - Alarm Report Send from equipment.

        S5F1 format:
        L,3
          ALCD (B - Alarm Code, bit 7 = set/clear)
          ALID (U4 - Alarm ID)
          ALTX (A - Alarm Text)

        Returns:
            S5F2 acknowledgment (ACKC5 = 0)
        """
        logger.debug(f"[{self.tool_id}] Received S5F1 - Alarm Report")

        try:

            data = self._decode_packet_data(5, 1, packet.data)

            def get_value(item: Any) -> Any:
                if hasattr(item, 'get'):
                    return item.get()
                return item

            alcd = get_value(data[0]) if len(data) > 0 else 0
            alid = get_value(data[1]) if len(data) > 1 else 0
            altx = get_value(data[2]) if len(data) > 2 else ""

            # Bit 7 of ALCD indicates set (1) or clear (0)
            is_set = bool(alcd & 0x80)
            alarm_code = alcd & 0x7F

            action = "SET" if is_set else "CLEARED"
            logger.info(f"[{self.tool_id}] Alarm {action}: ID={alid}, Code={alarm_code}, Text={altx}")

            # Prepare alarm data
            alarm_data = {
                "alid": alid,
                "alcd": alarm_code,
                "altx": altx,
                "is_set": is_set,
                "timestamp": datetime.now().astimezone().isoformat(),
                "_system_bytes": self._system_bytes(packet),
                "_stream": 5,
                "_function": 1,
            }

            # Notify callback if registered. Same contract as S6F11: ACKC5=0
            # goes back only after the alarm is journaled, so an alarm the tool
            # considers delivered is one we can still account for after a crash.
            if self._on_alarm:
                self._on_alarm(self.tool_id, alarm_data)
            return self.stream_function(5, 2)(0)
        except Exception as e:
            logger.error(
                "[%s] Refusing S5F1 (ACKC5=1) - not durably accepted: %s",
                self.tool_id, e,
            )
            return self.stream_function(5, 2)(1)

    def _handle_s16f9(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """Handle S16F9 (Process Job Event Notify) - the E40-style equivalent of
        an S6F11 event. Decode, map onto the canonical pipeline via ceid=0 +
        SECSGEM_RAW_EVENT, mark the connection as alive, then ack with S16F10."""
        logger.debug("[%s] Received S16F9 - Process Job Event (E40)", self.tool_id)
        try:
            decoded = self._decode_packet_data(16, 9, packet.data)
            data = self._secs_to_python(decoded)
            raw_event, event_data = e40.parse_s16f9(data, self._dv_name_by_id)
            event_data["tool_id"] = self.tool_id
            event_data["received_at"] = datetime.now().astimezone().isoformat()
            event_data["_system_bytes"] = self._system_bytes(packet)
            event_data["_stream"], event_data["_function"] = 16, 9
            logger.info("[%s] E40 ProcessJob event: %s (PRJobID=%s)",
                        self.tool_id, raw_event, event_data.get("PRJobID"))
            if self._on_event:
                # Deliberately not caught. S16F10 is a bare confirm with no
                # negative code, so declining to reply is the only way to tell
                # the tool we did not accept the event - secsgem turns the
                # exception into S16F0 (abort) and the tool can resend.
                # Swallowing it here would confirm an event we never stored.
                self._on_event(self.tool_id, 0, event_data)
            self._last_event_time = datetime.now()
        except Exception as exc:
            logger.error(
                "[%s] Refusing S16F9 (no confirm) - not durably accepted: %s",
                self.tool_id, exc,
            )
            raise
        return self.stream_function(16, 10)()

    def _handle_s16f7(self, handler: Any, packet: Any) -> secsgem.secs.SecsStreamFunction:
        """Handle S16F7 (Process Job Alert Notify). Carries PRJOBMILESTONE - the
        cleanest lot/job lifecycle signal in E40 mode. Ack with S16F8."""
        logger.debug("[%s] Received S16F7 - Process Job Alert (E40)", self.tool_id)
        try:
            decoded = self._decode_packet_data(16, 7, packet.data)
            data = self._secs_to_python(decoded)
            raw_event, event_data = e40.parse_s16f7(data, self._dv_name_by_id)
            event_data["tool_id"] = self.tool_id
            event_data["received_at"] = datetime.now().astimezone().isoformat()
            event_data["_system_bytes"] = self._system_bytes(packet)
            event_data["_stream"], event_data["_function"] = 16, 7
            logger.info("[%s] E40 ProcessJob alert: %s (PRJobID=%s)",
                        self.tool_id, raw_event, event_data.get("PRJobID"))
            if self._on_event:
                # See _handle_s16f9: S16F8 has no negative code either, so a
                # failure must surface as an aborted transaction, not a confirm.
                self._on_event(self.tool_id, 0, event_data)
            self._last_event_time = datetime.now()
        except Exception as exc:
            logger.error(
                "[%s] Refusing S16F7 (no confirm) - not durably accepted: %s",
                self.tool_id, exc,
            )
            raise
        return self.stream_function(16, 8)()

    @staticmethod
    def _secs_to_python(value: Any) -> Any:
        """Recursively coerce a decoded secsgem structure into plain Python
        dict/list/scalars so the E40 parsers can treat it uniformly."""
        if hasattr(value, "get") and not isinstance(value, dict):
            try:
                return GatewayHost._secs_to_python(value.get())
            except Exception:
                return value
        if isinstance(value, dict):
            return {k: GatewayHost._secs_to_python(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [GatewayHost._secs_to_python(v) for v in value]
        return value

    def drain_spool(self) -> bool:
        """Request transmission of any messages the equipment spooled while the
        host was disconnected (S6F23, RSDC=Transmit). The equipment re-sends the
        spooled primary messages (S6F11/S5F1) through the normal handlers, then
        clears its spool. Returns True if the equipment acknowledged (RSDA=0).

        Spooling is opt-in on the tool; if it isn't spooling, the equipment
        replies RSDA=2 (no spooled data) which we treat as a harmless no-op.
        """
        try:
            rsdc_transmit = self.settings.data_items.RSDC.TRANSMIT
            response = self.send_and_waitfor_response(
                self.stream_function(6, 23)(rsdc_transmit)
            )
            if response is None:
                logger.warning("[%s] No S6F24 response to spool drain", self.tool_id)
                return False
            rsda = self._decode_ack(response)
            if rsda == 0:
                logger.info("[%s] Spool drain requested (RSDA=0)", self.tool_id)
                return True
            if rsda == 2:
                logger.debug("[%s] Spool drain: no spooled data (RSDA=2)", self.tool_id)
                return True
            logger.warning("[%s] Spool drain denied: RSDA=%s", self.tool_id, rsda)
            return False
        except Exception as exc:
            logger.error("[%s] Spool drain failed: %s", self.tool_id, exc)
            return False

    def set_alarm_callback(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """Set callback for alarm events (S5F1)."""
        self._on_alarm = callback

    def enable_all_alarms(self) -> bool:
        """Enable reporting of ALL alarms via S5F3 (ALED=ENABLE, zero-length ALID).

        SEMI E5: a zero-length ALID list applies the action to every alarm. The
        equipment only sends S5F1 for alarms whose reporting is enabled; on a
        tool that doesn't enable alarms by default this guarantees the
        middleware actually receives them. Enabling alarm *reporting* is a
        data-collection action (same class as S2F33/35/37), not a
        process-affecting command, so it is safe for the read-only runtime.

        Returns True if the equipment acknowledged (ACKC5 == 0).
        """
        try:
            aled_enable = self.settings.data_items.ALED.ENABLE  # 128
            response = self.send_and_waitfor_response(
                self.stream_function(5, 3)({"ALED": aled_enable, "ALID": []})
            )
            if response is None:
                logger.warning(
                    "[%s] No S5F4 response to enable-all-alarms", self.tool_id
                )
                return False
            ackc5 = self._decode_ack(response)
            if ackc5 == 0:
                logger.info("[%s] All alarms enabled (S5F3 ACKC5=0)", self.tool_id)
                return True
            logger.warning(
                "[%s] Enable-all-alarms denied: ACKC5=%s", self.tool_id, ackc5
            )
            return False
        except Exception as e:
            logger.error("[%s] Enable-all-alarms failed: %s", self.tool_id, e)
            return False

    def _decode_ack(self, response: Any) -> int:
        """Decode a single-item ack reply (OFLACK/ONLACK/HCACK/...) to an int.

        ``response`` is the raw secsgem ``Message``; it must be decoded with
        the stream-function codec first. Reading ``response.data`` directly
        returns the SECS-II header byte, not the ack value.
        """
        try:
            value = self.settings.streams_functions.decode(response).get()
        except Exception as exc:
            logger.error("[%s] Failed to decode ack reply: %s", self.tool_id, exc)
            return -1
        if isinstance(value, dict):
            # Structured replies such as S2F42 decode to
            # {"HCACK": value, "PARAMS": [...]}; scalar replies decode
            # directly.  Prefer the well-known acknowledgement fields and
            # retain a single-value fallback for vendor-specific codecs.
            for key in ("HCACK", "ACKC5", "ACKC6", "ERACK", "LRACK", "DRACK"):
                if key in value:
                    value = value[key]
                    break
            else:
                if len(value) != 1:
                    return -1
                value = next(iter(value.values()))
        while isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, bool):
            parsed = _as_int(cast(Any, value))
            return parsed if parsed is not None else -1
        if isinstance(value, (bytes, bytearray)):
            return value[0] if value else -1
        try:
            return int(cast(Any, value))
        except (TypeError, ValueError):
            return -1

    def _decode_packet_data(self, stream: int, function: int, data: Any) -> Any:
        """Decode bytes using secsgem stream function classes when needed."""
        if not isinstance(data, bytes):
            return data
        try:
            message = self.stream_function(stream, function)()
            message.decode(data)
            return message.data
        except Exception as exc:
            logger.error(
                "[%s] Failed to decode S%sF%s bytes: %s",
                self.tool_id,
                stream,
                function,
                exc,
            )
            return data

    def request_offline(self) -> bool:
        """
        Request equipment to go offline (S1F15).

        Returns:
            True if request was accepted (OFLACK = 0)
        """
        try:
            response = self.send_and_waitfor_response(
                self.stream_function(1, 15)()
            )

            if response is not None:
                oflack = self._decode_ack(response)
                if oflack == 0:
                    logger.info(f"[{self.tool_id}] Equipment offline request accepted")
                    return True
                else:
                    logger.warning(f"[{self.tool_id}] Equipment offline request denied: OFLACK={oflack}")
                    return False

        except Exception as e:
            logger.error(f"[{self.tool_id}] Offline request error: {e}")

        return False

    def request_online(self) -> bool:
        """
        Request equipment to go online (S1F17).

        Per the DaVinci Host Interface Manual Data Item Dictionary, ONLACK is:
            0 = ON-LINE Accepted
            1 = ON-LINE not allowed
            2 = Equipment already ON-LINE
        We treat 0 and 2 as success: the goal is simply for the tool to BE
        online so data can flow, and "already online" satisfies that.

        Returns:
            True if the equipment is online (ONLACK 0 or 2), False otherwise.
        """
        try:
            response = self.send_and_waitfor_response(
                self.stream_function(1, 17)()
            )

            if response is not None:
                onlack = self._decode_ack(response)
                if onlack in (0, 2):
                    detail = "accepted" if onlack == 0 else "already online"
                    logger.info(f"[{self.tool_id}] Equipment online request {detail} (ONLACK={onlack})")
                    return True
                else:
                    logger.warning(f"[{self.tool_id}] Equipment online request denied: ONLACK={onlack}")
                    return False

        except Exception as e:
            logger.error(f"[{self.tool_id}] Online request error: {e}")

        return False

    def request_sv_namelist(self, svids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """
        Request status variable namelist (S1F11).

        Args:
            svids: List of SVID to query (None = all)

        Returns:
            List of SV definitions
        """
        try:
            response = self.send_and_waitfor_response(
                self.stream_function(1, 11)(svids if svids else [])
            )

            if response is not None:
                # Decode the S1F12 reply before parsing - response.data is raw
                # SECS-II bytes, not the decoded namelist structure.
                decoded = self.settings.streams_functions.decode(response)
                return self._parse_sv_namelist(decoded.get())

        except Exception as e:
            logger.error(f"[{self.tool_id}] SV namelist request error: {e}")

        return []

    def _parse_sv_namelist(self, data: Any) -> List[Dict[str, Any]]:
        """Parse S1F12 SV namelist response."""
        results = []
        try:
            def get_value(item: Any) -> Any:
                if hasattr(item, "get"):
                    return item.get()
                return item

            # S1F12 format: L,n [L,3 [SVID, SVNAME, UNITS], ...]
            if hasattr(data, '__len__'):
                for sv_data in data:
                    if hasattr(sv_data, '__len__') and len(sv_data) >= 2:
                        results.append({
                            "svid": get_value(sv_data[0]),
                            "svname": get_value(sv_data[1]),
                            "units": get_value(sv_data[2]) if len(sv_data) > 2 else ""
                        })
        except Exception as e:
            logger.error(f"Error parsing SV namelist: {e}")

        return results

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to equipment."""
        # Use the actual communication state from secsgem
        return self.communication_state.current == CommunicationState.COMMUNICATING

    @property
    def last_event_time(self) -> Optional[datetime]:
        """Get timestamp of last received event."""
        return self._last_event_time


# HSMS-SS timer defaults, in seconds. These are the DaVinci Host Interface
# Manual section 4.3.1.2 values and were applied to every machine before timers
# became per-profile; they remain the fallback so a profile that documents no
# timers behaves exactly as before.
DEFAULT_HSMS_TIMERS: Dict[str, int] = {"t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5}

# SEMI E37 permits 1..120 s for each of T3/T5/T6/T7/T8, and both vendor manuals
# that state a range agree on it. Anything outside is a configuration error, not
# a tuning choice.
HSMS_TIMER_MIN = 1
HSMS_TIMER_MAX = 120


def create_host_settings(
    host: str,
    port: int,
    device_id: int = 0,
    mode: str = "active",
    # Passive HSMS listener only; config validation makes this explicit.
    bind_address: str = "0.0.0.0",  # nosec B104
    timers: Optional[Mapping[str, int]] = None,
) -> secsgem.hsms.HsmsSettings:
    """
    Create HSMS settings for a host connection.

    Args:
        host: Equipment IP address (used as remote address in active mode;
            informational only in passive mode).
        port: HSMS TCP port (remote port in active mode, listen port in
            passive mode).
        device_id: SECS device ID, used as session_id
        mode: "active" -> we connect to the equipment;
              "passive" -> we listen on bind_address:port for the equipment.
        bind_address: Address to bind to when mode == "passive"
            (defaults to 0.0.0.0 to accept from any interface).

    Returns:
        HsmsSettings configured for the requested direction.
    """
    # The host's timers must match the tool's, or the side with the shorter
    # value declares a communications failure while the other still considers
    # the transaction open - an intermittent link drop with no error to point
    # at. secsgem's own defaults match the DaVinci values except T7 (8s), so
    # they are always set explicitly rather than left to the library.
    resolved = dict(DEFAULT_HSMS_TIMERS)
    for name, value in (timers or {}).items():
        key = str(name).strip().lower()
        if key not in DEFAULT_HSMS_TIMERS:
            raise ValueError(
                f"Unknown HSMS timer {name!r}; expected one of "
                f"{sorted(DEFAULT_HSMS_TIMERS)}"
            )
        seconds = int(value)
        if not HSMS_TIMER_MIN <= seconds <= HSMS_TIMER_MAX:
            raise ValueError(
                f"HSMS timer {key} must be between {HSMS_TIMER_MIN} and "
                f"{HSMS_TIMER_MAX} seconds; got {seconds}"
            )
        resolved[key] = seconds
    timers = resolved
    if str(mode).lower() == "passive":
        return secsgem.hsms.HsmsSettings(
            address=bind_address,
            port=port,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=device_id,
            **timers,
        )
    return secsgem.hsms.HsmsSettings(
        address=host,
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE,
        session_id=device_id,
        **timers,
    )
