# SECS/GEM EAP Middleware — Per-Machine Data-Loss Audit

Audited at /Volumes/Backup/astar-middleware-main (Python). Scope: ingestion durability (journal),
CSV durability (per-lot writer), outbox durability (SQLite), dedup identity, per-machine separation,
and the two Linkstuffs transports (MQTT + HTTPS). Tests examined: test_durable_ingress,
test_outbox_maintenance, test_reliability_remediation, test_davinci_multi_machine_audit,
test_edge_case_fixes, test_csv_pre_lot_ttl, test_event_liveness, test_linkstuffs_http.

## DATA FLOW DIAGRAM (ack/persist points and crash windows)

```
TOOL ──S6F11 / S5F1 (or S16F9/F7)──▶ gateway/host.py handler (receive thread)
  │ 1. _decode_packet_data + _parse_event_data          decode fail ──▶ ACK=1 / SxF0 abort [host.py:212-233, 695-736]
  │ 2. journal.append(ingress, WAL + synchronous=FULL)  store fail ──▶ raises ──▶ ACK=1  [service.py:1247, journal.py:147]
  │    ▲ PERSIST POINT 1 — durable BEFORE any ack. crash between commit & ack = tool resends (dedup).
  │ 3. _dispatch_entry (same callback, before ack)      [service.py:1267]
  │    ├─ MQTT outbox enqueue  (SQLite FULL, partition=endpoint)   ◀ PERSIST POINT 2
  │    ├─ per-machine HTTP outbox enqueue (SQLite FULL)            ◀ PERSIST POINT 3
  │    ├─ legacy API outbox enqueue                                 ◀ PERSIST POINT 4
  │    └─ PerLotCsvWriter.append ──▶ IN-MEMORY lot buffer / pre-lot pending   ◀ [C] no disk yet
  │       (enqueue failure = recorded in journal, replay retries — no exception to the tool)
  │ 4. handler returns S6F12(0) / S5F2(0) ──▶ ACK SENT   [host.py:227, 730]
  ▼
ingress_journal.sqlite3  (append-only; endpoint_id column; UNIQUE ingress_key)
  │  supervisor + startup replay every 5s [service.py:284, 928-935] rebuilds missing sinks, idempotently
  ▼
CSV: lot buffer ─(lot_end / close / stop)─▶ _write_atomic (tmp+fsync+os.replace) ─▶ local .csv  [csv_store.py:417-427]
  │    └─ mirror to network share ─fail─▶ journal.csv_mirror table ─retry in supervisor─▶ network copy  [csv_store.py:320-340]
  ▼
outboxes: worker threads drain per-machine FIFO heads ─▶ MQTT qos1 / HTTPS POST (retry, Retry-After)
          mark_sent / mark_failed(backoff ≤300s) / mark_dead (NEVER CALLED — see FINDING 1)

CRASH WINDOWS:
  W1  journal commit ─ ACK            safe: no ack yet / tool resends → dedup   [journal.py:58-101, service.py:1257-1266]
  W2  ACK ─ outbox commit             safe: journal pending → replay re-enqueues (dedup key) [outbox.py:156]
  W3  outbox commit ─ send            safe: durable SQLite, drained on restart
  W4  send ─ mark_sent                at-least-once: response lost ⇒ row retried ⇒ possible downstream dup
  W5  ACK ─ CSV file write            safe ONLY via journal replay; journal loss = permanent loss (residual risk)
  W6  local CSV write ─ mirror        local safe; network copy can be skipped by crash before enqueue_mirror (FINDING 4)
  W7  pre-lot rows, no lot_start      TTL 1h / cap 200 — cap records drop, TTL leaks journal refs (FINDING 3)
```

## Answers to the 9 questions

**1. ACK TIMING — persist-before-ack, in full.** gateway/host.py:181-233: decode → parse →
`self._on_event(self.tool_id, ceid, event_data)` (host.py:224-226) → `return self.stream_function(6, 12)(0)`
(host.py:227). Any exception returns `self.stream_function(6, 12)(1)` (host.py:233). secsgem 0.3.0
(deploy/wheels/secsgem-0.3.0) sends the reply only after the handler returns
(`result = callback(self, message); if result is not None: self.send_response(result, ...)` in
secsgem/secs/handler.py:132-136), so the ACK is strictly after the callback. The callback path
(service.py:1234-1267) is: (a) decode — host; (b) journal.append (service.py:1247, the only step allowed to
fail loudly → ACK=1); (c) _dispatch_entry → _dispatch_event (service.py:1372-1467): mapper at :1398,
outbox enqueues at :1454-1456, `self.journal.mark_dispatched(seq)` at :1461, CSV buffer append at :1466.
So the ack is sent only after decode + mapping + outbox commit + in-memory CSV append. The CSV row is
buffered in memory at ack time — NOT fsynced (W5 is covered by journal replay, not by the CSV write).
S5F1 identical: host.py:674-736, ack `stream_function(5, 2)(0)` at :730, journal.append in _on_alarm
(service.py:1479), dispatch → outbox at :1534-1535.

**2. DEDUP.** Event identity = sha256 of [endpoint_id, stream, function, ceid, system_bytes,
body-without-(received_at,timestamp)] (journal.py:58-101, compute_ingress_key). A tool retransmit of the
same S6F11 (same system bytes) hits `INSERT OR IGNORE` + UNIQUE key (journal.py:237), returns is_new=False,
is acknowledged without republishing (service.py:1257-1266). Outbox dedup: `key=f"telemetry:{event.event_key()}"`
(linkstuffs.py:98-99), `http-telemetry:{event.event_key()}` (linkstuffs_http.py:150-157), where event_key =
sha256({endpoint_id, ingress_key, ingress_index, event_type}) (models.py:296-329), and enqueue is
`INSERT OR IGNORE` on a UNIQUE column (outbox.py:156) — so replay re-queues without duplicating
(test_durable_ingress.py:147-154). Two machines with identical CEID+timestamp are distinguishable:
endpoint_id is inside both the journal key and every outbox key (test_davinci_multi_machine_audit.py:470-491,
test_durable_ingress.py:84-94). Only exception: `connect:` rows keyed with time_ns (linkstuffs.py:67) are
intentionally never deduped.

**3. CRASH WINDOWS.** (i) CSV row buffered but not fsynced: crash loses the buffer; the journal entry stays
csv_status='pending' and startup/supervisor replay rebuilds the lot file (service.py:1331-1370,
test_durable_ingress.py:97-132). (ii) Outbox row committed, not sent: durable SQLite; worker drains on
restart. (iii) Sent but ack lost: mark_sent only after HTTP 2xx (linkstuffs_http.py:201) or MQTT
publish-ack (linkstuffs.py:203-210) — at-least-once; a lost response causes a retry; on ThingsBoard a
retry with the same ts overwrites rather than duplicating, so practical duplication is bounded.
(iv) Pre-lot rows when the lot never starts: TTL 3600 s (csv_store.py:42) and hard cap 200/key
(csv_store.py:43, 141-145); the cap path records the drop in the journal, the TTL path does not
(FINDING 3). Residual risk (not a code bug): W5's guarantee depends entirely on the journal DB on C: —
loss/corruption of ingress_journal.sqlite3 (same disk, power failure, disk death) permanently loses every
event acked-but-not-yet-in-a-CSV; there is no second copy of the accepted event before the lot file lands.

**4. PER-MACHINE SEPARATION.** Journal: one shared DB, endpoint_id column + index (journal.py:172-194);
replay blocks per machine so one stuck tool cannot reorder another (service.py:1331-1370). MQTT outbox:
one shared DB, partition_key=endpoint_id (service.py:192-195, outbox.py:128-129); FIFO head per partition
(outbox.py:168-198). HTTPS: a distinct outbox FILE per machine, name carries a sha1 digest of the endpoint
so colliding sanitized names cannot share a queue (service.py:577-587, test_durable_ingress.py:168-177),
plus owner_display_name guard in the publisher (linkstuffs_http.py:63-66, 213-220). CSV: per-machine
csv_local_dir (models.py:135-141) and filenames keyed display_name+microsecond timestamp+LP
(csv_store.py:398-408); buffers keyed (endpoint_id, load_port) (csv_store.py:127). Duplicate endpoint_id or
display_name is a hard ConfigError (config.py:538-556). Same lot id on two machines → different dirs and
independent buffers.

**5. OUTBOX.** Retention default 30 d (config.py:800); purge_old deletes only status IN ('sent','dead')
older than the cutoff (outbox.py:257-264) — pending rows are never purged. Per-partition cap
100,000 pending (outbox.py:35, 141-149) → OutboxFullError → journal replay holds the machine's stream and,
after MAX_DISPATCH_ATTEMPTS=10 (service.py:91), permanently parks the entry (FINDING 2). Per-machine
dead-letter: mark_dead/requeue_dead exist (outbox.py:233-255) but are never called (FINDING 1). HTTPS down
for days: rows accumulate; worker retries each with in-loop retries + Retry-After (capped 60 s,
linkstuffs_http.py:283-317) and cross-loop exponential backoff ≤300 s (outbox.py:216-230); once the
partition hits the cap, new events take the FINDING 2 path.

**6. MQTT-vs-HTTP disabled.** queue_event/queue_machine_connect/queue_machine_attributes all return on
`if not self.config.enabled` (linkstuffs.py:62,73,87,97; linkstuffs_http.py:115,133,148); start() no-ops
(linkstuffs.py:106-108, linkstuffs_http.py:159-163); legacy_api.queue_event likewise (legacy_api.py:101-104).
Verified: test_reliability_remediation.py:38-59 — disabled transports create zero outbox rows. No
undrainable growth.

**7. NETWORK CSV MIRROR.** Local atomic write happens first and its rows are released (journal csv done)
before the mirror attempt (csv_store.py:313-340). Mirror failure appends to journal.csv_mirror
(csv_store.py:326-329, journal.py:382-393) and the supervisor retries every cycle (service.py:949).
Failure never blocks ingestion and never loses local data (test_davinci_multi_machine_audit.py:359-394).
Two gaps: crash between the local write and enqueue_mirror leaves the network copy permanently unmade
(FINDING 4), and csv_mirror has no cap if the share is down for a long time.

**8. SVID/ALARM paths.** S5F1 alarms are journaled (persist-before-ack) and durable (service.py:1469-1537).
Alarm rate limiting sheds above the per-machine limit but records each drop with a reason in the journal
(service.py:1522-1532, _note_alarm_shed :1539-1548) and emits an AlarmStormSummary event
(:1550-1574); clears and safety-class alarms are never shed (alarms.py:52-59). Alarms during an outage
stay queued in the outbox and deliver later. Alarm-state-unknown health event after every connect
(service.py:1594-1623). SVID samples are best-effort: they are NOT journaled (service.py:1881-1883);
if the outbox is full or the poll fails the sample is dropped (exception swallowed by the loop,
:1888-1890). None-valued SVIDs are silently filtered (:1870-1872).

**9. SILENT DROPS.** Unknown CEIDs: NOT dropped — mapped to event_type="unknown" with a one-time WARN
(mapper.py:281-292, profiles.py:109-127) and captured in both CSV (csv_store.py:16-25) and telemetry.
Unknown SVIDs: kept under SVID_{n} (mapper.py:384-386). Decode failures: ACK=1 (S6F11/S5F1) or SxF0
abort (S16F9/F7, host.py:761-766, 788-793) — the tool retains the message. Empty/malformed S6F11 body:
ValueError → ACKC6=1 (host.py:266-267). Oversized payloads: MQTT publish ValueError → mark_failed and
retried forever with backoff (linkstuffs.py:222-230) — never dead-lettered; very large telemetry payloads
serialize (test_davinci_multi_machine_audit.py:424-449). Alarm 0.5 s (machine, ALID, set/clear) dedup sheds
genuine rapid repeats — recorded as dropped (service.py:1514-1521).

## FINDINGS

### FINDING 1 — Dead-letter machinery is dead code; bad-token rows retry forever and are never purged — Severity: Medium
Location: eap_middleware/outbox.py:233-255 (mark_dead, requeue_dead); eap_middleware/linkstuffs_http.py:189-196
Evidence: mark_dead's docstring says `Status 'dead' is excluded from pending() so it is never retried... e.g. a 4xx bad-token response`, but the publish loop does
```python
except _PermanentPublishError as exc:
    self.outbox.mark_failed(item.id, str(exc))   # linkstuffs_http.py:193  (never mark_dead)
```
and mark_dead/requeue_dead are never called anywhere (grep: only definitions + a unit test). purge_old
(outbox.py:257-264) deletes only 'sent'/'dead' rows, so a 4xx row stays 'pending' forever.
Impact: a wrong token keeps one row per event growing the partition with retries every ≤300 s; the
operator-only requeue_dead recovery path (test_durable_ingress.py:180-187) is unreachable; the partition
cap eventually turns this into FINDING 2's loss. Fix: on _PermanentPublishError call mark_dead; keep
requeue_dead as the documented repair path; surface dead count in status (already in stats()).

### FINDING 2 — Full outbox → 10 replay attempts (~50 s) → journal entry permanently parked = telemetry lost — Severity: High
Location: eap_middleware/service.py:91, 1309-1329, 1452-1467; eap_middleware/outbox.py:141-149
Evidence:
```python
if self.journal.dispatch_attempts(entry.seq) >= MAX_DISPATCH_ATTEMPTS:   # service.py:1319
    self.journal.mark_dispatch_dropped(entry.seq, f"parked after {MAX_DISPATCH_ATTEMPTS} failures: {exc}")
```
When a partition hits max_pending_per_partition (100k, outbox.py:35), enqueue raises OutboxFullError
(outbox.py:146-148). _dispatch_event enqueues MQTT first, then HTTP, then legacy (:1454-1456): the HTTP
raise aborts before mark_dispatched, so every 5 s supervisor replay (:928-935) increments attempts; after
10 passes the entry is marked dispatch 'dropped' — pending_dispatch (:359) never returns it again and
nothing requeues journal 'dropped' rows. Impact: after ~1 day+ of HTTPS down (or a wedged MQTT broker
filling the shared outbox), the machine's telemetry is silently and permanently dropped for the affected
sink(s); CSV stays intact and the payload remains readable in the journal for 30 days, but there is no
auto-recovery and the event was already ACKed to the tool. Fix: park only after a time-based threshold
(e.g. hours), reset attempts on partial success, or keep OutboxFullError entries 'pending' (never parked)
and rely on the cap + a status alarm instead.

### FINDING 3 — Pre-lot TTL prune drops rows without journal accounting; entries stuck 'pending' forever (journal grows unboundedly) — Severity: Medium
Location: eap_middleware/csv_store.py:260-278 (_prune_pre_lot), 141-145 (cap path, contrast), 202-216 (flush_all)
Evidence: the cap path releases the journal ref:
```python
self._release([dropped[2]], reason="pre-lot buffer cap reached")   # csv_store.py:144
```
but _prune_pre_lot only pops the in-memory bucket:
```python
while bucket and bucket[0][0].timestamp() < cutoff:
    bucket.pop(0); dropped += 1          # csv_store.py:269-271  — no _release(...)
```
Reproduced: after a TTL prune, `holds(seq)` stays True and the journal entry remains csv_status='pending'
indefinitely; replay skips it (holds True), so it is never written and never marked dropped; purge_old
requires terminal statuses, so it is never deleted. flush_all (:202-216) also leaves pre-lot refs unresolved
(by design — rows stay replayable), but combined with TTL that just resets the leak each restart.
Impact: a machine that sends pre-lot rows without a lot_start (missing LotID) leaks one permanent pending
journal row per event: csv_pending in status grows forever, journal DB grows without bound, and TTL-dropped
rows get no audit record despite the journal's "nothing is discarded silently" contract. Fix: call
_release(seqs, reason="pre-lot TTL expired") from _prune_pre_lot.

### FINDING 4 — Network-mirror crash window and uncapped mirror queue — Severity: Low
Location: eap_middleware/csv_store.py:325-340; eap_middleware/journal.py:382-411
Evidence: `self._release(buffer.row_seqs)` (marks csv 'done', csv_store.py:333) runs before the mirror
block; a crash after the local write but before `self._journal.enqueue_mirror(...)` (:328-330) leaves the
network copy permanently unmade with no record. csv_mirror has no size cap; pending_mirrors(limit=200)
(journal.py:395) retries each pass but a multi-day share outage grows the table by one row per lot file.
Impact: local data is never lost (the journal/csv contract is the local file); only the convenience network
copy is skipped, silently, in a narrow window. Fix: enqueue_mirror before _release, and cap/flush-old
csv_mirror rows.

### FINDING 5 — Residual single point of failure: acked-but-unwritten CSV rows exist only in the journal — Severity: Medium (operational risk, not a defect)
Location: eap_middleware/service.py:1247/1466; eap_middleware/journal.py:147
Evidence: the ACK goes out after journal.append + in-memory CSV buffer append; the row is not fsynced to a
lot file until lot end (csv_store.py:417-427). The only crash protection for W5 is replay from
ingress_journal.sqlite3, and _write_atomic fsyncs the file but not the containing directory
(csv_store.py:417-427). If C:/SECSGEM_EAP/data/ingress_journal.sqlite3 is lost or corrupted (disk death,
antivirus, manual deletion), every event acked-but-not-yet-flushed is permanently gone while the tool
believes it was delivered. Impact: correct by design for crash/restart; exposed to single-disk loss.
Fix: replicate the journal (or treat the journal as the recovery point of record and back it up); fsync
the parent directory after os.replace.

### Negative results (verified, no finding)
- NO ack-before-persist anywhere: S6F11/S5F1 ACK=0 only after durable journal commit; failure → ACK=1/abort, tool retries (host.py:227,233,730,736; service.py:1240-1267; secsgem handler.py:132-139; tests test_reliability_remediation.py:133-144).
- NO cross-machine identity collision possible: endpoint_id is inside every journal key, outbox key, HTTP outbox filename, CSV buffer key, and config rejects duplicate endpoint_id/display_name (config.py:538-556).
- Disabled transports are true no-ops — no undrainable queue growth (question 6; test_reliability_remediation.py:38-59).
- Mirror failure never blocks ingestion nor loses local data (question 7; test_davinci_multi_machine_audit.py:359).
- Unknown CEIDs are captured ('unknown'), not dropped; unknown SVIDs fall back to SVID_{n}; decode failures are refused at the wire (question 9).
- Retransmitted messages (same system bytes) collapse to one downstream delivery; replay re-enqueues idempotently (question 2; tests test_durable_ingress.py:62-94, test_davinci_multi_machine_audit.py:151-190).
- Per-machine ordering is FIFO per partition with per-machine blocking on failure; one stuck machine cannot reorder another (outbox.py:168-198; service.py:1354-1366; test_durable_ingress.py:135-145).
- Config validation rejects enabled machines without an HTTPS route/token (config.py:705-726; test_reliability_remediation.py:81-83).
- One doc drift, no code impact: config/production.yaml's comment claims an absent token "makes the publisher drop events silently"; the code (linkstuffs_http.py:222-232) queues until repaired.

## Summary table

| # | Finding | Severity | Location | Data loss? |
|---|---------|----------|----------|------------|
| 1 | mark_dead/requeue_dead dead code; 4xx rows retry forever, never purged | Medium | outbox.py:233-255, linkstuffs_http.py:189-196 | eventual (via #2) |
| 2 | Outbox-full → 10 attempts → journal entry parked: telemetry permanently dropped for that sink | High | service.py:91,1309-1329; outbox.py:141-149 | YES (telemetry) |
| 3 | Pre-lot TTL prune leaks journal refs; entries stuck pending forever; unbounded journal growth | Medium | csv_store.py:260-278 | CSV rows dropped w/o record |
| 4 | Mirror crash window (copy skipped) + uncapped mirror queue | Low | csv_store.py:325-340; journal.py:382-411 | network copy only |
| 5 | Journal is the sole copy of acked-but-unwritten rows; no dir fsync after rename | Medium | service.py:1247/1466; journal.py:147 | only if journal DB lost |

Bottom line: per-machine data separation, dedup, and persist-before-ack are solid and test-covered; the
ingestion/CSV/outbox chain is at-least-once with journal replay. The two real loss paths are FINDING 2
(a full outbox converts a down-stream into permanent telemetry loss after ~50 s of backpressure) and
FINDING 3 (pre-lot TTL rows drop from CSV without audit and leak the journal). No ack-before-persist,
no cross-machine key collisions, no silent unknown-CEID drops.
