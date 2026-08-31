# Storage Capacity, Backup, and Recovery

The default site objective is **RPO 15 minutes** for configuration/release
metadata and **RPO 0 for already acknowledged SECS ingress**, with an **RTO of
4 hours**. These are commissioning inputs, not universal promises; the site
owner must approve replacements based on event rate and equipment recovery.

Back up off-host, encrypted, and access-controlled copies of configuration and
secrets, `data/` journals/outboxes, `machines/` CSV/admin state, release
identity/evidence, and installer metadata. Logs may use a separate retention
policy. Never take a file-copy backup of active SQLite files without a
SQLite-aware snapshot or a stopped/quiesced service.

## Capacity worksheet

Measure the 95th and worst-case bytes per accepted event, events per second,
CSV/log amplification, and SQLite WAL high-water mark. Required usable reserve
is at least:

`worst bytes/event × peak events/sec × maximum outage sec × amplification`

Add the measured space needed to quiesce, repair, checkpoint, and export
diagnostics. Configure `critical_free_bytes` above that total; warning and
recovery must be higher. A reserve smaller than the measured shutdown/repair
need is prohibited.

At warning, diagnose the named filesystem and queue depths. At critical, the
middleware stops durable acceptance and quiesces SECS sessions. Follow the
tool/profile-specific safe action approved in commissioning; do not assume the
equipment spools. Recovery is automatic only after hysteresis and SQLite
integrity checks pass.

## Restore drill

Restore to an isolated host with equipment VLAN access disabled. Run:

`python -m scripts.verify_restore --config <restored-production.yaml> --csv-root <restored-csv-dir> --report restore-report.json`

The verifier checks configuration, SQLite integrity/schema/foreign-key state,
CSV readability, release identity when supplied, and offline service
construction. After it passes, reconcile journal/outbox/CSV counts, enable one
supervised simulator path, then proceed through the approved physical-tool
reconnection plan. Retain the report, elapsed RTO, achieved RPO, exceptions,
and approver. Exercise application-data restore quarterly and bare-system
restore at least annually or after material installer/storage changes.
