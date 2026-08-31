"""Optional n8n-compatible encrypted legacy API publisher."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

from .models import CanonicalEvent, LegacyApiConfig
from .outbox import SQLiteOutbox
from .profiles import MachineProfile
from .secure_payload import AES_256_GCM_V2, LEGACY_CTR_V1, SecurePayloadCodec

logger = logging.getLogger(__name__)


# 408 and 429 are deliberately absent: both mean "try again later".
_PERMANENT_HTTP_CODES = frozenset({400, 401, 403, 404, 405, 410, 413, 422})


class _PermanentPostError(RuntimeError):
    """The legacy endpoint rejected the request; retrying cannot help."""



def _format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def build_legacy_api_payload(
    event: CanonicalEvent,
    profile: MachineProfile,
    token_id: str = "",
) -> Dict[str, Any]:
    mapping = profile.resolve_event(event.raw_event_name, event.ceid)
    tool_event = mapping.csv_tool_event
    datetime_start = ""
    datetime_end = ""
    if tool_event == "Lot_Start":
        datetime_start = _format_datetime(event.timestamp)
    elif tool_event == "Lot_End":
        datetime_start = str(
            event.raw_payload.get("LOT_START_TIME")
            or event.raw_payload.get("LotStartTime")
            or event.raw_payload.get("DatetimeStart")
            or ""
        )
        datetime_end = _format_datetime(event.timestamp)

    payload: Dict[str, Any] = {
        "Status": "Ok",
        "ErrorMsg": "",
        "DatetimeStart": datetime_start,
        "DatetimeEnd": datetime_end,
        "ToolEvent": tool_event,
        "EAP_ToolName": event.display_name,
        "LoadPort": event.load_port,
        "LotID": event.lot_id,
        "Recipe": event.recipe,
        "SECSGEM_Raw_Event": event.secs_raw_event or mapping.secs_raw_event,
    }
    if token_id:
        payload["TokenID"] = token_id
    return payload


class LegacyApiPublisher:
    """Publishes canonical events to the legacy HTTP API.

    Writes go through the durable outbox, so an API outage delays delivery
    rather than dropping events.
    """

    def __init__(
        self,
        config: LegacyApiConfig,
        outbox: SQLiteOutbox,
    ):
        self.config = config
        self.outbox = outbox
        self._codec: Optional[SecurePayloadCodec] = None
        if config.enabled and config.encrypted:
            if config.encryption_mode == AES_256_GCM_V2:
                self._codec = SecurePayloadCodec.from_aes256_gcm_key_base64(
                    config.encryption_key_b64
                )
            elif config.encryption_mode == LEGACY_CTR_V1 and (
                config.first_key and config.second_key
            ):
                self._codec = SecurePayloadCodec.from_raw_keys(
                    config.first_key,
                    config.second_key,
                )
            elif config.encryption_mode == LEGACY_CTR_V1:
                self._codec = SecurePayloadCodec.from_base64_keys(
                    config.first_key_b64,
                    config.second_key_b64,
                )
            else:
                raise RuntimeError(
                    "encrypted legacy API has no supported encryption mode"
                )
        self._running = False
        self._worker: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.config.enabled or self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._publish_loop,
            name="LegacyApiPublisher",
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

    def queue_event(self, event: CanonicalEvent, profile: MachineProfile) -> None:
        if not self.config.enabled:
            return
        payload = build_legacy_api_payload(event, profile, token_id=self.config.token_id)
        if (
            self.config.send_tool_events
            and payload["ToolEvent"] not in self.config.send_tool_events
        ):
            return
        body = self._request_body(payload)
        self.outbox.enqueue(
            self.config.url,
            body,
            key=f"legacy-api:{event.event_key()}:{self.config.encrypted}",
            partition_key=event.endpoint_id,
        )

    def _request_body(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.config.encrypted:
            return payload
        if self._codec is None:
            raise RuntimeError("encrypted legacy API is enabled without a codec")
        return {"data": self._codec.encrypt_json(payload)}

    def _publish_loop(self) -> None:
        while self._running:
            try:
                items = self.outbox.pending_heads(limit=50)
                for item in items:
                    try:
                        ok, error = self._post_json(item.topic, item.payload)
                    except _PermanentPostError as exc:
                        # Permanent for today's credentials/route, not for the
                        # data. Keep it ordered and queued for operator repair -
                        # but only for a few rounds. A row that never leaves
                        # `pending` stays the partition head forever and holds
                        # every later event for that machine hostage, so after
                        # 5 consecutive permanent failures it is dead-lettered
                        # (visible in outbox stats) and stops pinging the
                        # endpoint, matching the HTTP publisher's policy.
                        self.outbox.mark_failed(item.id, str(exc))
                        if self.outbox.attempts(item.id) >= 5:
                            self.outbox.mark_dead(
                                item.id, f"permanent legacy API failure: {exc}"
                            )
                        logger.warning(
                            "Legacy API permanent failure for %s: %s",
                            item.partition_key, exc,
                        )
                        continue
                    if ok:
                        self.outbox.mark_sent(item.id)
                    else:
                        logger.warning(
                            "Legacy API publish failed for %s: %s",
                            item.partition_key, error,
                        )
                        self.outbox.mark_failed(item.id, error)
                if not items:
                    time.sleep(0.5)
            except Exception as exc:
                logger.exception("Legacy API publish loop error: %s", exc)
                time.sleep(2)

    def _post_json(self, url: str, payload: Dict[str, Any]) -> tuple[bool, str]:
        data = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            # Configuration rejects non-HTTP(S) and insecure production URLs.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.config.timeout_sec
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status < 200 or response.status >= 300:
                    return False, f"HTTP {response.status}: {body[:500]}"
                try:
                    parsed = json.loads(body) if body else {}
                    if isinstance(parsed, dict) and parsed.get("Status") == "Fail":
                        return False, parsed.get("ErrorMsg", "Legacy API returned Fail")
                except json.JSONDecodeError:
                    pass
                return True, ""
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in _PERMANENT_HTTP_CODES:
                raise _PermanentPostError(f"HTTP {exc.code}: {body}") from exc
            return False, f"HTTP {exc.code}: {body}"
        except Exception as exc:
            return False, str(exc)
