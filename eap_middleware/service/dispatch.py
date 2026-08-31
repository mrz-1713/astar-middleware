"""SECS event ingress, journal replay, and fan-out to sinks."""


from __future__ import annotations


import time


from typing import (
    Dict, Optional, Sequence,
)


from ..journal import (
    KIND_ALARM,
    KIND_EVENT,
    PENDING,
    JournalEntry,
)


from ..models import (
    CanonicalEvent,
    MachineConfig,
)

from ..outbox import OutboxFullError


from .constants import (
    MAX_DISPATCH_ATTEMPTS,
    CSV_FAIL_LOG_INTERVAL_SEC,
    logger,
)
from .helpers import (
    optional_int,
)
from .state import ServiceState


class DispatchMixin(ServiceState):
    """SECS event ingress, journal replay, and fan-out to sinks."""


    def _on_secs_event(
        self, machine: MachineConfig, ceid: int, data: Dict[str, object]
    ) -> None:
        """Gateway callback for a collection event, called before the ACK.

        The journal write comes first and is the only step allowed to fail
        loudly: if it raises, gateway.host answers ACKC6=1 and the tool keeps
        the event. Everything after it is a derived view the replay can rebuild,
        so a mapper or publisher fault is recorded against the entry instead of
        being turned into a refusal - otherwise one bad event would have the
        tool retransmitting it forever.
        """
        machine = self._machines_by_endpoint.get(machine.endpoint_id, machine)
        self.storage_monitor.require_ingress_capacity()
        entry, is_new = self.journal.append(
            endpoint_id=machine.endpoint_id,
            kind=KIND_EVENT,
            stream=optional_int(data.get("_stream")) or 6,
            function=optional_int(data.get("_function")) or 11,
            ceid=int(ceid or 0),
            payload=dict(data),
            system_bytes=optional_int(data.get("_system_bytes")),
            generation=self._generations.get(machine.endpoint_id, 0),
        )
        if not is_new:
            # Already accepted under this transaction id. Acknowledge it again
            # so the tool stops retrying, but publish nothing: the original
            # delivery is what the sinks already have.
            logger.info(
                "%s: CEID %s repeats journal entry %s; acknowledged without "
                "republishing",
                machine.endpoint_id, ceid, entry.seq,
            )
            return
        with self._dispatch_lock:
            # The supervisor's replay pass may already have picked this entry
            # up between append() and here (it runs every few seconds). Re-read
            # the journal under the lock and derive what each sink still owes,
            # exactly as _replay_journal does - the two paths race in both
            # directions and the guard has to be symmetric.
            #
            # Gating on dispatch_status alone was not enough. The sinks fail
            # independently: a replay pass that got the CSV row written but hit
            # a full outbox leaves dispatch PENDING (mark_dispatch_failed only
            # counts the attempt) while the row already sits in the open lot
            # buffer. Re-entering here with csv=True appended that same row a
            # second time, silently duplicating a wafer in the lot file that is
            # the middleware's contractual output.
            fresh = self.journal.entry(entry.seq)
            if fresh is None:
                return
            publish = fresh.dispatch_status == PENDING
            csv = (
                fresh.csv_status == PENDING
                and not self.csv_writer.holds(entry.seq)
            )
            if not (publish or csv):
                return
            self._dispatch_entry(
                fresh, publish=publish, csv=csv, machine=machine
            )


    def _dispatch_entry(
        self,
        entry: JournalEntry,
        *,
        publish: bool,
        csv: bool,
        machine: Optional[MachineConfig] = None,
    ) -> bool:
        """Feed one journaled transaction to the sinks that still need it.

        Returns False if some requested sink is still unfinished, which the
        replay uses to hold back that machine's later entries and keep its
        stream in order.
        """
        if machine is None:
            machine = self._machines_by_endpoint.get(entry.endpoint_id)
        if machine is None:
            reason = "machine is no longer configured"
            if publish:
                self.journal.mark_dispatch_dropped(entry.seq, reason)
            if csv:
                self.journal.mark_csv_dropped([entry.seq], reason)
            return True
        payload = dict(entry.payload)
        payload["_ingress_key"] = entry.ingress_key
        try:
            if entry.kind == KIND_ALARM:
                if csv:
                    # Alarms are published, never written to a lot file.
                    self.journal.mark_csv_skipped(entry.seq)
                if publish:
                    self._dispatch_alarm(machine, payload, entry.seq)
                return True
            return self._dispatch_event(
                machine, entry.ceid, payload, entry.seq,
                publish=publish, csv=csv,
            )
        except Exception as exc:
            return self._record_dispatch_failure(entry, exc, publish=publish)


    def _record_dispatch_failure(
        self, entry: JournalEntry, exc: Exception, *, publish: bool
    ) -> bool:
        logger.exception(
            "Dispatch failed for %s journal entry %s (CEID %s)",
            entry.endpoint_id, entry.seq, entry.ceid,
        )
        if not publish:
            return False
        if isinstance(exc, OutboxFullError):
            # Backpressure, not a defect: the sink is full but will drain
            # (or an operator will free it). Parking here would turn a
            # temporary outage into permanent telemetry loss after ~10
            # replay passes, so the entry stays pending and the replay
            # keeps retrying it while the machine's stream is held back.
            logger.error(
                "%s: outbox full, holding journal entry %s for later "
                "delivery (CSV rows are already appended)",
                entry.endpoint_id, entry.seq,
            )
            return False
        self.journal.mark_dispatch_failed(entry.seq, str(exc))
        if self.journal.dispatch_attempts(entry.seq) >= MAX_DISPATCH_ATTEMPTS:
            logger.error(
                "Parking %s journal entry %s after %d failed dispatches: %s. "
                "The payload is still readable in the journal.",
                entry.endpoint_id, entry.seq, MAX_DISPATCH_ATTEMPTS, exc,
            )
            self.journal.mark_dispatch_dropped(
                entry.seq, f"parked after {MAX_DISPATCH_ATTEMPTS} failures: {exc}"
            )
            return True
        return False


    def _replay_journal(self, limit: int = 500) -> int:
        """Re-run the sinks for everything the journal has not seen finished.

        Called at startup, where it recovers whatever had been acknowledged but
        not yet written when the process died, and periodically afterwards so a
        briefly broken sink catches up without waiting for a restart. Entries
        go in arrival order and a failure holds back only its own machine, so
        one stuck tool cannot reorder another tool's stream.
        """
        entries: Dict[int, JournalEntry] = {}
        publish_seqs: set[int] = set()
        csv_seqs: set[int] = set()
        for entry in self.journal.pending_dispatch(limit):
            entries[entry.seq] = entry
            publish_seqs.add(entry.seq)
        for entry in self.journal.pending_csv(limit):
            # Rows sitting in an open lot buffer are not lost, just not written
            # yet. Replaying those would duplicate them inside the buffer.
            if self.csv_writer.holds(entry.seq):
                continue
            entries.setdefault(entry.seq, entry)
            csv_seqs.add(entry.seq)

        blocked: set[str] = set()
        replayed = 0
        for seq in sorted(entries):
            entry = entries[seq]
            if entry.endpoint_id in blocked:
                continue
            with self._dispatch_lock:
                # The live callback path may have applied this entry's sinks
                # while this pass was being assembled (or in an earlier pass).
                # Re-derive what is still owed from fresh journal state under
                # the lock, so replay never applies a sink twice and never
                # misses one either.
                fresh = self.journal.entry(seq)
                if fresh is None:
                    continue
                publish = seq in publish_seqs and fresh.dispatch_status == PENDING
                csv = seq in csv_seqs and fresh.csv_status == PENDING
                if not (publish or csv):
                    continue
                if csv and self.csv_writer.holds(seq):
                    # Rows sitting in an open lot buffer are not lost, just
                    # not written yet; replaying them would duplicate them.
                    continue
                if not self._dispatch_entry(fresh, publish=publish, csv=csv):
                    blocked.add(entry.endpoint_id)
                    continue
            replayed += 1
        if replayed:
            logger.info("Replayed %d journal entries", replayed)
        return replayed


    def _dispatch_event(
        self,
        machine: MachineConfig,
        ceid: int,
        data: Dict[str, object],
        seq: Optional[int] = None,
        *,
        publish: bool = True,
        csv: bool = True,
    ) -> bool:
        # If events are arriving via the E40 Process Job path (S16F9/F7) the tool
        # is in E40 report style - data is coarser than E30. Flag it once per
        # connection so ops can switch the tool to E30 for full granularity.
        if data.get("_e40"):
            state = self._event_liveness.setdefault(machine.endpoint_id, {})
            if not state.get("e40_flagged"):
                state["e40_flagged"] = True
                self._publish_health(
                    machine, "e40_mode",
                    "Receiving events via the E40 Process Job path (S16F9/F7). "
                    "The middleware is ingesting them, but E40 reports are coarser "
                    "than E30 (lot/job lifecycle only, no per-CEID carrier/"
                    "substrate/alarm detail). Switch the tool to E30/S6F11 "
                    "reporting for full granularity.",
                )
        profile = self._profile_for(machine)
        events = self._mapper(machine).from_secs_events(machine, ceid, data)
        if events:
            last = events[-1]
            self._runtime_states.setdefault(machine.endpoint_id, {}).update(
                {
                    "last_event": {
                        "event_type": last.event_type,
                        "ceid": last.ceid,
                        "timestamp": last.timestamp.isoformat(),
                    },
                    "current_lot": {
                        "lot_id": last.lot_id,
                        "wafer_id": last.wafer_id,
                        "load_port": last.load_port,
                    },
                }
            )
        if not events:
            # Nothing to deliver, but the entry must still reach a terminal
            # state or the replay would pick it up again on every pass.
            if publish and seq is not None:
                self.journal.mark_dispatched(seq)
            if csv and seq is not None:
                self.journal.mark_csv_skipped(seq)
            return True
        if events[0].event_type == "alarm":
            payload = events[0].raw_payload
            if csv and seq is not None:
                self.journal.mark_csv_skipped(seq)
            if not publish:
                return True
            self._on_alarm(
                machine,
                {
                    "alid": payload.get("AlarmID", payload.get("ALID", ceid)),
                    "alcd": payload.get("AlarmCode", payload.get("ALCD", 0)),
                    "altx": payload.get("AlarmText", payload.get("ALTX", "")),
                    # Set vs cleared comes from the profile that owns this
                    # CEID. Comparing against the DaVinci's own 3020002 marked
                    # every MG alarm-cleared (CEID 9) as still set.
                    "is_set": (
                        profile.resolve_event(ceid=ceid).csv_tool_event
                        != "AlarmCleared"
                    ),
                    "timestamp": payload.get("received_at", ""),
                    "source": "S6F11",
                    # Already journaled as the collection event that carried it,
                    # so _on_alarm must account against that entry rather than
                    # recording the same message a second time.
                    "_journal_seq": seq,
                    "_ingress_key": data.get("_ingress_key"),
                },
            )
            return True
        publish_error: Optional[BaseException] = None
        if publish:
            try:
                for event in events:
                    self.publisher.queue_event(event)
                    self._queue_http_event(event)
                    self.legacy_api.queue_event(event, profile)
            except BaseException as exc:
                # The CSV sink must still run: a full outbox (or any other
                # publish failure) is a backpressure state, not a reason to
                # skip the lot file. The exception is re-raised afterwards so
                # the journal keeps the entry replayable for the publish sink.
                publish_error = exc
            else:
                if seq is not None:
                    # Every publisher owns a durable outbox keyed on the
                    # event's idempotency key, so "queued" is the honest
                    # completion point and a replay re-queues without
                    # duplicating.
                    self.journal.mark_dispatched(seq)
        if csv:
            try:
                for event in events:
                    # The writer resolves the journal entry itself, once the lot
                    # file holding the row is actually on disk.
                    self.csv_writer.append(machine, profile, event, seq=seq)
            except Exception as exc:
                # The two sinks have separate status columns precisely so one
                # can fail without the other. Letting this escape made
                # _record_dispatch_failure overwrite a dispatch that had
                # already succeeded and been marked done - and, after ten
                # replays, park the entry claiming a publish that did happen
                # never did. Record it against the sink that actually failed.
                now = time.monotonic()
                last = self._csv_fail_last_log.get(machine.endpoint_id, 0.0)
                if now - last >= CSV_FAIL_LOG_INTERVAL_SEC:
                    suppressed = self._csv_fail_suppressed.pop(
                        machine.endpoint_id, 0
                    )
                    logger.exception(
                        "%s: CSV write failed for journal entry %s (CEID %s); "
                        "%d further failure(s) since the last log were "
                        "suppressed; the publish sink is unaffected",
                        machine.endpoint_id, seq, ceid, suppressed,
                    )
                    self._csv_fail_last_log[machine.endpoint_id] = now
                else:
                    self._csv_fail_suppressed[machine.endpoint_id] = (
                        self._csv_fail_suppressed.get(machine.endpoint_id, 0)
                        + 1
                    )
                if seq is not None:
                    self.journal.mark_csv_failed(seq, str(exc))
                if publish_error is None:
                    return False
            else:
                # A successful write clears the throttle so the next episode
                # starts with a full traceback rather than a stale count.
                self._csv_fail_last_log.pop(machine.endpoint_id, None)
                self._csv_fail_suppressed.pop(machine.endpoint_id, None)
        if publish_error is not None:
            raise publish_error
        self._log_event_outcome(machine, ceid, events, publish=publish, csv=csv)
        return True


    def _log_event_outcome(
        self,
        machine: MachineConfig,
        ceid: int,
        events: "Sequence[CanonicalEvent]",
        *,
        publish: bool,
        csv: bool,
    ) -> None:
        """Record what became of one collection event, at INFO.

        The middleware used to log nothing per event - only alarms - so an
        operator watching the panel saw a machine connect, saw alarms, and had
        no way to tell a tool delivering hundreds of events from one delivering
        none. Both look identical: a green connection and a quiet log. That is
        precisely the silent-failure mode the liveness watchdog exists to catch,
        and it should not need a watchdog to be visible.

        One line per canonical event, naming the CEID, what it mapped to, the
        lot/wafer/port it was attributed to, and which sinks accepted it.
        """
        sinks = ",".join(
            name for name, on in (("publish", publish), ("csv", csv)) if on
        ) or "none"
        for event in events:
            context = " ".join(
                f"{key}={value}"
                for key, value in (
                    ("lot", event.lot_id), ("wafer", event.wafer_id),
                    ("port", event.load_port), ("chamber", event.chamber),
                    ("recipe", event.recipe),
                )
                if value
            )
            logger.info(
                "[%s] CEID %s -> %s%s [%s]",
                machine.endpoint_id, ceid, event.event_type,
                f" {context}" if context else "", sinks,
            )
