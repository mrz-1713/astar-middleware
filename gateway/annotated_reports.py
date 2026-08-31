"""S6F13 (Annotated Event Report Send) support.

Why this exists
---------------
The SPTS fxP Omega publishes an equipment constant that chooses which SECS
message carries a collection event (Omega manual Table 6, ECID 4022
``EventReportMsg``):

    67075 -> S6F3   Discrete Variable Data Send
    67083 -> S6F11  Event Report Send        <- what this middleware expects
    67085 -> S6F13  Annotated Event Report Send

The NexGen MG documents the same pair (manual §6.5: "S6F13 ... is the same as
S6F11 with the exception that VID's are sent with data"), and its S2F33 has a
Boolean that selects annotated reports for a whole report definition.

secsgem 0.3.0 ships no S6F13/S6F14 classes, and the host registered no
handler, so a tool set to 67085 produced the worst failure mode this system
has: the link comes up, S2F33/35/37 are all acknowledged, and then nothing
arrives that the middleware can decode - a green connection with a permanently
empty feed and no diagnostic pointing at the cause.

S6F13 carries strictly *more* information than S6F11 (each value is preceded
by its VID rather than being positional), so decoding it is not a degraded
path: the VID/V pairs are flattened back to the positional ``values`` list the
mapper already consumes, and the pairs are kept alongside so a profile can key
by VID instead of position.
"""

from __future__ import annotations

from typing import Any, cast

from secsgem.secs.functions.base import SecsStreamFunction


class SecsS06F13(SecsStreamFunction):
    """Annotated Event Report Send.

    L,3
      1. <DATAID>
      2. <CEID>
      3. L,a
         1. L,2
            1. <RPTID>
            2. L,b
               1. L,2
                  1. <VID>
                  2. <V>

    Equipment -> Host, reply required (S6F14).
    """

    _stream = 6
    _function = 13

    # Declared in secsgem 0.3.0's SML form, the same way the library declares
    # S6F11, so the report list is named RPT and callers build an S6F13 with
    # the identical dict shape they already use for S6F11.
    _data_format = cast(Any, """
    < L
      < DATAID >
      < CEID >
      < L RPT
        < L
          < RPTID >
          < L VLIST
            < L
              < VID >
              < V >
            >
          >
        >
      >
    >
    """)
    _to_host = True
    _to_equipment = False
    _has_reply = True
    _is_reply_required = True
    _is_multi_block = True


class SecsS06F14(SecsStreamFunction):
    """Annotated Event Report Acknowledge: <ACKC6>. Host -> Equipment."""

    _stream = 6
    _function = 14

    _data_format = cast(Any, """
    < ACKC6 >
    """)
    _to_host = False
    _to_equipment = True
    _has_reply = False
    _is_reply_required = False
    _is_multi_block = False
