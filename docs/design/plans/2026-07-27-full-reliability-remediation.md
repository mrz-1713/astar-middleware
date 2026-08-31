# Full Reliability Remediation Implementation Plan

1. Add configuration and transport regression tests, then gate disabled MQTT,
   validate per-machine HTTPS routing, classify retryable HTTP responses, and
   isolate live tests behind an opt-in marker.
2. Add persistence and acknowledgment regression tests, then retain CSV buffers
   through failed writes, propagate callbacks failures to SECS acknowledgments,
   make alarm limiting configurable/state-aware, and make instance locking
   atomic.
3. Correct E40 reply metadata and empty confirmations, preserve alert outcomes,
   recover middleware-owned S2F33 collisions, request all SVs correctly, and
   preserve multiple S6F11 reports.
4. Replace last-arrival job attribution with evidence-based wafer/lot/job maps
   and expand aligned E90 lists into per-substrate canonical events.
5. Correct simulator query/identity/E90 behavior, generated DaVinci YAML, and
   HTTPS-first diagnostics.
6. Update affected documentation and existing assertions.
7. Run all non-live tests, focused regression groups, configuration probes,
   available static checks, and compileall for every Python package.

Hardcoded credentials are intentionally out of scope by user instruction.
