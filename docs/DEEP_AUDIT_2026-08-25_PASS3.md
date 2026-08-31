# Deep Audit — 2026-08-25 (third pass)

Third code-level audit of the middleware and simulator, run after
`DEEP_AUDIT_2026-08-20.md`, `DEEP_AUDIT_2026-08-21.md` and
`DEEP_AUDIT_2026-08-21_PASS2.md`. Scope this time was code, not vendor
documents — the document-coverage question was closed in PASS2
(§0 there: all 14 PDFs, 1,160 pages, read as both text and rendered images).
No PDF in `docs/` has changed since (`find docs -iname '*.pdf' -newermt
"2026-08-19 15:24"` returns nothing), so this pass did not re-read them.

Two parallel reviews ran independently: `eap_middleware/` core, and
`gateway/gui/simulator/simulator_gui/packaging/scripts`. Both were briefed on
PASS2's "reviewed and deliberately not changed" list and HANDOVER's deferred
P2s, and told not to re-flag those without new information. Neither did.

Suite: **665 passed, 1 skipped, 5 deselected** before → same count after, plus
**4 new regression tests**, all independently confirmed to fail against the
pre-fix code and pass against the fix.
`ruff --select=F,E9` clean on `eap_middleware gateway gui simulator
simulator_gui` both before and after.

---

## 1. Findings, all fixed

### F-1 (MAJOR) Reconnect watchdog leaked a thread per tick against a connected-but-silent tool

`eap_middleware/service.py`, inside the reconnect watchdog's per-tick loop.
For a connected machine whose event subscription looks acked-but-silent, the
watchdog spawned a fresh `Liveness-<endpoint>` thread on **every** tick
(default interval 10s) with no check that a previous one was still
outstanding. Each thread's S1F3 round-trip can block up to T3 (30-45s across
shipped profiles), and the OFF-LINE trap that ships by default on SPTS,
DaVinci and PTIQ (`request_online: false`) sets `offline_alarmed`, not
`alarmed` — the only flag the old code checked — so the "don't spawn if
already alarmed" guard never engaged for exactly the tool state most likely
to sit connected-but-silent for a long time. Net effect: an OFF-LINE tool
staying connected accumulated roughly 250-300 live threads per hour,
unbounded over a multi-day outage.

**Fix.** Extracted the spawn decision into `_maybe_start_liveness_check()` and
added a `_liveness_inflight: set[str]`, mirroring the existing
`_reconnect_inflight` pattern used one code-path over. A thread is spawned
only if none is already outstanding for that endpoint; `_guarded_liveness`
discards the flag in its `finally` regardless of outcome.

Test: `tests/test_event_liveness.py::test_liveness_check_does_not_spawn_a_second_thread_while_one_is_outstanding`
(ticks the guard 5 times against a session whose `request_svids` blocks on a
`threading.Event`, asserts exactly one `Liveness-*` thread exists throughout).
Confirmed failing pre-fix (`AttributeError` — the method didn't exist —
and, once inlined-vs-extracted is accounted for, the underlying spawn-guard
condition was absent).

### F-2 (MAJOR) An uncaught exception silently killed the ConfigurationSupervisor thread

`eap_middleware/service.py`, the `ConfigurationSupervisor` loop. Every other
I/O call in that loop body (`_replay_journal()`, `journal.purge_old()`,
`_write_status()`) is wrapped in try/except with a comment explaining why —
`consume_commands(self._control_data_dir())` was the one exception. Its
`finally: path.unlink()` (in `control.py`) only swallows `FileNotFoundError`;
any other `OSError` — a Windows AV/backup/indexer transiently holding a
command file, a real condition on the documented Windows Server deployment
target — propagated out of the loop and killed the daemon thread with no log
line (`threading.excepthook`'s default stderr write goes nowhere under a
Windows service). That thread also owns hot config reload, GUI command
processing, journal replay and journal purge, so a single transient lock
silently stopped all four; only a stale `runtime_status.json` would ever hint
at it.

**Fix.** Wrapped the `consume_commands`/`_process_command` block in the same
try/except-and-log pattern as its three neighbours.

Test:
`tests/test_unified_control.py::test_supervisor_survives_a_command_inbox_read_failure`
(monkeypatches `consume_commands` to raise `PermissionError` on its first
call, then asserts the supervisor thread is still alive **and** that a config
revision applied after the failing tick is still picked up — proving it kept
doing its job, not just that it didn't crash). Confirmed failing pre-fix with
`PytestUnhandledThreadExceptionWarning: Exception in thread
ConfigurationSupervisor`.

### F-3 (MAJOR) The Windows service installer had no error checking on any NSSM call

`scripts/install_service.ps1` is the actual production path to a 24/7
service — `deploy/install.ps1` doesn't call NSSM itself, it tells the
operator to run this script afterward. All six external `nssm` invocations
ran with no `$LASTEXITCODE` check, unlike every other build/install script in
the repo (`packaging/installer/build_installer.ps1`, both
`packaging/*/build_windows.ps1`), which check after every external call.
`$ErrorActionPreference = "Stop"` does not cover this: PowerShell does not
treat a non-zero exit code from a native executable as a terminating error.
Concrete scenario: re-running the script after a service-name collision (a
normal upgrade/reinstall case) makes `nssm install` fail, so the five
following `nssm set` calls fail too (the service doesn't exist), yet the
script still printed "Service installed." — an operator could walk away
believing a monitored service was running when nothing was installed.

**Fix.** Added `if ($LASTEXITCODE -ne 0) { throw ... }` after each of the six
NSSM calls, matching the pattern already used elsewhere in the repo.

No PowerShell test runner exists in this repo (pytest only); the fix follows
an existing, working pattern verbatim rather than introducing a new one.

### F-4 (MAJOR) The offline installer's integrity manifest didn't cover what it's documented to cover

`packaging/installer/build_installer.ps1` and `scripts/build_deploy_package.sh`
both built `RELEASE_MANIFEST.sha256` from only five bootstrap files
(`SETUP.bat`, `Setup.ps1`, `install.ps1`, `PYTHON_VERSION.txt`, the Python
installer `.exe`). `deploy/install.ps1`'s `Assert-ReleaseManifest` verifies
exactly and only what the manifest lists, plus a `count >= 2` sanity check —
it is generic and correct for whatever it's given. What it was given never
included `wheels/` (20+ third-party dependencies, `pip install --no-index`'d
with no per-wheel check of its own) or `source/` (the entire application:
`eap_middleware`, `gateway`, `gui`, `simulator`, `config` — copied verbatim
into the running app). `DEEP_AUDIT_2026-08-21_PASS2.md` cites this manifest
as proof "everything is verified before anything executes"; the mechanism is
sound, the coverage was narrower than that claim. In this project's own
documented air-gapped USB/network-share distribution model, a bit-corrupted
or substituted wheel or source file installed and ran with zero integrity
check.

**Fix.** Both build scripts now walk `wheels/` and `source/` recursively and
add every file to the manifest, alongside the original five entries. Verified
`Assert-ReleaseManifest`'s parsing contract (hex hash, optional `*` prefix,
forward-slashed relative path, path-escape check) needed no change — it
already handled an arbitrary-length manifest correctly.

Tests:
`tests/test_deploy_packaging_security.py::test_bash_manifest_covers_every_wheel_and_source_file`,
`::test_powershell_manifest_covers_every_wheel_and_source_file`. Both
confirmed failing against the pre-fix scripts.

---

## 2. Also handled: one uncommitted dead-code change, reverted

`eap_middleware/models.py`'s `timestamp_ms()` carried an uncommitted 2-line
guard (`if value.tzinfo is None: value = value.astimezone()`). Verified in a
Python REPL that `naive_dt.timestamp() == naive_dt.astimezone().timestamp()`
always holds, and the reassignment never escapes the function — a functional
no-op. The real fix for the naive/aware datetime landmine this was gesturing
at (`DEEP_AUDIT_2026-08-20.md` D-9) already shipped via `mapper._aware()`,
which makes `CanonicalEvent.timestamp` uniformly aware before it ever reaches
`timestamp_ms()`. Reverted rather than left in, since dead code here would
mislead a future reader into thinking this function defends against naive
input.

The untracked `docs/vendor/nexgen_secs_extracted.txt`,
`omega_secs_extracted.txt` and `nexgen_images/` were checked and are shallow
`pdftotext`-only leftovers from the PASS2 document pass — the same method
PASS2 itself says is insufficient alone. No new information; left in place
(gitignored scratch, not code).

---

## 3. Everything else reviewed, nothing new

**eap_middleware core** (fresh read): `mapper.py`, `profiles.py` (structural
code; data tables trusted to PASS2), `config.py`, `journal.py`, `csv_store.py`,
`outbox.py`, `linkstuffs.py`, `linkstuffs_http.py`, `secure_payload.py`,
`job_tracker.py`, `probe.py`, `logging_setup.py`, `legacy_api.py`,
`secs_runtime.py`, `alarms.py`, `control.py`, `single_instance.py`,
`netinfo.py`, `spts_module_vids.py`, `svid_admin.py`, `cli.py`. The durability
spine — journal → outbox, symmetric dispatch-lock re-derivation on live vs.
replay paths, `holds()`-guarded CSV eviction, atomic writes with fsync,
SQLite connection hygiene — held up under scrutiny.

**gateway/gui/simulator/simulator_gui/packaging/scripts** (fresh read): no
new races, leaks or silent failures in the runtime hot paths
(`gateway/host.py`, `event_subscription.py`, the simulator engine, GUI
service lifecycle). One cosmetic finding, not a data-loss risk: `gui/app.py`
force-destroys the window 20s after closing it while running the service
in-process, regardless of whether the daemon-thread `stop()` finished —
traced the durability chain and confirmed anything unflushed replays on next
start, so this is UI-framing only, not fixed here.

Independently re-judged the two HANDOVER "deferred P2, low value" items in
scope: the simulator_gui Stop button's missing timeout, and the non-atomic
subscription generator scripts. Both confirmed still low-value for the
reasons already stated (bounded window-close escape hatch; offline
developer-run tools that fail loudly, not silently) — not re-flagged as
something-more.

Reconfirmed, not a new finding: the full test suite still cannot run as one
pytest process (secsgem 0.3.0 module-level thread-safety issue, accepted in
prior audits) — every file passes individually.

---

## 4. Verdict

**Production-ready for controlled rollout**, with the four MAJOR findings
above now fixed and regression-tested. None were data-integrity defects in
the SECS/GEM path itself — three prior passes' work on that path holds. All
four were operational-longevity or deployment-integrity gaps specific to this
project's actual deployment shape (24/7 unattended fab hosts, air-gapped
USB/network-share installs, Windows Server as the target OS) — the kind of
gap that only shows up when you ask "what happens after go-live," not "does
this event parse correctly."
