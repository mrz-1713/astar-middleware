"""Linkstuffs MQTT Gateway publisher and payload builders."""

from __future__ import annotations

import json
import logging
import threading
import time
import hashlib
from typing import Any, Dict, Optional

from .models import CanonicalEvent, MachineConfig, LinkstuffsConfig
from .outbox import SQLiteOutbox
from .profiles import MachineProfile

logger = logging.getLogger(__name__)

LINKSTUFFS_TOPIC_CONNECT = "v1/gateway/connect"
LINKSTUFFS_TOPIC_DISCONNECT = "v1/gateway/disconnect"
LINKSTUFFS_TOPIC_TELEMETRY = "v1/gateway/telemetry"
LINKSTUFFS_TOPIC_ATTRIBUTES = "v1/gateway/attributes"


class LinkstuffsGatewayPublisher:
    """Publishes canonical events to the Linkstuffs MQTT gateway.

    Writes go through the durable outbox, so a broker outage delays delivery
    rather than dropping events.
    """

    def __init__(self, config: LinkstuffsConfig, outbox: SQLiteOutbox):
        self.config = config
        self.outbox = outbox
        self._client: Any = None
        self._connected = False
        self._running = False
        self._worker: Optional[threading.Thread] = None

    @staticmethod
    def connect_payload(machine: MachineConfig) -> Dict[str, str]:
        return {"device": machine.display_name, "type": machine.machine_profile}

    @staticmethod
    def disconnect_payload(machine: MachineConfig) -> Dict[str, str]:
        return {"device": machine.display_name}

    @staticmethod
    def attributes_payload(
        machine: MachineConfig,
        profile: MachineProfile,
    ) -> Dict[str, Dict[str, Any]]:
        return {
            machine.display_name: {
                "endpoint_id": machine.endpoint_id,
                "display_name": machine.display_name,
                "machine_profile": machine.machine_profile,
                "vendor": profile.vendor,
                "model": profile.model,
                "secs_host": machine.host,
                "secs_port": machine.port,
                "secs_device_id": machine.secs_device_id,
                "csv_local_path": str(machine.csv_local_dir),
                "csv_network_path": str(machine.csv_network_dir or ""),
            }
        }

    def queue_machine_connect(self, machine: MachineConfig) -> None:
        if not self.config.enabled:
            return
        payload = self.connect_payload(machine)
        self.outbox.enqueue(
            LINKSTUFFS_TOPIC_CONNECT,
            payload,
            key=f"connect:{machine.display_name}:{time.time_ns()}",
            partition_key=machine.endpoint_id,
        )

    def queue_machine_attributes(self, machine: MachineConfig, profile: MachineProfile) -> None:
        if not self.config.enabled:
            return
        payload = self.attributes_payload(machine, profile)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self.outbox.enqueue(
            LINKSTUFFS_TOPIC_ATTRIBUTES,
            payload,
            key=f"attrs:{machine.display_name}:{payload_hash}",
            partition_key=machine.endpoint_id,
        )

    def queue_event(self, event: CanonicalEvent) -> None:
        if not self.config.enabled:
            return
        self.outbox.enqueue(
            LINKSTUFFS_TOPIC_TELEMETRY,
            event.linkstuffs_telemetry_payload(),
            key=f"telemetry:{event.event_key()}",
            partition_key=event.endpoint_id,
        )

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("Linkstuffs publisher disabled")
            return
        if self._running:
            return
        self._client = self._create_client()
        self._running = True
        self._worker = threading.Thread(
            target=self._publish_loop,
            name="LinkstuffsPublisher",
            daemon=True,
        )
        self._worker.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the worker and wait up to `timeout` for it to notice.

        The caller owns the budget: EapMiddlewareService.stop() shares one
        deadline across every worker, because these joins used to be a fixed
        10s each and a four-machine service spent minutes in teardown while
        the control panel's run button stayed disabled.
        """
        self._running = False
        if self._worker:
            self._worker.join(timeout=max(0.05, timeout))
            if self._worker.is_alive():
                logger.warning(
                    "%s worker did not stop within %.1fs; it is a daemon "
                    "thread and ends with the process",
                    type(self).__name__, timeout,
                )
            self._worker = None
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                logger.debug("Ignoring MQTT disconnect error", exc_info=True)

    def _create_client(self) -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError(
                "paho-mqtt is required for Linkstuffs publishing. "
                "Install requirements.txt on the Windows server."
            ) from exc

        # v2 Track B: prefer paho-mqtt 2.x VERSION2 callback API to silence the
        # deprecation warning and stay forward-compatible. Fall back to no
        # explicit version on paho-mqtt 1.x (kwarg doesn't exist).
        client_kwargs: Dict[str, Any] = {
            "client_id": self.config.client_id,
            "protocol": mqtt.MQTTv311,
        }
        callback_api = getattr(mqtt, "CallbackAPIVersion", None)
        using_v2_callbacks = callback_api is not None
        if using_v2_callbacks:
            client_kwargs["callback_api_version"] = callback_api.VERSION2
        client = mqtt.Client(**client_kwargs)
        client.username_pw_set(self.config.access_token)
        if self.config.tls:
            client.tls_set()

        if using_v2_callbacks:
            # paho v2: on_connect(client, userdata, connect_flags, reason_code, properties)
            def on_connect(  # type: ignore[no-redef]
                client: Any, userdata: Any, connect_flags: Any,
                reason_code: Any, properties: Any,
            ) -> None:
                rc_val = int(getattr(reason_code, "value", reason_code) or 0)
                self._connected = rc_val == 0
                if self._connected:
                    logger.info("Connected to Linkstuffs MQTT gateway")
                else:
                    logger.error("Linkstuffs MQTT connect failed rc=%s", reason_code)

            # paho v2: on_disconnect(client, userdata, disconnect_flags, reason_code, properties)
            def on_disconnect(  # type: ignore[no-redef]
                client: Any, userdata: Any, disconnect_flags: Any,
                reason_code: Any, properties: Any,
            ) -> None:
                self._connected = False
                logger.warning("Linkstuffs MQTT disconnected rc=%s", reason_code)
        else:
            # paho v1 legacy signatures
            def on_connect(  # type: ignore[no-redef]
                client: Any, userdata: Any, flags: Any, rc: int,
            ) -> None:
                self._connected = rc == 0
                if self._connected:
                    logger.info("Connected to Linkstuffs MQTT gateway")
                else:
                    logger.error("Linkstuffs MQTT connect failed rc=%s", rc)

            def on_disconnect(  # type: ignore[no-redef]
                client: Any, userdata: Any, rc: int,
            ) -> None:
                self._connected = False
                logger.warning("Linkstuffs MQTT disconnected rc=%s", rc)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.connect_async(self.config.host, self.config.port, self.config.keepalive_sec)
        client.loop_start()
        return client

    def _publish_loop(self) -> None:
        last_purge = 0.0
        while self._running:
            try:
                if time.time() - last_purge > 3600:
                    self.outbox.purge_old()
                    last_purge = time.time()
                if not self._connected:
                    time.sleep(1)
                    continue
                items = self.outbox.pending_heads(limit=100)
                for item in items:
                    if self._publish_item(item.topic, item.payload):
                        self.outbox.mark_sent(item.id)
                    else:
                        self.outbox.mark_failed(item.id, "MQTT publish failed")
                if not items:
                    time.sleep(0.2)
            except Exception as exc:
                logger.exception("Linkstuffs publish loop error: %s", exc)
                time.sleep(2)

    def _publish_item(self, topic: str, payload: Dict[str, Any]) -> bool:
        if self._client is None:
            return False
        try:
            info = self._client.publish(
                topic,
                json.dumps(payload, separators=(",", ":"), default=str),
                qos=self.config.qos,
                retain=self.config.publish_retain,
            )
            if self.config.qos == 0:
                if info.rc != 0:
                    # QoS0 has no ack, so a non-zero rc (e.g. MQTT_ERR_NO_CONN
                    # when the broker dropped between the connected check and
                    # the publish) is the only signal that this publish was
                    # not delivered. Without this line a persistently failing
                    # QoS0 publish is invisible outside the outbox DB.
                    logger.warning(
                        "Linkstuffs MQTT QoS0 publish rejected for %s (rc=%s)",
                        topic, info.rc,
                    )
                return info.rc == 0
            info.wait_for_publish(timeout=10)
            return bool(info.is_published())
        except (ValueError, RuntimeError) as exc:
            # Paho rejects the message outright (bad topic, oversized payload,
            # client not connected). Returning False lets the caller back the
            # row off; letting it escape would re-fetch the same row forever
            # and starve every other item in the outbox.
            logger.warning("Linkstuffs publish rejected for %s: %s", topic, exc)
            return False
