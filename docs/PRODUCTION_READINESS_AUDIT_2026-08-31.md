# ASTAR Middleware and Simulator Production-Readiness Audit

**Audit date:** 2026-08-31  
**Audited workspace:** `/Volumes/Backup/astar-middleware-main`  
**Audited tree:** the working tree that became the initial commit of this
repository. The pre-publication history this audit ran against was discarded when
the repository was re-initialised, so no SHA from it resolves.  
**Verdict at audit time:** **NOT PRODUCTION READY**  
**Verdict now:** **RELEASE CANDIDATE** - every repository-actionable finding is
closed and gated by a test; the remaining gates need physical equipment, a
production tenant, and site sign-off, and none of them can be closed from source.

## Remediation status

Recorded here rather than in a second document so the finding and its closure
cannot drift apart. Findings below are the ones raised in this audit.

| Finding | State | Where it is enforced |
|---|---|---|
| PR-01 clean-CI dependency gap | Closed | `requirements-dev.txt` pins `openpyxl`; every CI job installs it |
| PR-02 no reproducible baseline | Partly closed | `scripts/release_evidence.py` and `.github/workflows/release.yml` bind a signed artefact to a clean commit; the *approval record* is still a human act |
| PR-03 disk exhaustion | Closed | `eap_middleware/storage_safety.py`; fault-injected in `tests/test_production_readiness_remediation.py` |
| PR-04 destructive upgrade | Closed | `deploy/upgrade.ps1` stages, health-checks and rolls back; CI injects a post-switch fault and asserts rollback |
| PR-05 service lifecycle outside CI | Closed | "Windows service lifecycle, upgrade, and rollback acceptance" step installs NSSM, starts the service, kills the child process and proves restart |
| PR-06 MQTT-only rejected | Closed | Validation accepts "HTTPS route OR enabled MQTT gateway"; `test_mqtt_only_is_a_supported_upstream_route` |
| PR-07 simulator false positives | Closed | S2F13/S2F41 decode the real wire body; HCACK is profile-specific |
| PR-08 commissioning | **Open, cannot be closed here** | Needs physical tools. See `docs/PRODUCTION_RELEASE_GATES.md` |
| PR-09 cross-generation dedup | Closed | Bounded retransmission window; `test_cross_generation_retries_are_bounded` |
| PR-10 unsigned binaries | Closed in CI | `packaging/sign_artifact.ps1` and the signing job require Authenticode; needs a real certificate |
| PR-11 backup/recovery | Closed | `eap_middleware/restore.py`, `scripts/verify_restore.py`, `docs/STORAGE_CAPACITY_AND_RECOVERY.md` |
| PR-12 OEM account hardening | Closed as guidance | `docs/OEM_SERVICE_ACCOUNT_HARDENING_CHECKLIST.md`; execution is a site act |
| PR-13 quality gates | Closed | Ruff runs in CI; Bandit is clean at medium and above |

Gates 8 through 12 of the acceptance list at the end of this document remain open
by nature. A green repository is a release *candidate*; it is not an approval.

## Executive decision

The core middleware has several strong production-oriented controls: it journals
SECS ingress before acknowledgement, uses durable per-sink outboxes, isolates
machine sessions, defaults the tracked production configuration to disabled,
requires TLS unless an explicit insecure test override is set, redacts sensitive
configuration, and has a broad automated suite. The current tree passed 690 tests,
the 22-machine slow test, compilation, Pyright, configuration checks, and a pinned
dependency vulnerability scan.

Those strengths are not sufficient for release approval. The audited tree has
confirmed blockers in five areas:

1. a clean CI environment deterministically fails the DaVinci generated-table
   gate because `openpyxl` is absent from the installed dependency manifest;
2. sustained upstream failure can fill a bounded outbox while the unbounded
   ingress journal continues growing, with no disk-capacity monitor or independent
   alert path;
3. the Windows upgrade is destructive in place and the actual Windows service
   installation/update/start path is not exercised by CI;
4. an advertised MQTT-only deployment is rejected by configuration validation;
5. the simulator gives false-positive protocol results for real encoded S2F13 and
   S2F41 traffic, so it cannot presently serve as evidence for those behaviors.

The release also lacks the required real-equipment and tenant qualification.
NexGen is explicitly unverified on hardware, PTIQ identifiers are installation
specific, and SPTS report layouts are not supplied by its vendor manual. A
simulator can support commissioning but cannot prove physical interlocks, OEM
control-state behavior, or a production Linkstuffs tenant.

## Audit scope and evidence

### Vendor PDFs and page images

All PDFs recursively under `docs/vendor` were extracted to text, rendered page by
page, assembled into contact sheets, and visually inspected. This included diagrams,
screenshots, state models, wiring/mechanical illustrations, warnings, tables, and
UI sequences that text extraction alone does not preserve.

| Document | Pages |
|---|---:|
| NexGen MG Series SECS V1.1.18 | 197 |
| Omega SPTS fxP 200 mm SECS-II manual | 231 |
| DAVINCI 200 Maintenance Manual V1.7 | 52 |
| DaVinci 200 Preventive Maintenance Checklist V1.6 | 16 |
| DaVinci 200 User Manual V1.8 | 61 |
| DaVinci 200 A-Star Safety Test V1.3 EN | 11 |
| DaVinci Recovery | 5 |
| Macrium Backup & Restore | 10 |
| Macrium Reflect Backup | 7 |
| Manual Service (23 Aug 2011) | 48 |
| Software Operation Manual EN | 204 |
| TC User Documentation EN | 151 |
| TheWizard User Manual | 156 |
| **Total** | **1,149** |

Inspection artifacts contain 1,149 rendered page images and 101 contact sheets.
`pdfimages -list` reported 2,427 embedded raster-object occurrences. Repeated logos,
masks, and reused objects are included in that number. Text extraction produced
49,913 lines. Render and extraction artifacts are under `tmp/pdfs/vendor_render`,
`tmp/pdfs/vendor_images`, and `tmp/pdfs/vendor_text` and are not release inputs.

### Code and release surfaces

The audit covered:

- middleware configuration, session lifecycle, SECS runtime, mapping, job tracking,
  alarm handling, CSV durability/mirroring, ingress journal, MQTT/HTTPS/legacy
  publishers, control plane, CLI, and GUI boundary;
- generic, DaVinci, SPTS, PTIQ, and NexGen profiles and generated subscription data;
- equipment and host simulators, profile simulator, NexGen simulator, runner,
  simulator configuration, and simulator GUI boundary;
- Windows offline package, Inno Setup wrapper, service installer, upgrade behavior,
  dependency locks/wheels, GitHub Actions, and operational documentation;
- tests, static analysis, security scanning, configuration validation, direct
  wire-body reproductions, and targeted failure-mode reproductions.

This is an audit of the available repository and local runtime. It is not a SEMI
certification, electrical safety certification, penetration test, or real-tool
factory acceptance test.

## Release-blocking findings

### PR-01 — Clean CI is guaranteed to fail the DaVinci generation gate

**Priority:** P0 release blocker  
**Area:** build reproducibility

`requirements.txt` installs pandas but not `openpyxl`. Pandas does not declare
`openpyxl` as a transitive dependency. The test deliberately skips workbook
checks when `openpyxl` is absent (`tests/test_vendor_doc_coverage.py:25-32`), but
the next CI step unconditionally invokes
`python -m scripts.gen_davinci_full_subscription`
(`.github/workflows/middleware.yml:43-54`). The generator opens the `.xlsx` through
pandas and therefore requires `openpyxl` (`scripts/gen_davinci_full_subscription.py`).

This was reproduced in a fresh Python 3.11 virtual environment populated only
from `requirements.txt`: 27 packages installed successfully, then the generator
failed with `ModuleNotFoundError: No module named 'openpyxl'` / pandas
`ImportError: Import openpyxl failed`.

**Required remediation:** add and pin `openpyxl` in a dedicated CI/development
dependency manifest, or change the generator to use a dependency that is actually
installed. Keep it out of the offline runtime wheel set if runtime does not need
it. Run the full clean matrix and preserve the generated-output diff gate.

### PR-02 — There is no releasable, reproducible baseline or acceptance record

**Priority:** P0 release blocker  
**Area:** release governance and qualification

The audit ran against a feature branch with 73 modified/added/deleted/untracked
status entries. HEAD is not the audited source state, so recreating HEAD does not
recreate the system tested here. `git ls-remote` exposed only `origin/main`; no
remote branch/ref identifies this working tree. There is also no attached evidence
of a successful Windows CI run for this exact content.

Four external-transport tests are marked `live` and were not executed. There is no
real-equipment acceptance record for the current profile/runtime changes. In
particular, `docs/NEXGEN_MG_PROFILE_NOTES.md:8-13` explicitly says that no NexGen
constant has been observed on a real tool.

**Required remediation:** commit the intended source and generated assets, review
the exact diff, tag/build from a clean commit, record CI artifact hashes and build
identity, and attach signed acceptance evidence for the exact release candidate.

### PR-03 — Sustained upstream outage can exhaust the disk and lose telemetry

**Priority:** P0 data-integrity blocker  
**Area:** durability and operations

Each SQLite outbox caps pending rows per partition at 100,000
(`eap_middleware/outbox.py:41-50,167-176`). When the outbox is full, the ingress
row intentionally remains pending for replay (`eap_middleware/service/dispatch.py:149-169`).
The ingress journal purges only rows whose dispatch and CSV states are terminal;
pending rows are never purged (`eap_middleware/journal.py:541-559`). No disk-free,
database-size, or filesystem-capacity monitor was found in the service.

The resulting failure chain is:

1. upstream is unavailable long enough to fill an outbox partition;
2. new events continue to be journaled and acknowledged but cannot enter the full
   outbox;
3. pending journal rows accumulate without a size bound;
4. the data volume eventually fills;
5. later journal writes fail and the host returns a negative SECS acknowledgement.

At step 5 the design relies on the equipment retaining/retrying the event. The
NexGen manual and `docs/NEXGEN_MG_PROFILE_NOTES.md:40` state that spooling is not
supported, so loss is possible once local durable acceptance fails. Health events
are queued to the same impaired upstream and are not an independent alert channel.

**Required remediation:** introduce high/critical disk-free and database-growth
thresholds, an independent Windows Event Log/service-monitor alert, tested
backpressure policy, capacity sizing by event rate and outage objective, operator
runbooks, and fault-injection tests through warning, critical, recovery, and
disk-full states. Define the safe equipment action for a critical threshold.

### PR-04 — Windows upgrades destructively replace the live application in place

**Priority:** P1 high  
**Area:** deployment and rollback

`deploy/install.ps1:259-294` deletes each existing application directory and then
copies the replacement. The installer does not itself stop the service, stage a
complete candidate, validate it, atomically switch versions, or roll back on
failure. The long-form operations guide tells an operator to stop the service
first (`docs/MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.md:571-593`), but that safety
condition is not enforced.

An interrupted copy can leave a stopped system with a partial application. If an
operator misses the manual stop, a running process can coexist with a partially
replaced module tree. CI's “upgrade” simply runs the same Setup EXE twice and does
not simulate an active service or an interrupted copy
(`.github/workflows/middleware.yml:158-180`).

**Required remediation:** enforce service stop and quiescence, stage into a
versioned directory, verify manifest/config/import/startup probes before switch,
atomically move a `current` junction/pointer, preserve the prior version, and
automatically roll back if the service does not become healthy.

### PR-05 — The actual Windows service lifecycle is outside installer CI

**Priority:** P1 high  
**Area:** deployment acceptance

The Setup wrapper runs `deploy/install.ps1` only
(`packaging/installer/AstarMiddleware.iss:75-110`). That script completes by telling
the operator to run `scripts/install_service.ps1` later
(`deploy/install.ps1:542-578`). This deliberate two-stage procedure is documented,
but the GitHub Actions installer smoke test never installs NSSM, invokes the service
installer, starts the service, tests restart-on-failure/boot behavior, exercises an
upgrade while the service exists, or proves effective access under the virtual
service identity.

The service script itself contains useful hardening: idempotent update, a virtual
service account, restricted runtime ACLs, and NSSM setting verification
(`scripts/install_service.ps1:149-217`). Those controls have not been proven in the
automated release path. It also does not explicitly set and verify an NSSM exit/
restart-delay policy.

**Required remediation:** add a Windows acceptance job or disposable Windows VM
test that obtains an approved NSSM binary, installs the service, verifies identity
and effective ACLs, starts it, validates health, kills the process to prove recovery,
upgrades it, reboots (or equivalent SCM boot test), and verifies preserved config,
queued data, and rollback.

### PR-06 — Documented MQTT fallback cannot be configured

**Priority:** P1 high  
**Area:** upstream routing

The operator documentation calls the MQTT Gateway protocol an explicit supported
fallback (`docs/LINKSTUFFS_SETUP.md:20-23`, `docs/OPERATIONS.md:136-139`). However,
for every enabled non-offline machine, configuration validation requires an HTTPS
device token and then separately requires the per-machine HTTPS route to be enabled
(`eap_middleware/config.py:900-939`). The checks do not accept an enabled global
MQTT gateway as the route.

A direct reproduction with `linkstuffs.enabled: true`, a non-empty MQTT gateway
token, and machine HTTPS disabled failed with:

> Missing linkstuffs_http.device_tokens for enabled machines: TOOL_01

The contradiction is currently locked in by
`tests/test_unified_control.py:304-320`, which expects this rejection even with
MQTT enabled.

**Required remediation:** decide the supported product contract. If MQTT fallback
is supported, validate “HTTPS route or enabled MQTT gateway,” test MQTT-only startup
and delivery, and align the control panel/docs. If it is not supported, remove the
fallback code and all operator claims rather than leaving a nonfunctional path.

### PR-07 — Simulator wire handling produces false-positive S2F13/S2F41 results

**Priority:** P1 high  
**Area:** simulator fidelity and test validity

The generic simulator correctly decodes raw bodies for S1F3, S2F33, S2F35, and
S2F37, but `_handle_s2f13` iterates `packet.data` directly
(`simulator/equipment.py:319-333`) and `_handle_s2f41` treats raw data as the
command, stringifies it, and always returns HCACK 0
(`simulator/equipment.py:532-548`).

Direct calls using real secsgem-encoded bodies reproduced both defects:

- S2F13 requesting ECIDs `[1, 2]` returned eight values derived from the eight
  encoded bytes instead of two values.
- S2F41 `START` logged the entire encoded byte string as an unknown command,
  left process state `IDLE`, and returned `HCACK: 0`.

The packaged `ProfileSimulator` inherits the S2F41 behavior. The standalone
`NexGenMgSimulator` inherits both generic handlers. The DaVinci/profile S2F13
override does decode the body, so the S2F13 defect is not universal.

Vendor semantics make the success response important: NexGen defines HCACK
failure categories and SPTS commonly returns HCACK 4 for an accepted asynchronous
action. Always returning 0 means simulator acceptance cannot validate command
state, command names, parameters, control-state restrictions, or asynchronous
completion.

**Required remediation:** route both handlers through `_decoded_body`, unwrap SECS
variables, implement profile-specific command/state/parameter tables and HCACK
values, and add raw-body plus real-HSMS round-trip tests for success, unknown
command, invalid parameter, wrong control state, and asynchronous completion.

### PR-08 — Vendor profiles still require installation-specific commissioning

**Priority:** P1 acceptance blocker  
**Area:** protocol conformance

- **NexGen MG:** all identifiers and report layouts are document-derived but not
  hardware verified. The manual omits TCP port, device/session ID, active/passive
  role, and HSMS timer values; repository defaults are explicitly guesses
  (`docs/NEXGEN_MG_PROFILE_NOTES.md:46-53`). Report ordering was manually read from
  diagrams and cannot be reliably reconstructed from extracted text
  (`docs/VENDOR_DOC_AUDIT.md:404-417`).
- **SPTS fxP Omega:** the vendor manual documents events but does not supply
  per-event V[] report layouts; they must be defined and verified per tool
  (`docs/VENDOR_DOC_AUDIT.md:419-423`).
- **PTIQ:** the generic interface leaves CEID/SVID numbers to the equipment EIB
  model export; profile constants are conventional defaults, not authoritative
  site values (`eap_middleware/profiles/ptiq.py:7-11`).
- **DaVinci:** the repository has the strongest tabular/commissioning evidence,
  but a simulator still cannot prove real control state, safety interlocks, recipe
  handling, or equipment-software revision behavior.

**Required remediation:** execute an approved commissioning matrix on each target
model/software revision. Capture identity, actual HSMS settings/timers, accepted
subscription bands, SVID/CEID readback, alarm behavior, reconnect/restart behavior,
at least one complete lot, CSV, tenant telemetry, outage/recovery, and all rejected
bands. Add observed quirks as immutable regression fixtures.

## Additional material findings

### PR-09 — Post-restart deduplication can suppress a genuine repeated event

**Priority:** P2 medium  
**Area:** event identity

The ingress key contains endpoint, S/F, CEID, system bytes, and normalized payload,
but not connection/equipment generation (`eap_middleware/journal.py:68-107`).
`generation` is stored in the row but omitted from key construction
(`eap_middleware/journal.py:255-315`). A direct reproduction appended the same
CEID/system-bytes/body in generation 1 and generation 2; the second append returned
the original sequence with `is_new=False` and retained generation 1. Dispatch then
acknowledges without republishing (`eap_middleware/service/dispatch.py:69-78`).

This is a narrow ambiguity: including generation unconditionally could duplicate
a legitimate retransmission across a host reconnect. The system needs an explicit
equipment-epoch/retransmission-window policy, not simply another key field.

### PR-10 — Release binaries are not code-signed

**Priority:** P2 medium/high  
**Area:** software supply chain

The package manifest and hashed offline wheel lock are good integrity controls, and
the Setup artifact receives a sidecar SHA-256. No Authenticode/signing step or
signature verification for ASTAR-built installers/executables was found. Sidecar
hashes distributed with the same artifact do not establish publisher identity.

**Required remediation:** sign Setup and simulator executables with an approved
certificate/HSM-backed process, verify signatures in release CI and installation
instructions, publish hashes through a separately governed channel, and retain an
SBOM/provenance record tied to commit and build identity.

### PR-11 — Backup/recovery is procedural, not an evidenced recovery objective

**Priority:** P2 medium  
**Area:** business continuity

The vendor PDFs provide manual Macrium and DaVinci recovery procedures. The repo
does not provide a tested backup schedule, protected copy policy, recovery-time
objective, recovery-point objective, periodic restore proof, or automated
reconciliation of the SQLite journal/outboxes and CSV state after restore.

**Required remediation:** define RPO/RTO, include configuration/secrets/journal/
outboxes/CSV/admin tables as appropriate, test bare-system and application-data
restores, and record periodic recovery evidence.

### PR-12 — OEM/service access practices require site hardening

**Priority:** P2 operational security  
**Area:** equipment environment

The DaVinci maintenance manual shows a factory-style camera administrator login
and password on page 49, and the TC User Documentation permits empty passwords in
one configuration flow on page 131. The credentials are not reproduced here.
These are equipment/site risks rather than middleware secrets, but they share the
same production network and can invalidate the middleware's trust assumptions.

**Required remediation:** inventory OEM/service accounts, change defaults where
the vendor permits, prohibit empty passwords, restrict management interfaces to a
separate VLAN/jump host, log privileged access, and obtain OEM approval before
changing safety/service settings.

### PR-13 — Quality gates and local GUI validation are incomplete

**Priority:** P3 low  
**Area:** release hygiene

Ruff `F,E9` reported 14 findings: nine pointless f-strings and five assigned-but-
unused variables. CI does not run Ruff. Bandit reported one high finding for SHA-1
used only as a non-security filename-collision tag, plus medium findings dominated
by internally constructed SQL placeholder strings, guarded bind-all settings,
subprocess/URL use, and simulator network behavior. No confirmed exploitable high
severity issue was established from those results.

The current macOS Python lacks `_tkinter`; 20 GUI tests were skipped locally. The
remaining GUI tests passed and Windows is present in the test matrix, but a passing
Windows result for the exact dirty tree was not available to this audit.

**Required remediation:** make Ruff part of CI, triage/suppress static-security
false positives with justification, and retain packaged Windows GUI startup and
interaction smoke evidence for the release candidate.

## Vendor requirements cross-check

The visual/text review established the following constraints that the release and
commissioning plan must respect:

- GEM communication/control state matters. Host authority belongs in Online Remote;
  equipment/local authority must not be inferred from a selected HSMS session.
- HSMS T3/T5/T6/T7/T8 behavior is equipment specific. DaVinci documentation uses
  45/10/5/10/5 seconds in the available commissioned material; SPTS documents
  30/5/10/5/6. NexGen does not provide those values in the supplied manual.
- NexGen reports formatted recipe management and spooling as unsupported. A host
  design must not depend on either for recovery.
- SPTS and NexGen remote commands use state- and parameter-sensitive HCACK results;
  accepted asynchronous work may be HCACK 4 rather than immediate-completion 0.
- Dynamic report definition/link/enable is band-sensitive. One unsupported VID or
  CEID can reject a whole message, so accepted/rejected bands and readback must be
  captured at commissioning.
- DaVinci operational, recovery, preventive-maintenance, and safety procedures
  require privileged HMI/service actions and physical checks. Middleware tests do
  not replace those OEM procedures.
- Safety interlocks, emergency-stop behavior, doors/covers, gas/vacuum/temperature
  protections, motion clearances, and authorized service access are outside what a
  SECS simulator can certify.

## Validation results

| Check | Result |
|---|---|
| Full default pytest suite | **690 passed, 21 skipped, 5 deselected** in 158.54 s |
| 22-machine marked-slow concurrency test | **1 passed** in 89.24 s |
| GUI-targeted tests | **58 passed, 20 skipped** (`_tkinter` unavailable locally) |
| Live external tests | **4 collected, not run** |
| Pyright 1.1.411 | **0 errors, 0 warnings** |
| Python `compileall` | **passed** |
| `git diff --check` | **passed** |
| Public production config validation | **passed**, 4 definitions, 0 enabled |
| Four packaged simulator config validations | **passed** |
| `pip-audit` 2.10.1 against pinned requirements | **no known vulnerabilities** |
| Ruff 0.12.11 `F,E9` | **14 findings** |
| Bandit 1.8.6 | no confirmed exploitable high issue; findings require documented triage |
| Clean requirements-only DaVinci generation | **failed: missing `openpyxl`** |
| Raw encoded S2F13/S2F41 simulator probes | **failed fidelity expectations** |
| MQTT-only configuration probe | **rejected despite enabled MQTT gateway** |
| Cross-generation journal identity probe | second event returned `is_new=False` |

Skipped live tests cover real HTTPS telemetry, HTTPS publisher delivery, MQTT
gateway connectivity/telemetry, and MQTT outbox drain. They require explicit
credentials and external targets and were not safe to infer from the ignored local
configuration. No real equipment was contacted or mutated during this audit.

## Positive controls worth preserving

- Persist-before-ack ingress with SQLite WAL/`synchronous=FULL` behavior and
  negative acknowledgement on storage failure.
- Durable, partitioned outboxes and per-machine replay ordering.
- Atomic local CSV publication and non-blocking network mirror design.
- Per-machine session/config/storage separation and bounded reconnect logic.
- TLS/certificate-verification gates, explicit insecure lab override, secret
  environment expansion, and redaction tests.
- Disabled-by-default tracked production template using documentation-only IPs.
- Banded subscriptions, generated vendor tables, full-profile recognition data,
  and numerous raw-wire/integration regressions.
- Hashed offline dependency lock and release-manifest verification.
- Hardened service-script intent: virtual account, restricted runtime directories,
  idempotent NSSM updates, and setting verification.

## Mandatory acceptance gates

Production approval should require all of the following, in order:

1. Fix PR-01 and obtain a green clean Windows/Linux CI run from a committed release
   candidate.
2. Implement and fault-test disk monitoring/backpressure/independent alerting for
   PR-03, with an approved capacity and retention model.
3. Replace destructive in-place upgrade with staged, enforced-stop, health-checked,
   rollback-capable deployment.
4. Add an automated Windows service lifecycle/upgrade/recovery acceptance test.
5. Resolve the MQTT product contract and add the corresponding startup/delivery
   tests.
6. Correct S2F13/S2F41 simulator wire handling and vendor HCACK/state semantics.
7. Sign release binaries and publish provenance/SBOM/hash evidence for the exact
   clean commit.
8. Complete representative-tool commissioning for DaVinci, SPTS, PTIQ, and each
   intended NexGen model/software revision; retain captured evidence and regression
   fixtures.
9. Run the four live upstream tests against a production-like tenant and exercise
   authentication failure, throttling, outage, queue growth, recovery, and duplicate
   delivery behavior.
10. Execute install, first-start, power-loss, process-kill, network partition,
    database corruption, disk-low/full, backup, restore, upgrade, and rollback drills
    on the target Windows build.
11. Obtain equipment-owner/OEM safety and access-control sign-off. Simulator results
    must not be used as physical safety evidence.
12. Freeze the release candidate, rerun this matrix from clean media, and approve
    only the signed artifacts produced by that run.

Until these gates are satisfied, the appropriate deployment scope is an isolated,
supervised engineering/commissioning environment—not unattended fab production.
