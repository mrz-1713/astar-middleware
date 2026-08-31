# ASTAR Middleware and Simulator — Final Deep Audit

**Audit date:** 2026-08-25  
**Scope:** middleware, simulator, Windows installation and release packaging, configuration, generated SECS/GEM tables, automated tests, dependencies, and every page/image in every PDF under `docs/`  
**Verdict:** **NOT READY for an uncontrolled production release.** The core data path and simulator are in good condition, but confirmed security, upgrade, installer, error-path, supply-chain, and commissioning blockers remain.

The working tree already contained modified and untracked implementation artifacts when this final review was consolidated. This audit added reports only; it did not overwrite or represent those pre-existing source changes as new remediation.

## Executive decision

Do not approve a general production rollout from the current branch. A controlled pilot is reasonable only after the P0/P1 items below are remediated, a Windows installer/upgrade rehearsal succeeds, and the installation-specific SECS/GEM questions are closed on representative equipment.

The strongest parts of the system are the journal/outbox design, generated protocol tables, simulator coverage, safe disabled-by-default production template, and the automated regression suite. The principal risk is not the normal happy path; it is the Windows trust boundary, deployment/upgrade behavior, and several failure and commissioning paths.

## Release gates

### P0 — must close before any production deployment

1. **Restrict the Windows trust boundary.** `deploy/install.ps1:171-229` grants all members of `BUILTIN\Users` recursive Modify permission to config, data, logs, archive, and machine-state directories, while `scripts/install_service.ps1:45-58` does not set an NSSM `ObjectName` and therefore relies on NSSM's LocalSystem default. The middleware hot-reloads configuration and consumes file-based control commands (`eap_middleware/service.py:1078-1103`, `eap_middleware/service.py:1181-1245`, `eap_middleware/control.py:111-145`). A standard local account can consequently alter equipment endpoints and tokens, issue control commands, or modify operational/audit data processed by a highly privileged service. Use a dedicated low-privilege service identity, service-only data ACLs, and a narrowly scoped operator group or authenticated control channel.

2. **Make the Setup EXE fail when its inner installation fails.** `packaging/installer/AstarMiddleware.iss:68-91` runs `install.ps1` during `ssPostInstall` and displays a message on failure, but does not assign a non-zero Setup exit code. Automation can record a successful installation even when Python, pip, manifest validation, or service setup failed.

3. **Make service installation idempotent for upgrades.** `scripts/install_service.ps1:1` promises “install or update”, but lines 45-46 always invoke `nssm install` and now correctly throw on non-zero status. An existing service—the ordinary upgrade case—therefore aborts before its application path and settings are updated. Detect an existing service, install only when absent, update settings in both paths, and verify the final service identity and configuration.

4. **Reconcile the release branch/history.** The audited local branch is 18 commits ahead of `origin/main`, `origin/main` has four independent commits, and Git reports no merge base. The remote does not contain this local commit/workflow history. The current source cannot enter a normal reviewed PR and has not received remote CI validation. Establish an authoritative history, review the complete import/diff, and run CI from the exact release commit.

### P1 — must close before broad production release

5. **Repair the legacy API failure path.** `eap_middleware/legacy_api.py:170-180` reads `OutboxItem.partition_key`, but the dataclass at `eap_middleware/outbox.py:22-28` has no such field. A forced transient failure reproduced `AttributeError` before `mark_failed()`; the attempts counter remained zero and the partition head stalled. The feature is disabled by default, but enabling it exposes a deterministic delivery-liveness fault.

6. **Replace known-vulnerable offline wheels.** The exact tracked deployment wheel set contains `aiohttp 3.14.1` (three advisories, fixed by 3.14.3) and `cryptography 49.0.0` (one advisory, fixed by 50.0.0). Current code does not import `aiohttp`, and the cryptography advisory concerns PKCS#7 decryption APIs not used by `secure_payload.py`, so observed reachability is low; known-vulnerable release artifacts should nevertheless not be shipped when compatible patched Windows wheels are available.

7. **Create a reproducible dependency definition.** `requirements.txt` mostly uses broad lower bounds, while the tracked `uv.lock` contains no packages and requires Python >=3.12 even though deployment and CI target Python 3.11. The offline wheel directory, rather than a valid reviewed lock, currently defines the real product. Pin and hash the release graph, regenerate it for Python 3.11, and audit it in CI.

8. **Fail closed for insecure HTTP configuration.** `eap_middleware/config.py:662-690` and `:826-869` accept `http://` and `verify_tls: false` with warnings only. MQTT already has an explicit insecure-mode gate. Add an equivalent test-only opt-in for HTTP, especially because the device token is placed in the request URL path.

9. **Bring the declared quality gates into CI.** Strict Pyright is configured but absent from CI and reports 97 errors across 54 production source files. The broader Ruff correctness subset (`F,E9`) is clean, but strict types are not. The 22-machine slow test is deselected by the default pytest configuration and therefore omitted by the workflow. CI also stages the middleware package but does not build and smoke-test the actual Inno Setup EXE, and it lacks dependency-vulnerability scanning.

### Commissioning gates — installation-specific evidence required

10. Confirm which SPTS events fire for the 14 per-module data-carrying recipe-step CEIDs (462-467, 470, 482-487, 490) versus generic CEIDs 857/858. Layouts exist, but the per-module events are not aliased/subscribed.
11. Supply and validate installation-specific PTIQ CEIDs before enabling PTIQ in production.
12. Confirm the inferred DaVinci `TimeFormat` mapping using the actual tool.
13. Run live HSMS/GEM qualification against representative MG, DaVinci, and SPTS equipment and the external Linkstuffs tenant. Four live external-I/O tests were intentionally not executed in this offline audit.
14. Decide and verify SPTS tool-side state/spooling behavior under disconnect, restart, and recovery.

## PDF and image review

All **14 PDFs, 1,160 pages total**, were text-extracted, rendered, reconciled by hash/page count, and visually inspected through page contact sheets. Dense or decisive pages were inspected at higher resolution. This included diagrams, tables, screenshots, dialog boxes, flowcharts, and scanned/image-only content—not only searchable text.

| Document | Pages |
|---|---:|
| Mac deployment guide | 11 |
| NexGen host communications documentation | 197 |
| Omega host communications documentation | 231 |
| DAVINCI maintenance manual | 52 |
| Preventive-maintenance checklist | 16 |
| DaVinci user manual | 61 |
| Safety manual | 11 |
| Recovery guide | 5 |
| Macrium Backup & Restore | 10 |
| Macrium Reflect guide | 7 |
| Service manual | 48 |
| Software Operation Manual | 204 |
| TC User Documentation | 151 |
| TheWizard documentation | 156 |
| **Total** | **1,160** |

The rendered Software Operation Manual page 127 confirms the pictured host-interface settings: enabled, Server mode, port 5000, T3/T5/T6/T7/T8 of 45/10/5/10/5 seconds, and Soft Start of 30 seconds.

Protocol-data reconciliation was strong: all 243 NexGen CEIDs matched, all 707 VIDs referenced by code were found among 760 parsed vendor VIDs, SPTS station types matched, documented timers matched, and generated tables were current. These checks establish documentary conformance, not hardware qualification.

## Verification performed

| Check | Result |
|---|---|
| Default automated suite | 669 passed, 1 skipped, 5 deselected in 147.74 s |
| 22-machine slow test, separately | 1 passed, 674 deselected in 98.84 s |
| Combined local result | **670 passed, 1 skipped**; four live external-I/O tests not run |
| Ruff correctness (`F,E9`) | Passed |
| Python byte compilation | Passed |
| `git diff --check` | Passed |
| Production config validation | Passed; four machine definitions, zero enabled |
| Simulator config validation | Passed; equipment/passive, `0.0.0.0:5051`, DaVinci profile |
| Middleware/simulator CLI startup/help | Passed |
| Generated-table regeneration | Passed; no output drift |
| Strict Pyright, production source only | Failed: 97 errors in 54 files |
| Offline wheel vulnerability audit | Failed: four advisories in two packages |
| Windows Setup EXE build/smoke test | Not possible locally; Inno Setup/PowerShell unavailable |

Generated-table totals were independently regenerated: MG 140 reports/243 events/37 bands; DaVinci full 282 events/208 reports/102 named DVs/14 bands and normal 54 events/45 reports/10 bands; SPTS full 225 events/58 reports/349 named DVs and normal 96 events/43 reports; ModuleVariables 13 families/880 offsets.

## Additional engineering observations

- A local ignored `config/production.local.yaml` contains a credential-like inline token and is readable by other local users (`0644`). It is not tracked (`.gitignore:27`). Treat it as a workstation secret: restrict it to `0600`, prefer an environment/secret reference, and rotate it if it has been shared. The token value was not copied into this report.
- Product and installer versions are hard-coded as `1.0.0`; artifacts do not embed the source commit/build identity. Code signing is not configured. Controlled offline distribution can compensate with a separately governed hash record, but traceability is weaker than a signed, uniquely versioned release.
- Bandit’s single “high” SHA-1 finding is not a cryptographic use: the digest is used for filename collision avoidance. SQL findings reviewed were literal/whitelisted statements. URL findings reinforce the insecure-HTTP gate described above.
- The legacy secure-payload HMAC does not authenticate the IV. Because this is an external wire contract and the feature is disabled by default, any migration must be coordinated with the peer; new deployments should prefer an authenticated-encryption format.

## Positive readiness evidence

- Journal/outbox persistence and recovery paths have substantial automated coverage.
- The full default suite completes in one process; the prior liveness/supervisor/packaging fixes in the dirty working tree are covered and green.
- Protocol profiles and generated tables show strong documentary conformance.
- The shipped production template is safe by default: no machines are enabled.
- Simulator configuration, CLI entry points, multi-machine load test, and profile generation pass locally.

## Required remediation sequence

1. Approve the Windows operator/service identity model and implement least-privilege ACLs.
2. Repair installer exit propagation and idempotent service upgrades; build and test the real Setup EXE on a clean Windows VM and over an existing installation.
3. Fix and test the legacy API failure path and insecure-HTTP gate.
4. Patch, lock, hash, and audit the Python 3.11 dependency graph.
5. Reconcile Git history; add Setup build/smoke, slow-test, dependency-audit, and staged type-check gates to CI.
6. Close vendor/tool commissioning questions and execute an acceptance matrix on real equipment.
7. Produce a uniquely versioned, signed or independently hash-attested release from the exact reviewed commit.

## Final acceptance criteria

Production approval requires: no open P0/P1 defects; clean install and in-place upgrade on supported Windows; service running as the approved limited identity; ACL and standard-user abuse tests passing; patched/audited wheels; CI from the exact release commit; real-tool communication, disconnect, retry, duplicate, restart, spool, and recovery tests; and documented operator rollback/recovery sign-off.

## Primary external references

- [NSSM command reference — `ObjectName` defaults to LocalSystem](https://www.nssm.cc/commands)
- [Microsoft guidance for selecting a service logon account](https://learn.microsoft.com/en-us/windows/win32/ad/guidelines-for-selecting-a-service-logon-account)
- [Inno Setup scripted event functions](https://jrsoftware.org/ishelp/topic_scriptevents.htm)
- [Inno Setup process exit codes](https://jrsoftware.org/ishelp/topic_setupexitcodes.htm)
- [OSV PYSEC-2026-3545 — aiohttp](https://osv.dev/vulnerability/PYSEC-2026-3545)
- [OSV PYSEC-2026-3552 — cryptography](https://osv.dev/vulnerability/PYSEC-2026-3552)
