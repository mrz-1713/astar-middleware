# Production-Readiness Remediation Design

**Date:** 2026-08-31

**Status:** Approved for implementation

**Source audit:** `docs/PRODUCTION_READINESS_AUDIT_2026-08-31.md`

## Objective

Make the middleware, simulator, packaging, and release process technically
ready for a production release candidate by closing every repository-actionable
finding in the source audit. Preserve the audited uncommitted baseline and do
not represent credential-, infrastructure-, tenant-, OEM-, or equipment-bound
acceptance work as complete unless retained evidence proves it ran.

The repository becomes **release-capable** when the local and CI gates in this
design pass. A specific artifact becomes **production-approved** only after the
external release gates are also satisfied for that exact commit and artifact.

## Selected Approach

Three approaches were considered:

1. Fix only the P0/P1 code defects. This is fast but leaves recovery,
   deduplication, supply-chain, and quality findings unresolved.
2. Close all repository-actionable PR-01 through PR-13 findings and encode the
   remaining external work as fail-closed release evidence gates. This is the
   selected approach because it produces a defensible release candidate without
   claiming that software tests replace commissioning or safety approval.
3. Narrow the supported product contract by removing MQTT fallback and vendor
   profiles that lack qualification. This reduces risk but contradicts the
   intended middleware scope and its operator documentation.

## Scope and Finding Map

The implementation covers:

- PR-01: a dedicated, pinned development/CI dependency manifest containing
  `openpyxl`, while keeping generation-only dependencies out of the offline
  runtime wheel set;
- PR-03: storage-capacity monitoring, independent local alerting, critical
  backpressure, recovery behavior, sizing guidance, and fault-injection tests;
- PR-04: a staged, versioned, health-checked, rollback-capable Windows upgrade;
- PR-05: automated Windows service lifecycle and upgrade acceptance;
- PR-06: a supported `HTTPS route OR enabled MQTT gateway` contract;
- PR-07: correct raw-wire S2F13/S2F41 decoding and profile-specific remote-command
  acceptance behavior;
- PR-09: an explicit cross-generation retransmission-window policy;
- PR-10: artifact-signing verification hooks, SBOM, provenance, and separately
  publishable hash evidence;
- PR-11: defined RPO/RTO, backup scope, restore verification, and reconciliation;
- PR-12: an OEM/service-account and equipment-network hardening checklist; and
- PR-13: enforced Ruff checks, justified security-tool suppressions, and packaged
  GUI smoke coverage.

PR-02 and PR-08 cannot be completed by source changes alone. The repository will
provide exact-build identity, qualification matrices, immutable fixture locations,
and fail-closed release evidence checks for them.

## Build Reproducibility and Quality Gates

Runtime and development dependencies are separated. `requirements.txt` remains
the exact portable runtime graph. A pinned development/CI manifest adds
generation, test, lint, type, audit, and SBOM tools, including `openpyxl`. CI
installs the development graph in clean jobs and retains the generated-output
diff gate for the DaVinci workbook.

CI runs Ruff correctness checks, strict Pyright, byte compilation, the default
test suite, the slow 22-machine test, configuration validation, generated-file
checks, dependency auditing, offline lock/hash verification, packaging tests,
and Windows acceptance. Security-tool exclusions must be narrow, inline or
configuration-backed, and explain why the flagged operation is non-security or
otherwise safe.

Each release emits a machine-readable evidence bundle containing source commit,
dirty-state assertion, dependency lock hashes, artifact hashes, test results,
SBOM location, signing result, and commissioning/tenant/OEM gate status. A
release approval command fails if the tree is dirty, required evidence is
missing, an artifact signature is absent or invalid, or evidence identifies a
different commit or artifact.

## Storage Safety and Backpressure

Storage protection is a service-level component rather than a publisher health
event. It samples free bytes/free percentage for every filesystem that holds the
ingress journal, outboxes, CSV state, logs, archives, and machine state, and also
tracks SQLite file growth and queue depth. Configuration supplies warning and
critical thresholds plus a recovery hysteresis. Invalid threshold ordering or
thresholds that cannot provide a positive safety margin fail validation.

State transitions are `normal -> warning -> critical -> recovering -> normal`.
Transitions are debounced, recorded in normal logs/status output, and sent to an
independent Windows Event Log channel. They are not dependent on MQTT, HTTPS, or
any outbox whose outage may have caused the pressure.

At warning, ingress continues and operators receive capacity/queue diagnostics.
At critical, the service stops durable acceptance before reserve space is
consumed: it quiesces affected SECS sessions so equipment cannot mistake failed
local persistence for accepted telemetry, rejects any race-window ingress with
the existing negative acknowledgement, and keeps control/status endpoints
available. Operators must follow the profile-specific safe equipment action in
the commissioning record. Automatic recovery occurs only after the configured
hysteresis is satisfied and journal/outbox integrity checks pass.

Tests use injected capacity probes rather than filling the developer disk. They
cover warning, critical, repeated samples, race-window write failure, independent
alert failure isolation, recovery hysteresis, service restart while critical,
and the interaction between a full outbox and an accumulating journal. The
operations guide includes an event-rate/outage sizing worksheet and prohibits
configuring a reserve smaller than the measured worst-case shutdown/repair need.

## Safe Windows Installation and Upgrade

Application versions are installed beneath a versioned releases directory. The
installer stages a complete candidate without modifying the active release,
verifies its manifest and dependency lock, runs import/config/startup probes,
and only then begins the switch.

For an upgrade, the installer records the active version, stops the service,
waits for a verified stopped/quiescent state, updates an atomic `current`
junction or equivalent pointer, starts the service, and waits for a bounded
health probe. Failure at or after the switch restores the previous pointer and
service state. The prior version is retained until explicit retention cleanup;
runtime data and operator configuration remain outside version directories and
are never rolled back by a code switch.

The service installer sets and verifies NSSM restart-on-unexpected-exit and
restart-delay policy in addition to identity, arguments, working directory,
logging, and startup type. Native command failures remain fatal. The Setup
wrapper propagates inner installer and rollback failure codes.

Windows acceptance uses a disposable runner or VM and a pinned, hash-verified,
approved NSSM source. It installs the service, verifies the virtual identity and
effective ACLs, starts it, validates health, terminates the process to prove SCM
recovery, upgrades while service/runtime state exists, checks preserved
configuration and queues, and validates rollback. Boot-start behavior is tested
by reboot where the environment permits it; otherwise that evidence remains a
mandatory external gate rather than being simulated by a source-text assertion.

## Upstream Routing Contract

An enabled non-offline machine must have at least one usable upstream route:

- enabled per-machine HTTPS with a device token; or
- an enabled global MQTT gateway with its required credentials and secure
  transport configuration.

HTTPS tokens are required only for machines using HTTPS. Enabling both routes is
allowed and preserves the existing dual-delivery behavior. Configuration errors
name the machine and missing route without exposing credentials. Tests cover
HTTPS-only, MQTT-only, both, neither, disabled/offline machines, startup, queued
delivery, outage, and recovery. Operator docs and control-panel validation use
the same rule.

## Simulator Protocol Fidelity

All handlers receive data through one decoding boundary. `_decoded_body`
normalizes raw secsgem bytes and already-decoded variables; helpers unwrap SECS
scalars/lists without stringifying encoded payloads.

S2F13 returns exactly the requested ECIDs in request order and reports unsupported
constants according to the active profile's policy. S2F41 parses RCMD and CP
parameters, consults a profile command table, evaluates communication/control/
process state and parameter rules, applies the accepted state transition, and
returns the profile-appropriate HCACK. The result distinguishes completed
acceptance, accepted asynchronous work, unknown command, invalid parameter,
wrong state, and cannot-perform conditions.

Generic behavior is conservative: unknown commands never return success and do
not mutate state. DaVinci, SPTS, PTIQ, and NexGen tables include only commands
and semantics supported by existing vendor evidence. Installation-specific or
unverified commands remain explicitly unsupported until commissioning supplies
an immutable captured fixture.

Tests call handlers with real encoded bodies and exercise real HSMS round trips.
They cover S2F13 cardinality/order plus S2F41 success, asynchronous acceptance,
unknown command, invalid parameter, wrong control/process state, state mutation,
and packaged ProfileSimulator/NexGen inheritance paths.

## Cross-Generation Event Identity

Connection generation is not blindly added to the ingress key because that would
republish legitimate reconnect retransmissions. Instead, deduplication across a
generation boundary is time bounded. The journal records the first/last receipt
time and generation. An identical endpoint/SF/CEID/system-bytes/payload event in
a new generation is a retransmission only when it arrives inside the configured
cross-generation window; outside that window it receives a new event identity.
Same-generation duplicates retain the existing behavior.

The window is explicit in configuration, bounded to a safe range, visible in
status output, and documented as equipment-specific commissioning input. Tests
cover same-generation retry, reconnect retry inside the window, genuine repeat
outside it, wall-clock boundary behavior, restart persistence, and concurrent
append races.

## Backup, Restore, and Operational Security

The default operational objective is documented as a starting policy, not an
universal site promise: configuration/secrets, journals, outboxes, CSV/admin
state, evidence, and release metadata are included; volatile logs may follow a
separate retention policy. Operators must approve site RPO/RTO values during
commissioning.

A restore verifier checks SQLite integrity, schema compatibility, outbox/journal
referential expectations, configuration validation, CSV reconciliation, release
identity, and service startup without contacting equipment until explicitly
enabled. Runbooks cover application-data restore and bare-system restore, require
protected/off-host copies, and record periodic drill evidence.

The commissioning checklist inventories OEM and service accounts, prohibits
empty/default credentials where the vendor permits changes, records OEM approval,
requires segmented management access, and captures privileged-access logging.
These controls are release evidence; middleware automation does not modify OEM
accounts or safety settings.

## Error Handling and Observability

New failure paths are fail-closed and non-secret-bearing. Storage alerts include
paths, capacity, thresholds, and state, never tokens or payload content. Upgrade
errors identify staging, validation, stop, switch, startup, health, or rollback
stage and preserve the previous release. Simulator rejections expose command and
reason codes suitable for tests but do not claim equipment behavior beyond the
selected profile evidence.

Status output exposes storage state, measured reserve, database sizes, queue
depths, active release identity, configured upstream route types, and simulator
profile/command-policy identity. Health publication to upstream remains useful
telemetry but is not the sole alert path for failures that impair upstream.

## Verification and Completion Criteria

Repository implementation is complete only when:

- focused tests for each affected finding pass;
- clean generation works from the dedicated development manifest;
- Ruff, strict Pyright, byte compilation, `git diff --check`, the default tests,
  and the slow test pass;
- dependency audit, SBOM/provenance generation, release lock/hash verification,
  and all public/packaged configuration validations pass;
- encoded-body and real-HSMS simulator tests pass;
- storage fault-injection passes through warning, critical, restart, and recovery;
- Windows service install, recovery, upgrade, and rollback acceptance is green in
  a supported Windows environment; and
- release-evidence validation correctly refuses absent, mismatched, unsigned, or
  unqualified artifacts.

Production approval additionally requires retained evidence for real HTTPS/MQTT
tenant testing; representative DaVinci, SPTS, PTIQ, and NexGen equipment and
software revisions; actual HSMS identifiers/timers/subscriptions; complete lot
and outage/recovery behavior; signed artifacts; reboot/power-loss, disk-full,
backup/restore and corruption drills; and equipment-owner/OEM safety and access
approval. Until those gates pass for the exact release candidate, deployment is
limited to isolated, supervised commissioning.

## Change Discipline

The audited baseline contains 72 modified, added, deleted, or untracked paths.
They are treated as intentional user work. Remediation extends overlapping files
carefully, never discards unrelated edits, and stages/commits only files belonging
to the current design step. No repository-history rewrite, credential use,
production connection, OEM account change, or live-equipment mutation is implied
by this approval.
