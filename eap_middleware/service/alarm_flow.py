"""Alarm ingress, rate limiting, and publication."""


from __future__ import annotations


import time


from typing import (
    Dict, Optional,
)


from ..journal import (
    KIND_ALARM,
    PENDING,
)


from ..models import (
    MachineConfig,
)


from .constants import (
    logger,
)
from .helpers import (
    optional_int,
)
from .state import ServiceState


class AlarmsMixin(ServiceState):
    """Alarm ingress, rate limiting, and publication."""


    def _on_alarm(self, machine: MachineConfig, alarm: Dict[str, object]) -> None:
        """Gateway callback for S5F1, and the in-process path for alarm CEIDs.

        Journals the alarm unless it is already journaled: an alarm that
        arrived as a collection event was recorded when that event was
        accepted, and recording it twice would double-count the same message.
        """
        self.storage_monitor.require_ingress_capacity()
        machine = self._machines_by_endpoint.get(machine.endpoint_id, machine)
        seq = optional_int(alarm.get("_journal_seq"))
        if seq is None:
            entry, is_new = self.journal.append(
                endpoint_id=machine.endpoint_id,
                kind=KIND_ALARM,
                stream=optional_int(alarm.get("_stream")) or 5,
                function=optional_int(alarm.get("_function")) or 1,
                ceid=optional_int(alarm.get("alid")) or 0,
                payload=dict(alarm),
                system_bytes=optional_int(alarm.get("_system_bytes")),
                generation=self._generations.get(machine.endpoint_id, 0),
            )
            self.journal.mark_csv_skipped(entry.seq)
            if not is_new:
                logger.info(
                    "%s: alarm repeats journal entry %s; acknowledged without "
                    "republishing", machine.endpoint_id, entry.seq,
                )
                return
            seq = entry.seq
            alarm = dict(alarm)
            alarm["_ingress_key"] = entry.ingress_key
            with self._dispatch_lock:
                fresh = self.journal.entry(entry.seq)
                if fresh is None or fresh.dispatch_status != PENDING:
                    return  # the replay pass already delivered this alarm
                self._dispatch_alarm(machine, alarm, seq)
        else:
            self._dispatch_alarm(machine, alarm, seq)


    def _dispatch_alarm(
        self, machine: MachineConfig, alarm: Dict[str, object], seq: Optional[int]
    ) -> None:
        alarm = {
            key: value for key, value in alarm.items() if key != "_journal_seq"
        }
        self._label_alarm_source(machine, alarm)
        # v2 Track A: shed alarms beyond the per-machine limit. Drops are
        # counted and surfaced as a single AlarmStormSummary event per
        # window via _drain_alarm_summary().
        alid = str(alarm.get("alid", alarm.get("ALID", "unknown")))
        is_set = bool(alarm.get("is_set", True))
        signature = (machine.endpoint_id, alid, is_set)
        now = time.monotonic()
        with self._alarm_lock:
            previous = self._recent_alarms.get(signature)
            self._recent_alarms[signature] = now
            # Entries older than the 0.5s dedup window can never match again;
            # prune them so a large ALID space cannot grow the dict without
            # bound over the process lifetime.
            for stale_key in [
                key for key, ts in self._recent_alarms.items() if now - ts > 60.0
            ]:
                del self._recent_alarms[stale_key]
            if previous is not None and now - previous < 0.5:
                self._note_alarm_shed(
                    seq, f"duplicate of ALID {alid} within 0.5s"
                )
                return
        if machine.alarm_rate_limit is not None and not self.alarm_limiter.admit(
            machine.endpoint_id,
            alarm=alarm,
            max_per_window=machine.alarm_rate_limit,
        ):
            self._note_alarm_shed(
                seq,
                f"shed by the {machine.alarm_rate_limit}/window alarm rate "
                "limit; see the AlarmStormSummary event",
            )
            return
        event = self._mapper(machine).alarm_event(machine, alarm)
        self.publisher.queue_event(event)
        self._queue_http_event(event)
        if seq is not None:
            self.journal.mark_dispatched(seq)


    def _label_alarm_source(
        self, machine: MachineConfig, alarm: Dict[str, object]
    ) -> None:
        """Name the module an SPTS alarm came from.

        The Omega manual (section 8.3) defines the alarm id as arithmetic over
        the originating station and station type, so "22400005" is recoverable
        as Process Module 1, an Etch module. Without this the operator gets
        only the number and whatever free text the tool chose to send.

        Only applied to the SPTS profile: the formula is specific to it, and on
        another vendor's numbering it would invent a module that does not
        exist. Never raises - a label is a nicety, an alarm is not.
        """
        if machine.machine_profile != "spts_fxp_omega":
            return
        try:
            from ..spts_module_vids import decode_alarm_id

            identity = decode_alarm_id(alarm.get("alid", alarm.get("ALID")))
        except Exception:  # pragma: no cover - defensive
            logger.debug("SPTS alarm decode failed", exc_info=True)
            return
        if identity is None:
            return
        alarm["alarm_source"] = identity.label
        alarm["alarm_station"] = identity.station_name
        alarm["alarm_station_type"] = identity.station_type_name
        alarm["alarm_offset"] = identity.offset


    def _note_alarm_shed(self, seq: Optional[int], reason: str) -> None:
        """Record an alarm that was deliberately not published.

        Shedding is how an alarm storm is stopped from becoming the outage, but
        the alarm itself is still on disk in the journal with the reason it was
        held back - so the gap can be explained afterwards instead of looking
        like an alarm the tool never sent.
        """
        if seq is not None:
            self.journal.mark_dispatch_dropped(seq, reason)


    def _drain_alarm_summary(self) -> None:
        """Emit one AlarmStormSummary event per machine that had drops this
        window. Called from the reconnect watchdog loop so it piggybacks on
        an already-running cadence."""
        drops = self.alarm_limiter.drain_drop_details()
        for endpoint_id, details in drops.items():
            machine = self._machines_by_endpoint.get(endpoint_id)
            if machine is None:
                continue
            event = self._mapper(machine).alarm_event(
                machine,
                {
                    "alid": -1,
                    "altx": (
                        f"AlarmStormSummary: {details['count']} alarms dropped; "
                        f"ALIDs={sorted(details['alids'])}"
                    ),
                    "is_set": True,
                    "_storm_summary": True,
                    "dropped_count": details["count"],
                    "dropped_by_alid": details["alids"],
                },
            )
            self.publisher.queue_event(event)
            self._queue_http_event(event)


    def _publish_alarm_state_unknown(self, machine: MachineConfig) -> None:
        """Signal that the alarm picture is unknown after every (re)connect.

        Only fires for profiles whose manual documents no AlarmsSet status
        variable, i.e. tools where the currently-active alarm set genuinely
        cannot be queried. On the NexGen MG this matters twice over: AlarmsSet
        is documented "Not Supported" AND spooling is unsupported, so alarms
        raised while the middleware was down are never redelivered. The manual
        also warns that irrecoverable errors and attention flags may never send
        a clearing message, so there is no natural resynchronisation to wait
        for. Emitting this explicitly stops anyone trusting a stale picture.
        """
        profile = self._profile_for(machine)
        if profile.resolve_svid_name("AlarmsSet") is not None:
            return
        event = self._mapper(machine).alarm_event(
            machine,
            {
                "alid": 0,
                "altx": (
                    "Alarm state unknown after connect: this tool cannot report "
                    "its active alarm set and does not spool, so any alarm "
                    "raised while the middleware was disconnected is lost."
                ),
                "is_set": False,
                "_alarm_state_unknown": True,
            },
        )
        self.publisher.queue_event(event)
        self._queue_http_event(event)
