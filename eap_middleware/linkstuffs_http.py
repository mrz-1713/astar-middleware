"""HTTPS REST publisher for Linkstuffs.

Use when MQTT 1883 isn't reachable (Cloudflare blocks it; only 443 gets through).

  POST /api/v1/{token}/telemetry  - device telemetry
  POST /api/v1/{token}/attributes - device attributes

One token per device — create each device in Linkstuffs admin and map
display_name -> token under linkstuffs_http.device_tokens in production.yaml.

Uses urllib.request (stdlib) - no extra runtime deps.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import ssl
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from .models import CanonicalEvent, LinkstuffsHttpConfig, MachineConfig
from .outbox import SQLiteOutbox
from .profiles import MachineProfile

logger = logging.getLogger(__name__)

# Synthetic topic strings the outbox stores, mirroring the MQTT publisher's
# topic constants so both publishers can share an outbox if needed.
HTTP_TOPIC_TELEMETRY = "http/telemetry"
HTTP_TOPIC_ATTRIBUTES = "http/attributes"


class _PermanentPublishError(Exception):
    """Raised when retrying cannot repair an HTTP request."""

    def __init__(self, code: int):
        super().__init__(f"permanent HTTP {code}")
        self.code = code


class _UndeliverableError(Exception):
    """Raised when this publisher cannot send a payload *yet*.

    Distinct from _PermanentPublishError: nothing about the payload is wrong,
    the publisher is missing something it may still be given (a device token) or
    has been handed a row that is not its to send. Both are configuration
    faults, and destroying telemetry over a configuration fault is the one
    outcome that cannot be undone - so these rows stay queued.
    """


def _redact_token(url: str) -> str:
    """Mask the device token in a Linkstuffs URL path.

    Module-level so the redirect handler - which has no publisher instance -
    can use it too. A redirect's Location header carries the same
    /api/v1/<TOKEN>/ path as the request, so an unredacted Location in a log
    line leaks exactly the credential `_redact` exists to protect.
    """
    marker = "/api/v1/"
    idx = url.find(marker)
    if idx == -1:
        return url
    start = idx + len(marker)
    end = url.find("/", start)
    if end == -1:
        end = len(url)
    return url[:start] + "***" + url[end:]


class _RedirectDowngradeError(Exception):
    """A redirect would have turned this POST into a GET.

    urllib follows 301/302/303 by re-issuing the request as a GET with the
    body dropped - permitted by RFC 7231 and what every stdlib version does.
    The Linkstuffs/ThingsBoard telemetry endpoint only accepts POST, so a
    base_url whose origin redirects (the usual case: an http:// origin that
    the server bounces to https://) comes back as 405 Method Not Allowed.
    _post classifies any 4xx as permanent, and five of those dead-letter the
    row - so a one-character configuration mistake silently destroys telemetry
    while the CSV files keep being written and the log blames the server.
    """

    def __init__(self, code: int, location: str):
        safe = _redact_token(location)
        super().__init__(
            f"HTTP {code} redirect to {safe!r} would drop the POST body; "
            "set linkstuffs_http.base_url to the origin the server actually "
            "serves (normally the https:// one) so no redirect is needed"
        )
        self.code = code
        # Redacted on purpose: this value reaches the log, and a Location
        # header carries the same device token as the request URL.
        self.location = safe


class _NoMethodDowngradeRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a body-dropping redirect into a loud, actionable failure."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        if req.get_method() == "POST" and code in (301, 302, 303):
            raise _RedirectDowngradeError(code, newurl)
        # 307/308 preserve the method and body, so they are safe to follow.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LinkstuffsHttpPublisher:
    """Drains a per-publisher outbox and POSTs each item to the right
    device-scoped URL. Threaded worker like LinkstuffsGatewayPublisher."""

    def __init__(
        self,
        config: LinkstuffsHttpConfig,
        outbox: SQLiteOutbox,
        owner_display_name: Optional[str] = None,
    ):
        self.config = config
        self.outbox = outbox
        # Set for per-machine publishers. A row for anyone else in this queue
        # means two machines are sharing an outbox file, which is exactly the
        # situation that used to end with one machine's telemetry being marked
        # sent by a publisher that had no token for it.
        self.owner_display_name = owner_display_name
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self.last_http_status: Optional[int] = None
        self._warned_missing_token: set[str] = set()
        self._url_opener: Optional[urllib.request.OpenerDirector] = None
        self._warned_redirect: set[str] = set()

    # ---------- queueing ----------

    @staticmethod
    def telemetry_payload(event: CanonicalEvent) -> List[Dict[str, Any]]:
        """Linkstuffs HTTP telemetry API takes the device's events as a
        list of {ts, values} objects (no display_name wrapper - device
        identity is implicit in the token-bearing URL)."""
        from .models import timestamp_ms
        return [{
            "ts": timestamp_ms(event.timestamp),
            "values": event.telemetry_values(),
        }]

    @staticmethod
    def attributes_payload(
        machine: MachineConfig,
        profile: MachineProfile,
    ) -> Dict[str, Any]:
        return {
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

    def queue_machine_attributes(
        self,
        machine: MachineConfig,
        profile: MachineProfile,
    ) -> None:
        if not self.config.enabled:
            return
        payload = {
            "display_name": machine.display_name,
            "attributes": self.attributes_payload(machine, profile),
        }
        import hashlib
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self.outbox.enqueue(
            HTTP_TOPIC_ATTRIBUTES,
            payload,
            key=f"http-attrs:{machine.display_name}:{digest}",
            partition_key=machine.endpoint_id,
        )

    def queue_event(self, event: CanonicalEvent) -> None:
        if not self.config.enabled:
            return
        self.outbox.enqueue(
            HTTP_TOPIC_TELEMETRY,
            {
                "display_name": event.display_name,
                "telemetry": self.telemetry_payload(event),
            },
            key=f"http-telemetry:{event.event_key()}",
            partition_key=event.endpoint_id,
        )

    # ---------- lifecycle ----------

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("Linkstuffs HTTP publisher disabled")
            return
        if not self.config.base_url:
            logger.error(
                "Linkstuffs HTTP publisher enabled but base_url is empty"
            )
            return
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._publish_loop,
            name="LinkstuffsHttpPublisher",
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

    # ---------- internals ----------

    def _device_token(self, display_name: str) -> str:
        return self.config.device_tokens.get(display_name, "")

    def _publish_loop(self) -> None:
        while self._running:
            try:
                items = self.outbox.pending_heads(limit=100)
                if not items:
                    time.sleep(0.5)
                    continue
                for item in items:
                    if not self._running:
                        break
                    try:
                        published = self._publish_item(item.topic, item.payload)
                    except _PermanentPublishError as exc:
                        # A 4xx is permanent for the current configuration, not
                        # for the data. Tokens and routes are operator-fixable;
                        # retaining the row is what lets it drain afterwards - but
                        # only for a few rounds. After 5 consecutive permanent
                        # failures the row is dead-lettered (visible in the
                        # outbox stats) and stops pinging the endpoint; the
                        # operator re-queues it with `eap-middleware
                        # outbox-requeue` once the token/route is fixed.
                        self.outbox.mark_failed(item.id, str(exc))
                        if self.outbox.attempts(item.id) >= 5:
                            self.outbox.mark_dead(
                                item.id, f"permanent HTTP failure: {exc}"
                            )
                        continue
                    except _UndeliverableError as exc:
                        # Keep it queued. The operator can add the token or fix
                        # the routing and the backlog then drains on its own.
                        self.outbox.mark_failed(item.id, str(exc))
                        continue
                    if published:
                        self.outbox.mark_sent(item.id)
                    else:
                        self.outbox.mark_failed(item.id, "HTTP publish failed")
                # Fetch the next partition head immediately after success. A
                # fixed sleep here turns a 100-event reconnect backlog into a
                # ten-second artificial delay.
            except Exception:
                logger.exception("Linkstuffs HTTP publish loop error")
                time.sleep(2)

    def _publish_item(self, topic: str, payload: Dict[str, Any]) -> bool:
        display_name = str(payload.get("display_name", ""))
        if (
            self.owner_display_name is not None
            and display_name != self.owner_display_name
        ):
            raise _UndeliverableError(
                f"payload for {display_name!r} found in the queue belonging to "
                f"{self.owner_display_name!r}; the two machines are sharing an "
                "outbox file"
            )
        token = self._device_token(display_name)
        if not token:
            if display_name not in self._warned_missing_token:
                self._warned_missing_token.add(display_name)
                logger.error(
                    "No Linkstuffs device token configured for %s. Its "
                    "telemetry stays queued until one is added to "
                    "linkstuffs_http.device_tokens - it is not discarded.",
                    display_name,
                )
            raise _UndeliverableError(f"no device token for {display_name!r}")
        if topic == HTTP_TOPIC_TELEMETRY:
            url = self._url_for(token, "telemetry")
            body = payload.get("telemetry", [])
        elif topic == HTTP_TOPIC_ATTRIBUTES:
            url = self._url_for(token, "attributes")
            body = payload.get("attributes", {})
        else:
            raise _UndeliverableError(f"unknown HTTP outbox topic {topic!r}")
        return self._post(url, body)

    def _url_for(self, token: str, suffix: str) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/api/v1/{token}/{suffix}"

    @staticmethod
    def _redact(url: str) -> str:
        """Mask the device token embedded in the URL path before logging.

        The Linkstuffs/ThingsBoard URL is .../api/v1/<TOKEN>/<suffix> and the
        token is a bearer credential for writing telemetry. Logs rotate on disk
        (C:/SECSGEM_EAP/logs), so the raw URL must never be written there.
        """
        return _redact_token(url)

    def _opener(self) -> urllib.request.OpenerDirector:
        """One opener per publisher: custom redirect policy, and the TLS
        context that `verify_tls: false` asks for.

        Built once rather than per request - an opener carries no per-request
        state, and rebuilding it on every publish would re-create the SSL
        context at the tool's event rate.
        """
        if self._url_opener is None:
            handlers: List[urllib.request.BaseHandler] = [
                _NoMethodDowngradeRedirect()
            ]
            if not self.config.verify_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=ctx))
            self._url_opener = urllib.request.build_opener(*handlers)
        return self._url_opener

    def _post(self, url: str, body: Any) -> bool:
        data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
        # Cloudflare WAF blocks the default 'Python-urllib/X.Y' User-Agent with 403.
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "astar-eap-middleware/1.0",
            },
        )
        opener = self._opener()
        attempt = 0
        while attempt <= self.config.retry_count:
            attempt += 1
            retry_after: Optional[float] = None
            try:
                with opener.open(
                    req, timeout=self.config.timeout_sec,
                ) as resp:
                    self.last_http_status = int(resp.status)
                    if 200 <= resp.status < 300:
                        return True
                    logger.warning(
                        "HTTP POST %s returned %s",
                        self._redact(url), resp.status,
                    )
            except _RedirectDowngradeError as exc:
                # Configuration, not data: keep the row queued so the backlog
                # drains once base_url is corrected, instead of dead-lettering
                # telemetry over a redirect.
                if exc.location not in self._warned_redirect:
                    self._warned_redirect.add(exc.location)
                    logger.error(
                        "Linkstuffs HTTP route is misconfigured: POST %s -> %s",
                        self._redact(url), exc,
                    )
                raise _UndeliverableError(str(exc)) from exc
            except urllib.error.HTTPError as exc:
                self.last_http_status = int(exc.code)
                logger.warning(
                    "HTTP POST %s failed: %s %s",
                    self._redact(url), exc.code, exc.reason,
                )
                if 400 <= exc.code < 500 and exc.code not in {408, 425, 429}:
                    raise _PermanentPublishError(exc.code) from exc
                retry_after = self._retry_after_seconds(exc)
            except (urllib.error.URLError, TimeoutError) as exc:
                logger.warning(
                    "HTTP POST %s transport error (attempt %d): %s",
                    self._redact(url), attempt, exc,
                )
            if attempt <= self.config.retry_count and self._running:
                time.sleep(
                    min(
                        60.0,
                        max(self.config.retry_delay_sec, retry_after or 0.0),
                    )
                )
        return False

    @staticmethod
    def _retry_after_seconds(exc: urllib.error.HTTPError) -> Optional[float]:
        value = exc.headers.get("Retry-After") if exc.headers else None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                now = time.time()
                return max(0.0, retry_at.timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                return None
