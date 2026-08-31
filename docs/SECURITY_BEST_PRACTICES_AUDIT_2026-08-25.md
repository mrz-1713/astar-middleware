# Security Best-Practices Audit — ASTAR Middleware and Simulator

**Date:** 2026-08-25  
**Status:** Remediation required before production approval  
**Scope:** application trust boundaries, configuration/secrets, persistence/control paths, Windows service/installer, transport security, dependencies, release pipeline, and simulator exposure.

## Critical severity

No independently exploitable remote critical vulnerability was confirmed in the reviewed default configuration.

## High severity

### SEC-001 — Standard Windows users can control data consumed by a LocalSystem service

**Evidence:** `deploy/install.ps1:171-229` grants `BUILTIN\Users` Modify permission recursively over logs, data, archive, machines, and application config. `scripts/install_service.ps1:45-58` installs via NSSM without an `ObjectName`; NSSM's documented default is LocalSystem. The service hot-reloads its writable configuration and consumes file-based commands in `eap_middleware/service.py:1078-1103` and `:1181-1245`; command files are created/consumed by `eap_middleware/control.py:111-145`.

**Impact:** Any ordinary local user can cross the operator/service boundary: redirect equipment or upstream traffic, change device credentials/configuration, request service actions, corrupt or erase queues/history, or falsify operational evidence. The affected process runs with extensive local privileges.

**Recommendation:** Run under a dedicated least-privilege identity; make executable code, configuration, queues, journal, and audit data writable only by administrators and that service identity; create an explicit `ASTAR Operators` group for the minimum required control actions; and replace or authenticate the shared writable command inbox. Verify effective ACLs and service identity during installation.

### SEC-002 — Known-vulnerable packages are embedded in the offline release

**Evidence:** Auditing the exact wheel filenames found `aiohttp 3.14.1` affected by PYSEC-2026-3545/3546/3547 and `cryptography 49.0.0` affected by PYSEC-2026-3552. Compatible patched Windows CPython 3.11 wheels for `aiohttp 3.14.3` and `cryptography 50.0.0` were confirmed available, and a temporary updated subset audited clean.

**Impact:** The shipped artifact contains publicly known vulnerable code. Current reachability appears limited: production source does not import `aiohttp`, and the cryptography advisory requires PKCS#7 EnvelopedData decryption APIs not used by `secure_payload.py`. Future dependency use or code changes could make that assumption false.

**Recommendation:** Remove unused `aiohttp` if possible; otherwise update to 3.14.3 or later. Update cryptography to 50.0.0 or later. Rebuild the full wheelhouse from a Python 3.11 lock with hashes, re-run tests, generate an SBOM, and make vulnerability audit a release gate.

## Medium severity

### SEC-003 — Insecure upstream HTTP can be enabled without an explicit test-only override

**Evidence:** `eap_middleware/config.py:662-690` and `:826-869` permit `http://` and `verify_tls: false`, emitting warnings rather than rejecting production configuration. MQTT already uses an explicit insecure-mode gate. The device token is included in the URL path.

**Impact:** Misconfiguration can expose credentials and production payloads to observation or manipulation and can leak tokens through URL-aware infrastructure or logs.

**Recommendation:** Require HTTPS with certificate verification by default. Introduce a conspicuous `allow_insecure` switch restricted to simulator/test configurations, validate URL schemes, redact tokens from all logging/exception paths, and add negative configuration tests.

### SEC-004 — Setup can report success after the privileged inner install fails

**Evidence:** `packaging/installer/AstarMiddleware.iss:68-91` catches a non-zero `install.ps1` result during `ssPostInstall` but only displays a message. It does not return a non-zero Setup process exit code.

**Impact:** Deployment automation and operators can accept a partial or insecure installation as successful, leaving files without a functioning or correctly configured service.

**Recommendation:** Store failure state and return a non-zero custom Setup exit code; add rollback/cleanup behavior where safe; and test corrupt manifest, pip failure, existing-service upgrade, and service-start failure cases using the compiled Setup EXE.

### SEC-005 — Dependency resolution and release provenance are not reproducible

**Evidence:** `requirements.txt` mostly contains lower bounds; `uv.lock` contains no resolved packages and declares Python >=3.12 while production targets 3.11. Offline wheels currently determine the actual dependency graph. Version strings are broadly hard-coded to `1.0.0`, and artifacts lack source-commit identity and configured code signing.

**Impact:** Two nominally identical builds can contain different dependencies, review cannot reliably connect an installed binary to source, and a compromised or mistaken artifact is harder to detect.

**Recommendation:** Use a complete Python 3.11 lock with hashes; verify every offline file against a generated release manifest; create an SBOM; embed semantic version plus commit/build ID; and sign Windows artifacts or operate a formally controlled independent hash-attestation process.

### SEC-006 — Local ignored production secret has weak filesystem permissions

**Evidence:** The ignored `config/production.local.yaml` is not tracked but contains a credential-like inline token and has mode `0644`. `.gitignore:27` excludes it. The secret value was not recorded during this audit.

**Impact:** Other accounts on the development machine may read the token; copying or backing up the file can spread it outside the intended secret boundary.

**Recommendation:** Change the file to owner-only permissions (`0600`), prefer an environment or managed-secret reference, scan controlled history/artifacts for accidental copies, and rotate the token if exposure cannot be excluded.

### SEC-007 — Service update path is not idempotent

**Evidence:** `scripts/install_service.ps1:1` describes install-or-update, but `:45-46` always executes `nssm install` and aborts when the service already exists.

**Impact:** A normal upgrade can fail partway through, leaving old service arguments/identity with new files or encouraging unsafe manual workarounds.

**Recommendation:** Detect existence, update atomically, preserve/validate intended credentials, stop/restart with bounded rollback, and assert all NSSM settings after installation.

## Low severity

### SEC-008 — Legacy secure-payload authentication omits the IV

**Evidence:** The legacy `secure_payload` construction authenticates ciphertext but not the IV. The feature is disabled by default and is governed by an external peer protocol.

**Impact:** In CTR-like encryption, IV manipulation can produce controlled changes in the first plaintext block even when ciphertext authentication succeeds. Exploitation depends on the peer contract and feature enablement.

**Recommendation:** For new integrations, use a standard AEAD construction and authenticate version/context metadata. For existing peers, design a versioned, dual-read migration rather than changing the wire format unilaterally.

### SEC-009 — Simulator defaults bind to all interfaces

**Evidence:** The validated simulator configuration listens on `0.0.0.0:5051` in passive equipment mode.

**Impact:** On an untrusted or incorrectly firewalled network, unintended hosts may connect to the simulator. This is primarily an environment hardening concern.

**Recommendation:** Default local development to loopback, require an explicit opt-in for all-interface binding, and document firewall/network-segment requirements for lab use.

## Informational and reviewed scanner findings

- Bandit’s reported SHA-1 use is for filename collision avoidance, not password storage, signatures, or integrity enforcement; it is not treated as a security vulnerability.
- Reviewed SQL construction uses fixed or whitelisted identifiers where binding is unavailable; no user-controlled SQL injection path was confirmed.
- The production template enables zero machines and uses HTTPS verification, reducing accidental network activity in a fresh installation.
- Four live tests requiring external tenant I/O were not run. Their absence is an acceptance-test gap, not evidence of a vulnerability.

## Security release checklist

- [ ] Approve a least-privilege service/operator identity model.
- [ ] Remove `BUILTIN\Users` write access from service-trusted configuration, command, queue, and audit locations.
- [ ] Verify service identity and effective ACLs in clean-install and upgrade tests.
- [ ] Patch and lock the dependency wheelhouse; attach audit and SBOM results.
- [ ] Reject insecure HTTP/TLS settings unless a test-only override is explicit.
- [ ] Make Setup and upgrade failures machine-detectable and recoverable.
- [ ] Rotate/restrict local secrets and confirm no secret exists in release artifacts/history.
- [ ] Reconcile repository history and run security/CI gates against the exact release commit.
- [ ] Complete real-equipment and external-tenant security/availability acceptance tests.

Production sign-off should remain blocked until SEC-001 and SEC-002 are closed and all medium findings are either remediated or explicitly accepted by the accountable system owner with compensating controls.

## Primary external references

- [NSSM command reference](https://www.nssm.cc/commands)
- [Microsoft service-logon account guidance](https://learn.microsoft.com/en-us/windows/win32/ad/guidelines-for-selecting-a-service-logon-account)
- [Microsoft LocalSystem account reference](https://learn.microsoft.com/en-us/windows/win32/services/localsystem-account)
- [Inno Setup scripted event functions](https://jrsoftware.org/ishelp/topic_scriptevents.htm)
- [Inno Setup process exit codes](https://jrsoftware.org/ishelp/topic_setupexitcodes.htm)
- [OSV PYSEC-2026-3545](https://osv.dev/vulnerability/PYSEC-2026-3545), [PYSEC-2026-3546](https://osv.dev/vulnerability/PYSEC-2026-3546), and [PYSEC-2026-3547](https://osv.dev/vulnerability/PYSEC-2026-3547) — aiohttp advisories
- [OSV PYSEC-2026-3552](https://osv.dev/vulnerability/PYSEC-2026-3552) — cryptography advisory
