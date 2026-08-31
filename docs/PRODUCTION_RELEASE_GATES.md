# Production Release Gates

Repository tests establish a release candidate; they do not approve a fab
deployment. Production approval is fail-closed and applies only to the exact
clean commit and signed artifact recorded by `scripts/release_evidence.py`.

## Required retained evidence

- Clean source commit, CI run URLs/results, dependency-lock hashes, SBOM,
  separately published artifact SHA-256, and a valid Authenticode signature.
- Production-like HTTPS and/or MQTT tenant tests covering authentication
  failure, throttling, outage, queue growth, recovery, and duplicate delivery.
- A completed qualification record for each DaVinci, SPTS, PTIQ, and NexGen
  model/software revision in scope. Store immutable raw HSMS fixtures under
  `tests/fixtures/commissioned/<vendor>/<model>/<software-revision>/`.
- Windows service install, virtual identity/ACL, process-kill recovery,
  upgrade, rollback, reboot/boot-start, power-loss, low/full-disk, corruption,
  backup, and restore drill evidence.
- Equipment-owner and OEM approval for the safe action used when storage enters
  critical backpressure and for every service/safety account or setting change.

Never mark a gate complete with simulator output standing in for physical
interlocks, OEM control behavior, tenant acceptance, reboot, or power loss.

## Commissioning matrix

For every physical tool record: serial/model/software, observed HSMS role,
address/port/session ID, T3/T5/T6/T7/T8, actual SVID/CEID tables, accepted and
rejected subscription bands, alarm set/clear, remote-command HCACK and control/
process state, reconnect/restart, a complete lot and CSV, tenant telemetry,
upstream outage/recovery, duplicate behavior, and owner/OEM sign-off.

The shipped PTIQ IDs and NexGen settings are documentation-derived starting
points. They are not production evidence until this matrix is complete.
