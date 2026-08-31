"""
SECS-II Data Type Utilities for Production SECS/GEM Communication

Provides proper SECS-II data type wrappers for authentic protocol compliance.
Uses secsgem.secs.variables for real SECS-II encoding.

SEMI E5 SECS-II Data Types:
- L (List): Container for other items
- B (Binary): Raw binary data
- BOOLEAN: True/False
- A (ASCII): ASCII string (max 16M chars)
- U1/U2/U4/U8: Unsigned integers (1/2/4/8 bytes)
- I1/I2/I4/I8: Signed integers (1/2/4/8 bytes)
- F4/F8: Floating point (4/8 bytes, IEEE 754)
"""

from datetime import datetime
from typing import Any, Dict, List

import secsgem.secs.variables as secs_var


class SecsDataTypes:
    """
    Factory class for creating proper SECS-II data types.
    
    Ensures all data sent via HSMS uses authentic SECS-II encoding
    that real semiconductor equipment expects.
    """
    
    @staticmethod
    def u1(value: int) -> secs_var.U1:
        """Create unsigned 1-byte integer (0-255)."""
        return secs_var.U1(min(max(int(value), 0), 255))
    
    @staticmethod
    def u2(value: int) -> secs_var.U2:
        """Create unsigned 2-byte integer (0-65535)."""
        return secs_var.U2(min(max(int(value), 0), 65535))
    
    @staticmethod
    def u4(value: int) -> secs_var.U4:
        """Create unsigned 4-byte integer (0-4294967295)."""
        return secs_var.U4(min(max(int(value), 0), 4294967295))
    
    @staticmethod
    def u8(value: int) -> secs_var.U8:
        """Create unsigned 8-byte integer."""
        return secs_var.U8(max(int(value), 0))
    
    @staticmethod
    def i1(value: int) -> secs_var.I1:
        """Create signed 1-byte integer (-128 to 127)."""
        return secs_var.I1(min(max(int(value), -128), 127))
    
    @staticmethod
    def i2(value: int) -> secs_var.I2:
        """Create signed 2-byte integer (-32768 to 32767)."""
        return secs_var.I2(min(max(int(value), -32768), 32767))
    
    @staticmethod
    def i4(value: int) -> secs_var.I4:
        """Create signed 4-byte integer."""
        return secs_var.I4(int(value))
    
    @staticmethod
    def f4(value: float) -> secs_var.F4:
        """Create 4-byte IEEE 754 floating point."""
        return secs_var.F4(float(value))
    
    @staticmethod
    def f8(value: float) -> secs_var.F8:
        """Create 8-byte IEEE 754 floating point (double precision)."""
        return secs_var.F8(float(value))
    
    @staticmethod
    def ascii(value: str) -> secs_var.String:
        """Create ASCII string (SECS-II A type)."""
        return secs_var.String(str(value))
    
    @staticmethod
    def binary(value: bytes) -> secs_var.Binary:
        """Create binary data."""
        return secs_var.Binary(value)
    
    @staticmethod
    def boolean(value: bool) -> secs_var.Boolean:
        """Create boolean value."""
        return secs_var.Boolean(bool(value))
    
    @staticmethod
    def list_(*items: Any) -> secs_var.List:
        """Create SECS-II List containing items."""
        return secs_var.List(list(items))
    
    @staticmethod
    def clock() -> secs_var.String:
        """Create CLOCK format timestamp (A16): YYYYMMDDHHmmSScc."""
        # SEMI E30 GEM clock format: 16 character ASCII
        now = datetime.now()
        clock_str = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 10000:02d}"
        return secs_var.String(clock_str)
    
    @staticmethod
    def clock_12() -> secs_var.String:
        """Create 12-character clock format (A12): YYMMDDHHmmSS."""
        return secs_var.String(datetime.now().strftime("%y%m%d%H%M%S"))


class ProductionDataBuilder:
    """
    Builds production-ready SECS-II message data with proper types.
    
    Uses authentic SECS-II data types as specified in SEMI E5.
    """
    
    @staticmethod
    def build_s6f11(
        dataid: int,
        ceid: int,
        reports: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Build S6F11 Event Report Send data with proper SECS-II types.
        
        SEMI E5 S6F11 structure:
        L,3
          <DATAID>      - Data ID (U4 or I4)
          <CEID>        - Collection Event ID (U4 or I4)
          L,n           - Report list
            L,2
              <RPTID>   - Report ID (U4 or I4)
              L,m       - Variable list
                <V1>
                <V2>
                ...
        
        Args:
            dataid: Data ID (typically 0)
            ceid: Collection Event ID
            reports: List of {"rptid": int, "variables": [...]}
        
        Returns:
            Dictionary with proper SECS-II structure
        """
        return {
            "DATAID": SecsDataTypes.u4(dataid),
            "CEID": SecsDataTypes.u4(ceid),
            "RPT": [
                {
                    "RPTID": SecsDataTypes.u4(rpt["rptid"]),
                    "V": rpt["variables"]
                }
                for rpt in reports
            ]
        }
    
    @staticmethod
    def build_process_report(
        datetime_str: str,
        tool_event: str,
        tool_name: str,
        load_port: int,
        chamber: str,
        lot_id: str,
        lot_start_time: str,
        wafer_qty: int,
        wafer_id: str,
        recipe: str,
        slot: int,
        ppstate: str
    ) -> List[Any]:
        """
        Build process event report variables with authentic SECS-II types.
        
        This matches real semiconductor equipment report format.
        
        Returns:
            List of SECS-II typed variables
        """
        return [
            SecsDataTypes.ascii(datetime_str),     # DATETIME (A26)
            SecsDataTypes.ascii(tool_event),       # TOOL_EVENT (A)
            SecsDataTypes.ascii(tool_name),        # EAP_TOOLNAME (A)
            SecsDataTypes.u1(load_port),           # LOAD_PORT (U1: 1-4)
            SecsDataTypes.ascii(chamber),          # CHAMBER (A)
            SecsDataTypes.ascii(lot_id),           # LOT_ID (A)
            SecsDataTypes.ascii(lot_start_time),   # LOT_START_TIME (A14)
            SecsDataTypes.u1(wafer_qty),           # WAFER_QTY (U1: 0-25)
            SecsDataTypes.ascii(wafer_id),         # WAFER_ID (A)
            SecsDataTypes.ascii(recipe),           # RECIPE (A)
            SecsDataTypes.u1(slot),                # SLOT (U1: 1-25)
            SecsDataTypes.ascii(ppstate)           # PPSTATE (A)
        ]
    
    @staticmethod
    def build_wafer_measurement_report(
        wafer_id: str,
        recipe: str,
        slot: int,
        die_count: int,
        pass_count: int,
        fail_count: int,
        yield_pct: float,
        measurements: list[float]
    ) -> List[Any]:
        """
        Build wafer measurement report with authentic data types.
        
        Returns:
            List of SECS-II typed variables
        """
        return [
            SecsDataTypes.ascii(wafer_id),
            SecsDataTypes.ascii(recipe),
            SecsDataTypes.u1(slot),
            SecsDataTypes.u4(die_count),
            SecsDataTypes.u4(pass_count),
            SecsDataTypes.u4(fail_count),
            SecsDataTypes.f4(yield_pct),
            # Measurement array
            secs_var.Array(secs_var.F4, [SecsDataTypes.f4(m) for m in measurements])
        ]
    
    @staticmethod
    def build_s2f33_reports(report_definitions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build S2F33 Define Report data with proper types.
        
        Args:
            report_definitions: List of {"rptid": int, "vids": [int, ...]}
        
        Returns:
            S2F33 data structure
        """
        return {
            "DATAID": SecsDataTypes.u4(0),
            "DATA": [
                {
                    "RPTID": SecsDataTypes.u4(rpt["rptid"]),
                    "VID": [SecsDataTypes.u4(vid) for vid in rpt["vids"]]
                }
                for rpt in report_definitions
            ]
        }
    
    @staticmethod
    def build_s2f35_links(event_links: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build S2F35 Link Event Report data with proper types.
        
        Args:
            event_links: List of {"ceid": int, "rptids": [int, ...]}
        
        Returns:
            S2F35 data structure
        """
        return {
            "DATAID": SecsDataTypes.u4(0),
            "DATA": [
                {
                    "CEID": SecsDataTypes.u4(link["ceid"]),
                    "RPTID": [SecsDataTypes.u4(r) for r in link["rptids"]]
                }
                for link in event_links
            ]
        }
    
    @staticmethod
    def build_s2f37_enable(enable: bool, ceids: List[int]) -> Dict[str, Any]:
        """
        Build S2F37 Enable/Disable Event Report data with proper types.
        
        Args:
            enable: True to enable, False to disable
            ceids: List of CEIDs (empty = all events)
        
        Returns:
            S2F37 data structure
        """
        return {
            "CEED": SecsDataTypes.boolean(enable),
            "CEID": [SecsDataTypes.u4(c) for c in ceids]
        }


# Standard SECS-II Variable IDs (VIDs) used in semiconductor manufacturing
class StandardVID:
    """Standard Variable IDs per SEMI E30 GEM."""
    
    # Status Variables (SVIDs)
    CLOCK = 1              # Equipment clock (A16)
    CONTROL_STATE = 2      # Control state (U1)
    EVENTS_ENABLED = 3     # List of enabled events
    ALARMS_ENABLED = 4     # List of enabled alarms
    ALARMS_SET = 5         # List of set alarms
    PREVIOUS_PROCESS_STATE = 6
    PROCESS_STATE = 7
    
    # Data Variables (DVIDs)
    DATAID = 100
    CEID = 101
    EQID = 102
    LOTID = 103
    WAFERID = 104
    PPID = 105             # Process Program ID (Recipe)
    SLOT = 106
    PORTID = 107
    CHAMBER = 108
    
    # Equipment Constants (ECIDs)
    ESTABLISH_COMMS_TIMEOUT = 1
    TIME_FORMAT = 2
    INIT_CONTROL_STATE = 3
    MAX_SPOOL_TRANSMIT = 4


# Standard Collection Event IDs per SEMI E30 GEM
class StandardCEID:
    """Standard Collection Event IDs."""
    
    # Equipment State Events
    EQUIPMENT_OFFLINE = 1
    CONTROL_STATE_LOCAL = 2
    CONTROL_STATE_REMOTE = 3
    
    # Processing Events
    PROCESSING_STARTED = 100
    PROCESSING_COMPLETED = 101
    PROCESSING_STOPPED = 102
    
    # Material Events (matches EAP plan)
    POD_ARRIVED = 1001
    LOT_STARTED = 1002
    WAFER_STARTED = 1003
    WAFER_COMPLETED = 1004
    LOT_COMPLETED = 1005
    POD_REMOVED = 1006
    
    # Alarm Events
    ALARM_SET = 200
    ALARM_CLEARED = 201
