"""Tunables and module-wide constants for the service."""


from __future__ import annotations


import logging


# Hard-coded rather than __name__: the package split must not rename the
# logger that operators already filter production logs on.
logger = logging.getLogger("eap_middleware.service")


SIMULATOR_MISSING_HINT = (
    "runtime_mode is 'simulated' but the simulator package is not installed. "
    "The simulator ships separately as SecsGemSimulator; run it on the "
    "equipment side and set this machine to runtime_mode: 'real' with the "
    "simulator's host and port."
)


# A journal entry whose dispatch keeps failing is eventually parked rather than
# retried forever: past this many attempts the fault is ours (a mapper or
# publisher defect), not a transient one, and an endless retry loop would hold
# up every later event for that machine. The payload stays readable either way.
MAX_DISPATCH_ATTEMPTS = 10


# Total wall-clock budget for the *joins* in stop(), shared across every
# machine and every background worker rather than spent per thread.
#
# It used to be a fixed 10s timeout on each of ~7 joins per machine, all
# sequential, so a four-machine service needed 209s to stop even when nothing
# was wrong - and the control panel re-enables "Run service here" only in the
# `finally` after stop() returns, so the button sat dead for three and a half
# minutes. The joins that actually expire are the ones whose worker is blocked
# in network I/O (an SVID poll waits up to T3=45s; an HTTP publish is
# timeout_sec x retry_count), which on a busy service is most of them.
#
# What this budget does NOT cover, because these are the guarantees the stop
# exists to provide and truncating them would lose data or strand the tool:
#   - closing each host's socket (GatewayHost.retire() force-closes it),
#   - flushing open lot buffers to local CSV,
#   - releasing the single-instance lockfile.
# Only the waiting-for-threads-to-notice part is bounded.
STOP_TIMEOUT_SEC = 20.0


# How often the mirror worker looks for network copies that have come due.
# The per-task backoff lives in the queue itself (journal.fail_mirror), so
# this only bounds how quickly a freshly-due task is noticed.
MIRROR_POLL_INTERVAL_SEC = 5.0


# Minimum gap between full CSV-sink failure tracebacks for one machine. Shorter
# failures within the window are counted and reported once the window lapses,
# so a persistently broken sink logs one traceback plus a count instead of one
# traceback per collection event.
CSV_FAIL_LOG_INTERVAL_SEC = 60.0
