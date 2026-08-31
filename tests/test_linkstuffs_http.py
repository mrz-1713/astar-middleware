"""LinkstuffsHttpPublisher tests against an in-process HTTP server.

The local server captures every POST so we can assert the exact URL path,
JSON body, and that the publisher uses the right per-device token.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from dataclasses import replace
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

import pytest

from eap_middleware.config import ConfigError, service_config_from_dict
from eap_middleware.linkstuffs_http import (
    HTTP_TOPIC_TELEMETRY,
    LinkstuffsHttpPublisher,
)
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import (
    LinkstuffsHttpConfig,
    MachineConfig,
)
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry


# ----- in-process capturing HTTP server -----

class _CapturingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server contract
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.captured.append({  # type: ignore[attr-defined]
            "path": self.path,
            "body": body,
            "content_type": self.headers.get("Content-Type", ""),
            "user_agent": self.headers.get("User-Agent", ""),
        })
        # Default: 200 OK like Linkstuffs
        status = self.server.status_to_return  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs):  # silence noisy default logging
        pass


def _start_server() -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    server.captured: List[Dict[str, Any]] = []  # type: ignore[attr-defined]
    server.status_to_return = 200  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


# ----- shared fixtures -----

def _machine(tmp_path, display="DAVINCI_HTTP_TEST"):
    return MachineConfig(
        endpoint_id="TOOL_HTTP",
        display_name=display,
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _event(machine, profile):
    return CanonicalMapper(profile).from_secs_event(
        machine, 3140002,
        {
            "DATETIME": "20251128094700",
            "_v_raw": ["W001", "LOT_HTTP", "Recipe_X"],
        },
    )


def _wait_captured(server, expected: int, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and len(server.captured) < expected:
        time.sleep(0.05)
    return server.captured


# ----- tests -----

def test_telemetry_post_lands_on_per_device_url(tmp_path):
    server, base_url = _start_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={machine.display_name: "TOKEN-ABC"},
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_event(_event(machine, profile))
        captured = _wait_captured(server, 1)
    finally:
        publisher.stop()
        server.shutdown()

    assert len(captured) == 1
    assert captured[0]["path"] == "/api/v1/TOKEN-ABC/telemetry"
    body = json.loads(captured[0]["body"])
    assert isinstance(body, list) and len(body) == 1
    entry = body[0]
    assert "ts" in entry and isinstance(entry["ts"], int)
    values = entry["values"]
    assert values["event_type"] == "process_start"
    assert values["lot_id"] == "LOT_HTTP"
    assert values["recipe"] == "Recipe_X"


def test_attributes_post_lands_on_per_device_url(tmp_path):
    server, base_url = _start_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={machine.display_name: "TOK-1"},
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_machine_attributes(machine, profile)
        captured = _wait_captured(server, 1)
    finally:
        publisher.stop()
        server.shutdown()

    assert captured[0]["path"] == "/api/v1/TOK-1/attributes"
    attrs = json.loads(captured[0]["body"])
    assert attrs["display_name"] == machine.display_name
    assert attrs["vendor"] == "MueTec"


def test_unmapped_machine_stays_queued_without_blocking_others(tmp_path):
    """A missing token is repairable configuration, not permission to lose the
    event. Its machine partition waits while another machine still publishes."""
    server, base_url = _start_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mapped = replace(
        _machine(tmp_path, display="MAPPED_DEVICE"), endpoint_id="MAPPED"
    )
    unmapped = replace(
        _machine(tmp_path, display="NO_TOKEN_DEVICE"), endpoint_id="UNMAPPED"
    )
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={"MAPPED_DEVICE": "TOK-MAPPED"},
        ),
        outbox,
    )
    try:
        publisher.start()
        publisher.queue_event(_event(unmapped, profile))  # retained for a token
        publisher.queue_event(_event(mapped, profile))    # published
        captured = _wait_captured(server, 1, timeout=3.0)
    finally:
        publisher.stop()
        server.shutdown()

    assert len(captured) == 1
    assert captured[0]["path"] == "/api/v1/TOK-MAPPED/telemetry"
    assert outbox.stats()["pending"] == 1


def test_disabled_publisher_does_not_publish(tmp_path):
    server, base_url = _start_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=False,            # off
            base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_event(_event(machine, profile))
        time.sleep(0.5)
    finally:
        publisher.stop()
        server.shutdown()

    assert server.captured == [], "disabled publisher must not POST"


def test_4xx_response_is_not_retried(tmp_path):
    """Linkstuffs returns 401 for a bad token. Don't burn through retry
    budget - mark failed and move on."""
    server, base_url = _start_server()
    server.status_to_return = 401  # type: ignore[attr-defined]
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={machine.display_name: "BAD-TOKEN"},
            retry_count=3,
            retry_delay_sec=0.05,
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_event(_event(machine, profile))
        # Wait for the first attempt to land, then watch for ~0.5s to confirm
        # no in-call retry burst (which would add 3 more requests immediately).
        _wait_captured(server, 1, timeout=3.0)
        time.sleep(0.5)
    finally:
        publisher.stop()
        server.shutdown()

    # 4xx must NOT trigger the publisher's in-call retry loop (would be 4
    # attempts back-to-back). Outbox-level backoff is 2s+, so within ~1s of
    # the first attempt we see at most 1 publish.
    assert len(server.captured) == 1, server.captured


def test_5xx_response_is_retried_then_marked_failed(tmp_path):
    server, base_url = _start_server()
    server.status_to_return = 503  # type: ignore[attr-defined]
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
            retry_count=2,
            retry_delay_sec=0.02,
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_event(_event(machine, profile))
        time.sleep(0.6)
    finally:
        publisher.stop()
        server.shutdown()

    # 5xx errors retry: 1 + 2 retries = at least 3 attempts
    assert len(server.captured) >= 3


def test_retry_after_header_is_parsed_for_rate_limits():
    headers = Message()
    headers["Retry-After"] = "7"
    error = urllib.error.HTTPError(
        "https://example.invalid", 429, "rate limited", headers, None
    )
    assert LinkstuffsHttpPublisher._retry_after_seconds(error) == 7.0


# ----- config validation -----

def _base_yaml(http_section):
    return {
        "linkstuffs": {
            "enabled": False, "host": "x", "port": 1883, "access_token": "",
            "client_id": "t",
        },
        "linkstuffs_http": http_section,
        "machines": [{
            "endpoint_id": "TOOL_01",
            "display_name": "X",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "10.0.0.1",
        }],
    }


def test_linkstuffs_http_requires_base_url_when_enabled():
    with pytest.raises(ConfigError, match="base_url"):
        service_config_from_dict(_base_yaml({
            "enabled": True,
            "device_tokens": {"X": "tok"},
            # base_url missing
        }))


def test_linkstuffs_http_requires_at_least_one_token_when_enabled():
    with pytest.raises(ConfigError, match="device_tokens"):
        service_config_from_dict(_base_yaml({
            "enabled": True,
            "base_url": "https://server",
            # device_tokens missing
        }))


def test_linkstuffs_http_disabled_with_no_settings_is_ok():
    data = _base_yaml({"enabled": False})
    data["machines"][0]["enabled"] = False
    cfg = service_config_from_dict(data)
    assert cfg.linkstuffs_http.enabled is False
    assert cfg.linkstuffs_http.base_url == ""


def test_publisher_sends_custom_user_agent_not_python_urllib(tmp_path):
    """Cloudflare WAF blocks 'Python-urllib/X.Y' with 403. Verify we send a custom UA."""
    server, base_url = _start_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        publisher.start()
        publisher.queue_event(_event(machine, profile))
        captured = _wait_captured(server, 1)
    finally:
        publisher.stop()
        server.shutdown()

    ua = captured[0]["user_agent"]
    assert "Python-urllib" not in ua, (
        f"User-Agent {ua!r} would be blocked by Cloudflare; need a custom UA"
    )
    assert ua, "User-Agent must not be empty"
    assert publisher.last_http_status == 200


def test_linkstuffs_http_loads_device_token_mapping():
    cfg = service_config_from_dict(_base_yaml({
        "enabled": True,
        "base_url": "https://server",
        "device_tokens": {
            "X": "tok-x",
            "SPTS_fxP_OMEGA_01": "tok-spts",
            "DAVINCI200_MC4_HC1_01": "tok-dav",
        },
    }))
    assert cfg.linkstuffs_http.enabled is True
    assert cfg.linkstuffs_http.device_tokens == {
        "X": "tok-x",
        "SPTS_fxP_OMEGA_01": "tok-spts",
        "DAVINCI200_MC4_HC1_01": "tok-dav",
    }


# ----- redirect must not silently downgrade the POST -----

class _RedirectingHandler(BaseHTTPRequestHandler):
    """Stands in for an origin that bounces http:// to https://."""

    def do_POST(self):  # noqa: N802 - http.server contract
        self.server.posts.append(self.path)  # type: ignore[attr-defined]
        self.send_response(301)
        self.send_header("Location", f"https://moved.invalid{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - http.server contract
        # What urllib turns the POST into, and what the real telemetry
        # endpoint answers to it.
        self.server.gets.append(self.path)  # type: ignore[attr-defined]
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


def test_redirect_is_refused_instead_of_dead_lettering_telemetry(tmp_path, caplog):
    """A 301 on a POST must not become a GET, a 405, and a dead-lettered row.

    urllib re-issues 301/302/303 as a GET with the body dropped. The
    Linkstuffs telemetry endpoint is POST-only, so it answers 405, `_post`
    reads any 4xx as permanent, and five of those dead-letter the payload -
    telemetry destroyed by a base_url scheme. The row must stay queued.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    server.posts: List[str] = []  # type: ignore[attr-defined]
    server.gets: List[str] = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=base_url,
            device_tokens={"DEV_REDIRECT": "TOKEN-SECRET"},
            retry_count=0,
        ),
        SQLiteOutbox(tmp_path / "redirect.sqlite3"),
    )
    try:
        with caplog.at_level("ERROR"):
            with pytest.raises(Exception) as excinfo:
                publisher._publish_item(
                    HTTP_TOPIC_TELEMETRY,
                    {
                        "display_name": "DEV_REDIRECT",
                        "telemetry": [{"ts": 1, "values": {"a": 1}}],
                    },
                )
    finally:
        server.shutdown()

    # Undeliverable (stays queued), never a permanent failure that dead-letters.
    assert type(excinfo.value).__name__ == "_UndeliverableError", (
        f"a redirect must keep the row queued; got {type(excinfo.value).__name__}"
    )
    assert server.gets == [], (
        "the POST must never be re-issued as a GET against the telemetry "
        f"endpoint; server saw GETs: {server.gets}"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "TOKEN-SECRET" not in logged, (
        "the device token must not reach the log, including via the "
        f"redirect Location header: {logged}"
    )
    assert "base_url" in logged, "the log must name the setting to fix"
