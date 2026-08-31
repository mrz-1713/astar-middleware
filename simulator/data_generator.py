"""
SECS/GEM Equipment Simulator Data Generator

Generates realistic semiconductor manufacturing data for testing.
Matches the EAP plan specifications for event types and data format.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ProcessState(Enum):
    """Equipment process states matching production events."""
    IDLE = "IDLE"
    SMIF_POD_PRESENT = "SMIFPodPresent"
    LOADED = "Loaded"
    CLAMPED = "Clamped"
    MOUNTED = "Mounted"
    MATERIAL_RECEIVED = "MaterialReceived"
    LOT_START = "Lot_Start"
    WAFER_START = "Wfr_Start"
    PROCESSING_START = "Proc_Start"
    PROCESSING_END = "Proc_End"
    WAFER_END = "Wfr_End"
    LOT_END = "Lot_End"
    UNMOUNTED = "UnMounted"
    # E30 processing states, used by the S2F41 remote commands.
    EXECUTING = "Executing"
    PAUSED = "Paused"
    UNCLAMPED = "UnClamped"
    SMIF_POD_UNCLAMPED = "SMIFPodUnClamped"
    UNLOADED = "Unloaded"
    SMIF_POD_ABSENT = "SMIFPodAbsent"


# Lot processing event sequence (matches plan)
LOT_EVENT_SEQUENCE = [
    ProcessState.SMIF_POD_PRESENT,
    ProcessState.LOADED,
    ProcessState.CLAMPED,
    ProcessState.MOUNTED,
    ProcessState.MATERIAL_RECEIVED,
    ProcessState.LOT_START,
    # Wafer events happen per wafer
    ProcessState.LOT_END,
    ProcessState.UNMOUNTED,
    ProcessState.UNCLAMPED,
    ProcessState.SMIF_POD_UNCLAMPED,
    ProcessState.UNLOADED,
    ProcessState.SMIF_POD_ABSENT
]

# Wafer processing sequence (per wafer)
WAFER_EVENT_SEQUENCE = [
    ProcessState.WAFER_START,
    ProcessState.PROCESSING_START,
    ProcessState.PROCESSING_END,
    ProcessState.WAFER_END
]


@dataclass
class LotContext:
    """Current lot processing context."""
    lot_id: str
    lot_start_time: str
    wafer_count: int
    current_wafer: int = 0
    load_port: int = 1
    chamber: str = "A"
    recipe: str = "RCP1"
    wafer_ids: List[str] = field(default_factory=list)


@dataclass
class DataGenerator:
    """
    Generates realistic semiconductor manufacturing data.
    
    Follows the EAP event sequence from the plan:
    - SMIF Pod events (load/mount sequence)
    - Lot Start/End events
    - Wafer Start/End events with chamber info
    - Processing Start/End events
    """
    
    tool_id: str
    yield_rate: float = 0.95  # Simulated yield rate
    recipes: List[str] = field(default_factory=lambda: [
        "Met_Etch_ANISO_Rcp1",
        "Met_Etch_ANISO_Rcp2", 
        "Poly_Etch_Rcp1",
        "Oxide_Etch_Rcp1"
    ])
    chambers: List[str] = field(default_factory=lambda: ["A", "B"])
    
    # Current lot context
    _lot_context: Optional[LotContext] = field(default=None, init=False)
    _event_index: int = field(default=0, init=False)
    _wafer_event_index: int = field(default=0, init=False)
    _in_wafer_sequence: bool = field(default=False, init=False)
    
    # Lot counter for unique IDs
    _lot_counter: int = field(default=0, init=False)
    
    def generate_lot_id(self) -> str:
        """Generate a unique lot ID like TEST20251116_33."""
        self._lot_counter += 1
        date_str = datetime.now().strftime("%Y%m%d")
        return f"TEST{date_str}_{self._lot_counter:02d}"
    
    def generate_wafer_ids(self, lot_id: str, count: int) -> List[str]:
        """Generate wafer IDs for a lot."""
        return [f"{lot_id},{i+1}" for i in range(count)]
    
    def start_new_lot(self) -> None:
        """Start processing a new lot."""
        lot_id = self.generate_lot_id()
        wafer_count = random.randint(1, 25)  # 1-25 wafers per lot
        
        self._lot_context = LotContext(
            lot_id=lot_id,
            lot_start_time=datetime.now().strftime("%Y%m%d%H%M%S"),
            wafer_count=wafer_count,
            load_port=random.choice([1, 2]),
            chamber=random.choice(self.chambers),
            recipe=random.choice(self.recipes),
            wafer_ids=self.generate_wafer_ids(lot_id, wafer_count)
        )
        self._event_index = 0
        self._wafer_event_index = 0
        self._in_wafer_sequence = False
    
    def get_next_event(self) -> Tuple[ProcessState, Dict[str, Any]]:
        """
        Get the next event in the lot processing sequence.
        
        Returns:
            Tuple of (ProcessState, event_data dict)
        """
        # Start new lot if needed
        if self._lot_context is None:
            self.start_new_lot()
        
        ctx = self._lot_context
        assert ctx is not None
        
        # Handle wafer sequence
        if self._in_wafer_sequence:
            if self._wafer_event_index < len(WAFER_EVENT_SEQUENCE):
                state = WAFER_EVENT_SEQUENCE[self._wafer_event_index]
                self._wafer_event_index += 1
                return state, self._make_event_data(state)
            else:
                # Move to next wafer or continue lot sequence
                ctx.current_wafer += 1
                self._wafer_event_index = 0
                if ctx.current_wafer < ctx.wafer_count:
                    # Process next wafer
                    state = WAFER_EVENT_SEQUENCE[0]
                    self._wafer_event_index = 1
                    return state, self._make_event_data(state)
                else:
                    # All wafers done, continue lot sequence
                    self._in_wafer_sequence = False
                    self._event_index = LOT_EVENT_SEQUENCE.index(ProcessState.LOT_END)
        
        # Handle lot sequence
        if self._event_index < len(LOT_EVENT_SEQUENCE):
            state = LOT_EVENT_SEQUENCE[self._event_index]
            self._event_index += 1
            
            # Check if we should start wafer sequence
            if state == ProcessState.LOT_START:
                self._in_wafer_sequence = True
                self._wafer_event_index = 0
                ctx.current_wafer = 0
            
            return state, self._make_event_data(state)
        
        # Lot complete, start new one
        self._lot_context = None
        return self.get_next_event()
    
    def _make_event_data(self, state: ProcessState) -> Dict[str, Any]:
        """Create event data dictionary for current state."""
        ctx = self._lot_context
        assert ctx is not None
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Current wafer ID
        wafer_id = ""
        if ctx.current_wafer < len(ctx.wafer_ids):
            wafer_id = ctx.wafer_ids[ctx.current_wafer]
        
        return {
            "DATETIME": timestamp,
            "TOOL_EVENT": state.value,
            "EAP_TOOLNAME": self.tool_id,
            "LOAD_PORT": ctx.load_port,
            "CHAMBER": ctx.chamber if state in [
                ProcessState.WAFER_START, ProcessState.WAFER_END,
                ProcessState.PROCESSING_START, ProcessState.PROCESSING_END
            ] else "",
            "LOT_ID": ctx.lot_id,
            "LOT_START_TIME": ctx.lot_start_time,
            "WAFER_QTY": ctx.wafer_count,
            "WAFER_ID": wafer_id,
            "RECIPE": ctx.recipe,
            "SECSGEM_RAW_EVENT": state.value,
            # Additional fields for S6F11
            "CLOCK": datetime.now().strftime("%Y%m%d%H%M%S"),
            "EQID": self.tool_id,
            "PPSTATE": state.value,
            "SLOT": ctx.current_wafer + 1,
            "DIE_X": random.randint(0, 10),
            "DIE_Y": random.randint(0, 10),
            "TEST_VALUE": round(random.gauss(100.0, 5.0), 4),
            "BIN_CODE": 1 if random.random() > 0.05 else random.randint(2, 4),
            "PASS_FAIL": "PASS" if random.random() > 0.05 else "FAIL"
        }
    
    def generate_event_data(self) -> Dict[str, Any]:
        """
        Generate the next event data in the sequence.
        
        This is called by the equipment simulator for each S6F11 event.
        """
        _state, data = self.get_next_event()
        return data
