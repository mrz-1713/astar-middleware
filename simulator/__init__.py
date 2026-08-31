"""
SECS/GEM Equipment Simulator Package

This package provides a simulated SECS/GEM equipment for testing purposes.
It implements the SEMI E30 (GEM) standard over HSMS (SEMI E37).
Uses authentic SECS-II data types per SEMI E5.
"""

from .equipment import EquipmentSimulator
from .data_generator import DataGenerator
from .secs_data_types import (
    SecsDataTypes,
    ProductionDataBuilder,
    StandardCEID,
    StandardVID,
)

__version__ = "1.0.0"

__all__ = [
    "EquipmentSimulator",
    "DataGenerator",
    "SecsDataTypes",
    "ProductionDataBuilder",
    "StandardCEID",
    "StandardVID",
]
