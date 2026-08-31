"""The service class itself: state plus every concern mixin."""

from __future__ import annotations

from .lifecycle import LifecycleMixin
from .simulator_runtime import SimulatorMixin
from .http_outbox import HttpOutboxMixin
from .control_plane import ControlMixin
from .wiring import WiringMixin
from .dispatch import DispatchMixin
from .alarm_flow import AlarmsMixin
from .health import HealthMixin


class EapMiddlewareService(
    LifecycleMixin,
    SimulatorMixin,
    HttpOutboxMixin,
    ControlMixin,
    WiringMixin,
    DispatchMixin,
    AlarmsMixin,
    HealthMixin,
):
    """Production service orchestration for 22-machine middleware.

    Behaviour lives in the mixins; see ``state.py`` for the shared state and
    the cross-concern contract.
    """
