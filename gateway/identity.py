"""Vendor-tolerant S1F2 identity codec.

DaVinci documents a 24-character SOFTREV even though the legacy SEMI MDLN
data item is limited to 20 characters. The wire item is still ASCII; this
codec removes only the artificial local length limit.
"""

from secsgem.secs.data_items import DataItemBase
from secsgem.secs.functions.base import SecsStreamFunction
from secsgem.secs.variables import String


class ExtendedIdentity(DataItemBase):
    """ASCII identity item without the legacy 20-character MDLN limit."""

    __type__ = String


class SecsS01F02Extended(SecsStreamFunction):
    """S1F2 that tolerates a vendor's over-long SOFTREV."""

    _stream = 1
    _function = 2
    _data_format = [ExtendedIdentity]
    _to_host = True
    _to_equipment = False
    _has_reply = False
    _is_reply_required = False
    _is_multi_block = False
