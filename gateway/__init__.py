"""SECS/GEM host (active HSMS) layer used by eap_middleware.secs_runtime.

This package was once a standalone prototype; everything except the live
HSMS host + event-subscription helpers has been removed. The production
middleware lives in `eap_middleware/`.
"""

from .host import GatewayHost, create_host_settings
from .event_subscription import (
    EventSubscriptionManager,
    SubscriptionConfig,
)

__all__ = [
    "GatewayHost",
    "create_host_settings",
    "EventSubscriptionManager",
    "SubscriptionConfig",
]
