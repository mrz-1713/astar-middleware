# Audit Remediation and AES-256 Design

**Date:** 2026-08-25

**Status:** Approved for implementation

**Source audits:** `docs/DEEP_AUDIT_2026-08-25_FINAL.md` and
`docs/SECURITY_BEST_PRACTICES_AUDIT_2026-08-25.md`

## Objective

Close the locally actionable P0/P1 and security findings from the final audits,
including a safe migration from the unauthenticated-IV legacy encryption
format to authenticated AES-256 encryption. Preserve existing uncommitted work
and do not claim that source changes complete acceptance activities requiring a
Windows test environment, signing credentials, repository administration, an
external tenant, or real equipment.

## Scope

The implementation covers:

- Windows service identity, directory ACLs, and operator boundaries;
- Setup failure propagation and idempotent service upgrades;
- the legacy API outbox failure path;
- authenticated AES-256 payload encryption with an explicit legacy mode;
- fail-closed HTTP/TLS configuration;
- dependency patching, locking, hashing, auditing, and SBOM generation;
- CI enforcement for types, dependency audit, the slow scale test, and the
  compiled Setup artifact;
- safe simulator network defaults;
- local production-secret permissions; and
- focused documentation and regression tests for each change.

The following remain external release gates and will be documented rather than
reported as completed: authoritative Git-history reconciliation, code signing,
clean-install and in-place-upgrade rehearsal on supported Windows, external
tenant tests, vendor commissioning decisions, and live equipment qualification.

## Encryption Decision

### Considered approaches

1. Replace the existing AES-256-CTR format in place with AES-256-GCM. This is
   cryptographically clean but silently breaks the existing n8n/PHP peer.
2. Continue AES-256-CTR and change its HMAC input to `IV || ciphertext`. This
   fixes the confirmed IV weakness but still changes the unversioned wire
   contract and maintains a bespoke construction.
3. Add a versioned AES-256-GCM format and retain the exact current construction
   as an explicit compatibility mode. This is the selected approach because it
   provides standard authenticated encryption and makes peer migration
   deliberate and observable.

### Version 2 format

The new mode is named `aes_256_gcm_v2` and uses:

- a base64-encoded key that must decode to exactly 32 bytes;
- a fresh 12-byte random nonce for every encryption operation;
- `AESGCM` from `cryptography`;
- the ASCII protocol marker `astar-legacy-api:aes-256-gcm:v2` as associated
  authenticated data; and
- the serialized value `v2.` followed by base64 of `nonce || ciphertext || tag`.

The HTTP request shape remains `{"data": "<versioned value>"}`. JSON is encoded
as compact UTF-8. Decryption rejects an unknown version, malformed base64,
truncated input, an incorrect key, modified nonce/ciphertext/tag, invalid UTF-8,
and non-object JSON with one non-secret-bearing `SecurePayloadError` family.

### Legacy format

The current `base64(IV[16] || HMAC-SHA3-512(ciphertext) || ciphertext)` format
remains byte-for-byte compatible under the explicit name `legacy_ctr_v1`.
Existing raw passphrase normalization and two-key behavior remain confined to
that mode. The implementation does not present v1 as secure for new
integrations and does not alter its peer contract unilaterally.

When an encrypted legacy API is enabled, configuration must explicitly select
an encryption mode. The public production template and newly created GUI
configuration use `aes_256_gcm_v2`. Version 2 requires its dedicated 32-byte
base64 key. Version 1 requires the existing first/second raw or base64 key pair.
Disabled legacy integrations remain valid without keys or a migration choice.

## Windows Trust Boundary

The middleware service runs as the Windows virtual service account
`NT SERVICE\AstarSecsGemEapMiddleware`, not LocalSystem. Installation creates
or reuses a local `ASTAR Operators` group. It never grants recursive Modify
permission to `BUILTIN\Users`.

ACL responsibilities are separated:

- administrators retain Full Control over the installation;
- the virtual service account receives the access required to read application
  code/configuration and modify runtime logs, archives, queues, journals,
  machine state, and its command paths;
- `ASTAR Operators` may read operational logs and modify only the configuration
  and control locations needed by the existing control panel;
- ordinary authenticated users receive no write access to service-trusted
  configuration, commands, queues, journals, archives, or audit data; and
- inherited broad permissions are removed or protected before the narrower
  grants are applied.

The installer verifies the effective service account and key ACL entries after
configuration. A failure to establish the intended boundary is fatal rather
than a warning.

## Installer and Upgrade Behavior

`install_service.ps1` detects whether the NSSM service exists. It installs only
when absent; both clean-install and update paths apply the complete application,
arguments, working directory, log rotation, startup type, and `ObjectName`
configuration. It stops an existing service when necessary, performs bounded
updates, verifies the final NSSM values, and starts or restores it only after a
valid configuration is established. Every native-command failure is checked.

The Inno Setup script records an inner-install failure and returns a documented
non-zero custom Setup exit code. The UI still explains the failure, while
unattended automation can reliably reject the install. Tests cover manifest,
pip, existing-service, settings-verification, and service-start failure paths
at source level; the compiled installer is built and smoke-tested on Windows CI.

## Application Failure and Transport Controls

`OutboxItem` includes `partition_key`, and every query constructing an item
selects and populates it. Tests force transient and permanent legacy API
failures and prove attempts advance, dead-letter policy works, and later
partitions are not blocked by an attribute error.

Global and per-machine HTTP routes require HTTPS and certificate verification
by default. Plain HTTP or `verify_tls: false` is accepted only when an explicit
`allow_insecure: true` flag is present. The flag is described as test/lab-only,
is false in production templates and GUI defaults, and produces a prominent
startup warning. Validation and request errors never include device-token URL
segments or secret values.

## Dependencies and Provenance

Unused `aiohttp` is removed if import and packaging checks confirm it has no
runtime consumer. `cryptography` is raised to at least the audited fixed
version. The release dependency graph targets Python 3.11, pins exact versions,
and records hashes in a reviewed lock/requirements artifact. The offline
wheelhouse is rebuilt from that graph and every wheel and source file remains
covered by the release SHA-256 manifest.

CI installs from the locked graph, audits the resolved environment, emits an
SBOM artifact, and fails on known vulnerabilities. Release output embeds the
semantic version plus source commit/build identity where the existing build
interfaces permit it. Signing remains an external credential-dependent gate;
unsigned controlled releases require an independently governed hash record.

## CI and Simulator Hardening

CI runs the configured strict Pyright check over production source and tests.
Existing type errors in the enforced scope are corrected rather than adding a
permissive baseline. The 22-machine slow test runs in a dedicated job, and a
Windows job builds the actual Inno Setup EXE and exercises install failure and
upgrade smoke scenarios where runner capabilities permit.

The simulator defaults to `127.0.0.1`. Binding to `0.0.0.0` or another
non-loopback address requires an explicit exposure option and logs the lab
network/firewall warning. Existing deliberate lab configurations can opt in.

The ignored local `config/production.local.yaml` is changed to owner-only
permissions on the development host. Its values are never printed, copied into
the design, added to Git, or inspected beyond what is necessary to verify mode.
Rotation remains an operator action if prior exposure cannot be excluded.

## Error Handling and Compatibility

Configuration errors identify the field and corrective action without
revealing tokens or keys. Installer and service-update failures identify the
failed stage and exit code. Encryption authentication failures do not reveal
whether the nonce, ciphertext, tag, or key was wrong. All security-relevant
defaults fail closed.

Compatibility is explicit rather than inferred: v1 and v2 have separate mode
names, validation, keys, and test vectors. No automatic downgrade is allowed.
The publisher emits only its configured version. Codec dual-read support exists
for migration tooling and tests, but receiving an unknown or unconfigured
version never falls back to v1.

## Verification

Implementation is complete only after:

- focused encryption, configuration, outbox, installer-text, ACL, service
  upgrade, simulator, packaging, and CI-configuration tests pass;
- known-answer v1 compatibility and v2 AES-GCM round-trip/tamper tests pass;
- the default pytest suite passes;
- the slow 22-machine test passes separately;
- Ruff correctness, strict Pyright, Python byte compilation, and
  `git diff --check` pass;
- dependency lock/hash consistency and vulnerability audit pass; and
- production and simulator configuration validation pass.

Windows Setup execution, service-account effective-access checks, code signing,
external I/O, and real-equipment qualification are reported as pending unless
they actually run in an appropriate environment with retained evidence.

## Change Discipline

The working tree contains pre-existing modified and untracked artifacts. The
implementation will inspect and extend overlapping changes, never discard or
rewrite them wholesale, and will keep unrelated edits out of remediation
commits. Any step requiring repository-history replacement, credential use, or
external production changes requires separate user authority.
