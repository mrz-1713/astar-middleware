"""Extract the SPTS fxP Appendix E module-variable table from the manual.

`docs/vendor/Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf` Appendix E does
not list absolute SVIDs. It gives a formula:

    VID = (station number * 10000) + (station type offset * 100)
          + (variable offset) + 10000

so the same variable name lands on a different VID depending on which physical
station the module sits in. This script captures the two halves the formula
needs - the station-type offsets and each type's variable offsets - as data, so
`eap_middleware.spts_module_vids` can resolve VIDs for a given tool layout
without 880 hand-copied table rows.

    python -m scripts.gen_spts_module_variables

Reads the reviewed text extract under `docs/vendor/`; poppler's `pdftotext`
is needed only to regenerate that extract from a new PDF revision.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict

from scripts.vendor_text import vendor_lines

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "docs" / "vendor" / "Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf"
EXTRACT = ROOT / "docs" / "vendor" / "omega_secs_extracted.txt"
OUTPUT = ROOT / "output" / "spts_fxp_omega" / "ModuleVariables.json"

# Manual Appendix E, "Station Number" list.
STATION_NUMBERS: Dict[str, int] = {
    "Transport": 0,
    "VCE A": 1,
    "Process Module 1": 2,
    "Process Module 2": 3,
    "Process Module 3": 4,
    "Process Module 4": 5,
    "Process Module 5": 6,
    "Process Module 6": 7,
    "VCE B": 8,
    "Cool Station": 10,
}

# Manual Appendix E, "Station Type" list, mapped to the `Statx_<family>_` name
# prefix each type uses in the variable table. The manual lists the type offsets
# and the variable names separately and never states the correspondence, so it
# is asserted here from the name prefixes and must be reviewed against a real
# tool before the resulting VIDs are trusted on hardware.
TYPE_OFFSETS: Dict[str, int] = {
    "ForceFill": 3,
    "SDep": 4,
    "HSE": 7,
    "HeatNT": 9,
    "Etch": 24,
    "Deposition": 25,
    "Softetch": 26,
    "C3M": 46,
    "PrimaxxMonarch25": 53,
    "ProCve": 58,
}
# Families whose type offset the manual does not state. Kept out of the VID
# maths rather than guessed: a wrong offset silently mislabels every variable
# in the family.
UNMAPPED_FAMILIES = ("DeltaAPM", "VCE", "PreHeat")


def extract_text() -> list[str]:
    return vendor_lines(PDF, EXTRACT)


def build() -> Dict[str, object]:
    lines = extract_text()
    # Both appendix titles appear twice: once in the table of contents and once
    # as the real heading. Take the LAST occurrence of each - anchoring on the
    # first lands in the TOC and yields a 17-line window that parses to nothing.
    start = max(
        i for i, line in enumerate(lines)
        if "Appendix E - Module Equipment Specific Status" in line
    )
    # Appendix F (Status Variable Value Definitions) repeats many of the same
    # names while documenting their enumerated values, so an unbounded scan
    # double-counts every one of them and picks up page numbers as offsets.
    end = max(
        (i for i, line in enumerate(lines) if "Appendix F" in line),
        default=len(lines),
    )
    if end <= start:
        end = len(lines)
    families: Dict[str, Dict[str, str]] = defaultdict(dict)
    for line in lines[start:end]:
        match = re.match(r"^\s*(\d{1,4})\s\s+(Statx_([A-Za-z0-9]+)_\S*)", line)
        if match:
            offset, name, family = match.groups()
            families[family].setdefault(offset, name)

    if not families:
        sys.exit("Appendix E parsed to nothing - check the manual revision.")

    # Appendix F section 24.1 lists the values the `StationType` status
    # variable actually reports. They are a DIFFERENT numbering scheme from
    # Appendix E's type offsets - Etch is offset 24 for VID arithmetic but
    # reports as 180-185 at runtime - so both are captured and the mapping
    # between them is asserted explicitly in eap_middleware.spts_module_vids
    # rather than inferred from the names.
    f_start = max(i for i, line in enumerate(lines) if "Appendix F" in line)
    f_end = next(
        (i for i in range(f_start + 2, len(lines))
         if re.match(r"^\s*24\.2", lines[i])),
        len(lines),
    )
    station_type_values: Dict[str, str] = {}
    for line in lines[f_start:f_end]:
        match = re.match(r"^\s*(\d{1,3})\s*=\s*(.+)$", line.strip())
        if match:
            station_type_values[match.group(1)] = match.group(2).strip()

    collisions = _collisions(families)
    return {
        "station_type_values": station_type_values,
        "_station_type_values_note": (
            "Appendix F section 24.1: what the StationType status variable "
            "reports at runtime. NOT the same numbers as type_offsets, which "
            "are Appendix E VID-arithmetic offsets. Etch is offset 24 but "
            "reports as 180-185 (six variants sharing one offset); ForceFill "
            "has an offset (3) but no documented runtime value; Delta APM has "
            "a runtime value (169) but no documented offset."
        ),
        "description": (
            "SPTS fxP Appendix E module status variables. Variable offsets per "
            "station-type family; absolute VIDs are computed per tool layout by "
            "eap_middleware.spts_module_vids."
        ),
        "_source": PDF.name + " Appendix E",
        "_generated_by": "scripts/gen_spts_module_variables.py",
        "_formula": (
            "VID = (station * 10000) + (type_offset * 100) + variable_offset "
            "+ 10000"
        ),
        "_formula_is_ambiguous": (
            "The manual spaces station types 100 apart but four families "
            "exceed 99 variables (Etch 201, DeltaAPM 134, Deposition 108, "
            "Softetch 103), so their high offsets overrun the next type's "
            "range. A VID is therefore only decodable when the station's "
            "module type is known - it can never be resolved from a flat "
            "table. See docs/VENDOR_DOC_AUDIT.md."
        ),
        "_known_collisions": collisions,
        "station_numbers": STATION_NUMBERS,
        "type_offsets": TYPE_OFFSETS,
        "unmapped_families": {
            family: sorted(families.get(family, {}), key=int)[:1] and
            len(families.get(family, {}))
            for family in UNMAPPED_FAMILIES
        },
        "families": {
            family: dict(sorted(offsets.items(), key=lambda kv: int(kv[0])))
            for family, offsets in sorted(families.items())
        },
    }


def _collisions(families: Dict[str, Dict[str, str]]) -> int:
    """How many VIDs two mapped families would both claim, at one station."""
    seen: Dict[int, str] = {}
    clashes = 0
    for family, offsets in families.items():
        type_offset = TYPE_OFFSETS.get(family)
        if type_offset is None:
            continue
        for offset in offsets:
            vid = 20000 + type_offset * 100 + int(offset) + 10000
            if vid in seen and seen[vid] != family:
                clashes += 1
            seen[vid] = family
    return clashes


def main() -> None:
    data = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in data["families"].values())  # type: ignore[union-attr]
    print(
        f"Wrote {OUTPUT}: {len(data['families'])} families, "  # type: ignore[arg-type]
        f"{total} variable offsets, "
        f"{data['_known_collisions']} formula collisions at one station"
    )


if __name__ == "__main__":
    main()
