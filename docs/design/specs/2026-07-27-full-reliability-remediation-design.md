# Full Reliability Remediation Design

Date: 2026-07-27

## Objective

Resolve every actionable defect identified by the DaVinci PDF and engineering
review except credential visibility. Existing ThingsBoard and legacy encryption
credentials remain hardcoded at the user's explicit request. ThingsBoard HTTPS
is the primary upstream transport; MQTT remains optional, disabled by default,
and completely inert while disabled.

## Scope

The implementation covers:

- DaVinci E30/E40 wire-format compliance and acknowledgment behavior.
- Durable event ingestion, CSV buffering, and upstream outboxes.
- Correct multi-load-port, multi-report, and multi-substrate attribution.
- Idempotent report subscription provisioning.
- Alarm-state correctness and configurable rate limiting.
- HTTPS-first routing, retry behavior, and diagnostics.
- Configuration validation and atomic single-instance enforcement.
- DaVinci simulator fidelity and generated configuration correctness.
- Safe test isolation and regression coverage for every repaired defect.

The implementation does not remove, rotate, mask, or externalize existing
hardcoded production or legacy cryptographic credentials.

## Architecture

The current service, mapper, gateway, publisher, and simulator boundaries remain
in place. This is a cohesive reliability pass rather than a transport rewrite.
Public interfaces remain compatible where practical; new plural mapping APIs
are added alongside existing scalar wrappers.

### HTTPS-first transport routing

ThingsBoard HTTPS is the primary upstream path. At startup, each enabled machine
must have at least one usable upstream route:

1. A non-empty HTTPS device token keyed by the machine's `display_name`; or
2. An enabled MQTT gateway configuration.

When HTTPS is enabled and MQTT is disabled, every enabled machine must have a
matching HTTPS token. Publisher queue methods become no-ops when their transport
is disabled, preventing undrainable outbox growth. MQTT support and its tests
remain available as an explicitly enabled fallback.

### Acceptance-aware ingestion

S5F1 and S6F11 handlers return success only after decoding and the registered
callback complete. Exceptions return the appropriate nonzero protocol
acknowledgment so equipment may retain or retry the message. Durable outbox keys
make retries idempotent.

Local CSV persistence is the durability boundary for lot files. A buffer is
removed only after the local atomic write succeeds. A network-copy failure is
reported but does not roll back the already durable local file.

### Job and substrate attribution

Load-port resolution uses evidence in this order:

1. Explicit PortID/load-port data.
2. A known CtrlJobID-to-port mapping.
3. A known wafer/substrate-to-port mapping.
4. A known lot-to-port mapping.
5. The only currently active load port.

If multiple active ports remain possible, the event is unresolved rather than
assigned to the last-arriving port. Tracker state is populated from carrier,
material, control-job, and E90 substrate events and cleaned when a carrier/job
leaves.

E90 list payloads are expanded into aligned per-substrate records. A new plural
mapper API returns all canonical events, while the current single-event API
remains as a compatibility wrapper for scalar payloads and legacy callers.

S6F11 parsing preserves every report, including RPTID and its V list. The mapper
selects the middleware-owned report by configured RPTID and expected layout;
unknown additional reports remain available in the raw payload instead of being
silently discarded.

## Protocol compliance

### E40

- S16F7 and S16F9 are defined as reply-requiring equipment-to-host messages.
- S16F8 and S16F10 are header-only confirmations.
- Handlers emit empty confirmations.
- S16F7 ACKA, ERRCODE, and ERRTEXT data is retained in canonical telemetry so
  aborted and failed outcomes remain distinguishable.

### E30 report subscriptions

DRACK 3 is treated as a collision, not successful verification. The subscription
manager deletes only middleware-owned colliding report IDs using the documented
zero-length report definition, redefines them, and proceeds only after a
successful acknowledgment. Link and enable steps remain mandatory.

### Status and report handling

`request_status(None)` sends an empty S1F3 list, which requests all DaVinci status
variables. Multi-report S6F11 messages preserve and process the configured report
without losing other reports.

## Alarm behavior

Alarm limiting is an optional per-machine setting and defaults to disabled.
When configured:

- Alarm clears always pass.
- Personal- and equipment-safety alarms always pass.
- Set/clear state is tracked by ALID.
- S5F1 and alarm collection-event paths share one policy so neither bypasses
  throttling and duplicated notifications can be deduplicated.
- Storm summaries retain sufficient alarm identity/category information for
  operational diagnosis.

The middleware remains observational and does not control or bypass physical
EMO, door, load-port, or other equipment safety interlocks.

## HTTP delivery behavior

HTTP 408, 425, 429, and 5xx responses are retryable with bounded backoff.
`Retry-After` is honored when present. Authentication/authorization failures and
structurally invalid payload responses are dead-lettered after recording the
redacted error. URL and token logging remains redacted even though credentials
remain hardcoded in configuration.

## Configuration and locking

Startup validation covers:

- TCP port range 1-65535.
- SECS device ID range 0-32767.
- Nonnegative retry counts, retry delays, reconnect intervals, retention, and
  operational timing values, with strictly positive values where zero is not
  meaningful.
- Unique machine identifiers and display names.
- A usable upstream route for every enabled machine.

Single-instance enforcement uses an atomic operating-system file lock. PID
content remains for diagnostics, and stale files can be reclaimed only after the
OS lock is no longer held.

## Diagnostics and generated configuration

`test-machine` performs an HSMS connection/Select and SECS identity exchange for
enabled targets; disabled targets are skipped unless explicitly selected.
`test-linkstuffs` tests HTTPS first using a non-mutating ThingsBoard attributes
request. MQTT is tested only when enabled.

The generated DaVinci gateway snippet is updated to the current machine schema,
including `endpoint_id`, `display_name`, `machine_profile`, `secs_device_id`,
per-machine collection flags, and valid path fields.

## Simulator fidelity

The DaVinci simulator is corrected to:

- Return all configured SVs for empty S1F3.
- Return the complete SV name list for S1F11.
- Return all configured equipment constants for empty S2F13.
- Report the documented DaVinci model and software revision in S1F1.
- Encode E90 status/state/type arrays as documented U1 enumerations.
- Model report-definition collision/delete behavior.
- Support multi-report S6F11 fixtures.

The simulator remains deterministic and local; it does not contact external
services.

## Test isolation and verification

Live ThingsBoard tests receive a `live` pytest marker and require an explicit
environment opt-in. Default pytest configuration excludes them, while keeping
their hardcoded credentials unchanged as requested.

Regression tests cover:

- Correct header-only E40 confirmations and preserved alert errors.
- Nonzero acknowledgments for decode and callback failures.
- Two concurrent load ports using real DaVinci PM report shapes.
- Multi-substrate E90 expansion and multi-report S6F11 preservation.
- DRACK collision recovery.
- CSV write failure retaining the buffer.
- Disabled MQTT producing no pending outbox rows.
- Per-machine HTTPS route validation.
- Retryable HTTP statuses and `Retry-After`.
- Alarm clear/safety priority and unified event-path behavior.
- Empty-list status/constant semantics and documented simulator identity/types.
- Atomic single-instance contention.
- Configuration boundary validation.
- Correct current-schema generated configuration.
- Non-mutating HTTPS diagnostics and MQTT conditional diagnostics.

Final verification consists of the complete non-live pytest suite, targeted
protocol and persistence tests, production/generated configuration loading,
Python bytecode compilation for `eap_middleware`, `gateway`, and `simulator`, and
any configured static checkers available in the environment. No live
ThingsBoard writes are part of verification.

## Success criteria

- Every non-credential review finding has a regression test and implementation
  fix.
- Disabled MQTT creates no worker and no outbox records.
- Every enabled machine has a validated HTTPS or MQTT route.
- Equipment is never sent a success acknowledgment after failed ingestion.
- Concurrent jobs are never assigned to a port without unambiguous evidence.
- All local lot data survives recoverable write failures.
- Simulator payloads match the documented DaVinci types and query semantics.
- Default tests perform no external writes.
- The full non-live test suite and compilation pass.
