"""Production service orchestration for 22-machine middleware.

Formerly a single 2,455-line ``service.py``.  It is now a package split by
concern - see ``state.py`` for the shared state and the contract between the
concern mixins - but every name the old module exported is re-exported here,
so ``from eap_middleware.service import X`` keeps working unchanged.
"""

from .constants import (
    MAX_DISPATCH_ATTEMPTS,
    MIRROR_POLL_INTERVAL_SEC,
    SIMULATOR_MISSING_HINT,
    STOP_TIMEOUT_SEC,
    logger,
)
from .core import EapMiddlewareService
from .errors import SimulatorUnavailableError, StaleSessionError
from .helpers import (
    event_liveness_decision,
    machine_http_outbox_path,
    reconnect_delay,
    resolve_data_path,
)

__all__ = [
    "EapMiddlewareService",
    "MAX_DISPATCH_ATTEMPTS",
    "MIRROR_POLL_INTERVAL_SEC",
    "SIMULATOR_MISSING_HINT",
    "STOP_TIMEOUT_SEC",
    "SimulatorUnavailableError",
    "StaleSessionError",
    "event_liveness_decision",
    "logger",
    "machine_http_outbox_path",
    "reconnect_delay",
    "resolve_data_path",
]
