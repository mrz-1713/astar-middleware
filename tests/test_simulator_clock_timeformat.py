"""The DaVinci simulator's Clock width must follow its own TimeFormat ECID.

All three vendor manuals define the constant identically:

  * Omega (SPTS fxP) Table 6, ECID 67 `TimeFormat` - "0 = 12 byte format,
    1 = 16 byte format".
  * NexGen MG §8.4, ECID 5 `TimeFormat` - "0 = 12-byte format, 1 = 16-byte
    format", **default=1**.
  * DaVinci vendor workbook (`SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx`,
    EC sheet) ECID 4010001 `TimeFormat`, format U1, **Default Value = 1**,
    "12-byte, 16-byte, or Extended format".

16-byte is the Year-2000-compliant form all three default to. The simulator
advertised `TimeFormat=1` and then emitted the 12-byte `yymmddhhmmss` form
anyway, so it contradicted its own equipment constant and the 16-byte branch -
the one a default-configured DaVinci actually sends - was never exercised
end to end against the mapper.
"""

from __future__ import annotations

import re

import secsgem.hsms

from eap_middleware.mapper import _parse_clock
from simulator.secsgem_equipment import SecsGemEquipment

DAVINCI_CLOCK_SVID = 1010005
TIMEFORMAT_ECID = 4010001


def _equipment() -> SecsGemEquipment:
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=5000,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    return SecsGemEquipment(settings=settings, tool_id="DAV_CLOCK_TEST")


def test_default_timeformat_is_the_vendor_default_of_16_byte():
    equipment = _equipment()
    assert int(equipment._davinci_ecid_value(TIMEFORMAT_ECID)) == 1

    clock = equipment._davinci_svid_value(DAVINCI_CLOCK_SVID)
    assert re.fullmatch(r"\d{16}", clock), f"expected 16-byte clock, got {clock!r}"
    # YYYYMMDDhhmmsscc - a four-digit year is the whole point of the 16-byte form.
    assert clock.startswith("20"), clock


def test_timeformat_zero_selects_the_12_byte_legacy_form(monkeypatch):
    equipment = _equipment()
    monkeypatch.setattr(
        equipment, "_davinci_ecid_value",
        lambda ecid: 0 if ecid == TIMEFORMAT_ECID else 0,
    )
    clock = equipment._davinci_svid_value(DAVINCI_CLOCK_SVID)
    assert re.fullmatch(r"\d{12}", clock), f"expected 12-byte clock, got {clock!r}"


def test_mapper_resolves_both_widths_to_the_same_wall_time():
    """Whichever width the tool is configured for, the mapper must agree."""
    equipment = _equipment()
    wide = _parse_clock({"Clock": equipment._davinci_svid_value(DAVINCI_CLOCK_SVID)})

    equipment._davinci_ecid_value = (  # type: ignore[method-assign]
        lambda ecid: 0 if ecid == TIMEFORMAT_ECID else 0
    )
    narrow = _parse_clock({"Clock": equipment._davinci_svid_value(DAVINCI_CLOCK_SVID)})

    assert wide.year == narrow.year, (wide, narrow)
    assert abs((wide - narrow).total_seconds()) < 5.0, (wide, narrow)
