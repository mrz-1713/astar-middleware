"""Setup must not require anyone to type a config, an IP, or a command.

Each test here stands for one thing an operator used to have to type by
hand on a two-machine installation, and the way it used to go wrong.
"""

from __future__ import annotations

import ipaddress
import time
from pathlib import Path

import pytest

from eap_middleware import netinfo, probe
from gui import model as gui_model
from simulator_gui import model as sim_model

ROOT = Path(__file__).resolve().parents[1]


def _deny_writes(directory: Path) -> None:
    """chmod a directory read-only, or skip if this platform ignores that.

    Windows ignores POSIX mode bits, and root bypasses them entirely, so
    these tests would otherwise fail claiming a write succeeded that the
    product never permits in the field.
    """
    directory.chmod(0o500)
    probe = directory / ".writable-probe"
    try:
        probe.write_bytes(b"")
    except OSError:
        return
    probe.unlink()
    directory.chmod(0o700)
    pytest.skip("this platform does not enforce read-only directories")


# ----- addresses come from a pick-list, not from reading ipconfig -----

def test_unusable_addresses_are_never_offered():
    """Binding any of these produces a listener no peer can reach.

    169.254 is the one that costs an afternoon: DHCP failed, the simulator
    starts happily, and the far end times out exactly as it would against
    a wrong IP.
    """
    for rejected in ("127.0.0.1", "127.0.1.1", "169.254.10.4", "0.0.0.0", ""):
        assert not netinfo._is_usable(rejected), rejected
    assert netinfo._is_usable("192.168.1.10")
    assert netinfo._is_usable("10.4.0.9")


def test_primary_address_leads_the_list():
    """A Windows host often has several adapters present, and only one of
    them is the one the peer machine can see."""
    addresses = netinfo.local_ipv4_addresses()
    primary = netinfo.primary_ipv4()
    if primary:
        assert addresses[0] == primary


def test_address_list_is_stable_between_calls():
    """getaddrinfo does not promise an order, and a pick-list that
    reshuffles between launches is one an operator stops trusting."""
    assert netinfo.local_ipv4_addresses() == netinfo.local_ipv4_addresses()


def test_only_a_listener_may_bind_every_adapter():
    """0.0.0.0 is a legal bind and never a legal destination."""
    found = ["10.0.0.5", "10.0.1.5"]
    assert sim_model.address_choices("passive", found)[0] == netinfo.ALL_INTERFACES
    assert netinfo.ALL_INTERFACES not in sim_model.address_choices("active", found)


def test_wildcard_bind_resolves_before_being_quoted_to_the_peer():
    """'Connect to 0.0.0.0' is the advice that cannot possibly work."""
    assert sim_model.peer_target(
        "passive", "0.0.0.0", 5051, "10.0.0.5"
    ) == "10.0.0.5:5051"


def test_an_explicit_bind_is_quoted_verbatim():
    assert sim_model.peer_target(
        "passive", "10.1.2.3", 5051, "10.0.0.5"
    ) == "10.1.2.3:5051"


def test_a_dialler_has_no_address_to_offer_the_peer():
    """An active simulator connects out; the peer is the one that listens,
    so there is nothing to enter on the other machine."""
    assert sim_model.peer_target("active", "10.1.2.3", 5051, "10.0.0.5") == ""


def test_no_adapter_found_yields_no_advice_rather_than_a_wrong_one():
    assert sim_model.peer_target("passive", "0.0.0.0", 5051, "") == ""


# ----- a passive listener is not asked to choose an address -----

def test_wildcard_bind_is_reported_as_accepting_everywhere():
    """In passive mode the address goes straight to socket.bind, so the
    default already accepts on every adapter. It restricts; it is not a
    destination. Presenting it as a required choice is what leads an
    operator to pin one NIC on a multi-homed machine and become unreachable in
    a way that looks exactly like a wrong IP on the other machine."""
    summary = sim_model.listening_summary(
        "passive", "0.0.0.0", ["10.0.0.5", "10.0.1.5"]
    )
    assert "every adapter" in summary
    assert "10.0.0.5" in summary and "10.0.1.5" in summary


def test_a_pinned_adapter_says_what_it_refuses():
    summary = sim_model.listening_summary(
        "passive", "10.0.0.5", ["10.0.0.5", "10.0.1.5"]
    )
    assert "Restricted to 10.0.0.5" in summary
    assert "refused" in summary


def test_an_empty_address_counts_as_unrestricted():
    """A config written without the key must not read as a restriction."""
    for unrestricted in ("", "   ", "0.0.0.0"):
        assert sim_model.binds_every_adapter(unrestricted), repr(unrestricted)
    assert not sim_model.binds_every_adapter("10.0.0.5")


def test_a_dialler_gets_no_listening_summary():
    assert sim_model.listening_summary("active", "10.0.0.5", ["10.0.0.5"]) == ""


# ----- discovery never runs on the UI thread -----

def test_address_helpers_do_no_network_lookups(monkeypatch):
    """These are driven by widget traces that fire on every keystroke, and
    getaddrinfo on a Windows host with no reachable DNS blocks for seconds.
    Typing an IP re-ran the scan per character."""

    def explode(*_args, **_kwargs):
        raise AssertionError("discovery must not run from a widget trace")

    monkeypatch.setattr(netinfo, "local_ipv4_addresses", explode)
    monkeypatch.setattr(netinfo, "primary_ipv4", explode)
    monkeypatch.setattr(netinfo, "reachable_address", explode)

    found = ["10.0.0.5"]
    assert sim_model.address_choices("passive", found)
    assert sim_model.peer_target("passive", "0.0.0.0", 5051, "10.0.0.5")
    assert sim_model.listening_summary("passive", "0.0.0.0", found)
    assert sim_model.binds_every_adapter("0.0.0.0")


# ----- the firewall rule matches the port actually in use -----

def test_firewall_rule_targets_the_configured_port():
    command = sim_model.firewall_command(5051)
    assert "-LocalPort 5051" in command
    assert "-Direction Inbound" in command
    # Re-running the panel button must not stack duplicate rules.
    assert "Remove-NetFirewallRule" in command


def test_firewall_command_is_elevated_and_policy_scoped():
    argv = list(sim_model.firewall_argv(5051))
    assert argv[0] == "powershell"
    joined = " ".join(argv)
    assert "-Verb RunAs" in joined
    # Bypass is per-process; the machine's policy is never rewritten.
    assert "Bypass" in joined
    assert "Set-ExecutionPolicy" not in joined


def test_quotes_in_a_command_cannot_end_the_argument_early():
    argv = list(netinfo.elevated_powershell_argv("Write-Host 'hi'"))
    assert "''hi''" in argv[-1]


# ----- the middleware panel works on a machine that has nothing yet -----

def test_first_run_seeds_a_config_from_the_shipped_template(tmp_path):
    target = tmp_path / "config" / "production.yaml"
    written = gui_model.seed_config(target, template=ROOT / "config" / "production.yaml")

    assert written == target
    assert "machines:" in target.read_text(encoding="utf-8")


def test_seeding_never_overwrites_a_live_config(tmp_path):
    """production.yaml carries the operator's machine IPs and device tokens.
    Re-seeding over it would silently reset them to template placeholders."""
    target = tmp_path / "production.yaml"
    target.write_text("machines: [] # mine\n", encoding="utf-8")

    gui_model.seed_config(target, template=ROOT / "config" / "production.yaml")

    assert target.read_text(encoding="utf-8") == "machines: [] # mine\n"


def test_seeding_says_so_when_there_is_no_template(tmp_path):
    with pytest.raises(OSError):
        gui_model.seed_config(tmp_path / "out.yaml", template=tmp_path / "absent.yaml")


def test_seeding_never_invents_an_install_tree(monkeypatch, tmp_path):
    """Two of the candidate paths are traps.

    One is derived from sys.executable for the frozen-exe case and resolves
    to Python's own bin directory under a normal interpreter; the other is a
    Windows absolute path that would fabricate a literal "C:/SECSGEM_EAP"
    folder under the working directory on a developer's Mac.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gui_model,
        "candidate_config_paths",
        lambda: [
            tmp_path / "nowhere" / "config" / "production.yaml",
            Path("C:/SECSGEM_EAP/config/production.yaml"),
        ],
    )

    assert gui_model.seed_target_path() is None
    assert list(tmp_path.iterdir()) == []


def test_seeding_picks_a_config_dir_that_already_exists(tmp_path):
    """install.ps1 creates the config directory and fills it, so after a real
    install it is always there and seeding only ever replaces a deleted file."""
    real = tmp_path / "app" / "config"
    real.mkdir(parents=True)

    import unittest.mock

    with unittest.mock.patch.object(
        gui_model,
        "candidate_config_paths",
        lambda: [tmp_path / "absent" / "config" / "production.yaml",
                 real / "production.yaml"],
    ):
        assert gui_model.seed_target_path() == real / "production.yaml"


def test_a_missing_service_is_not_mistaken_for_a_running_one(tmp_path):
    """The panel asks a service to test connections by dropping a command
    file. With no service nothing consumes it, so the panel must know to do
    the work itself instead of appearing to hang."""
    assert not gui_model.service_is_live(tmp_path)


def test_a_stale_status_file_means_no_service(tmp_path):
    status = tmp_path / "runtime_status.json"
    status.write_text("{}", encoding="utf-8")
    stale = time.time() - gui_model.SERVICE_STALE_AFTER_SEC - 60
    import os

    os.utime(status, (stale, stale))

    assert not gui_model.service_is_live(tmp_path)


def test_a_fresh_status_file_means_a_live_service(tmp_path):
    (tmp_path / "runtime_status.json").write_text("{}", encoding="utf-8")

    assert gui_model.service_is_live(tmp_path)


def test_the_host_field_offers_addresses_without_closing_the_set():
    """A simulator on this network is at one of these; a real tool is not, so the
    box has to stay editable."""
    kinds = {path: kind for path, _label, kind in gui_model.MACHINE_FIELDS}
    assert kinds["host"] == "address"
    # An "address" kind must round-trip as plain text through the coercers,
    # or saving the form would corrupt the field.
    assert gui_model.parse_value("address", " 10.0.0.4 ") == "10.0.0.4"
    assert gui_model.format_value("address", "10.0.0.4") == "10.0.0.4"


# ----- proving the cable works must not depend on the telemetry setup -----

def test_connection_test_ignores_the_upstream_route(tmp_path):
    """A machine with no Linkstuffs token must still be testable.

    Full config validation refuses any *enabled* machine that has no
    upstream route, so building the probe target through it would answer
    "Missing linkstuffs_http.device_tokens" when an operator asks whether
    an HSMS cable works - about a transport the test never uses.
    """
    import yaml
    from eap_middleware.config import ConfigError, service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    row = next(m for m in raw["machines"] if m["endpoint_id"] == "TOOL_02")
    row.update(host="10.0.0.9", port=5051, hsms_mode="active", enabled=True)

    # The trap, pinned: this is what the panel must not call.
    with pytest.raises(ConfigError, match="device_tokens"):
        service_config_from_dict(raw)

    target = gui_model.probe_target(row)
    assert target.endpoint_id == "TOOL_02"
    assert (target.host, target.port, target.hsms_mode) == ("10.0.0.9", 5051, "active")


def test_probe_target_names_the_field_that_is_wrong():
    """The panel shows these verbatim, so they have to say which box."""
    with pytest.raises(ValueError, match="host"):
        gui_model.probe_target({"host": "  ", "port": 5051})
    with pytest.raises(ValueError, match="Port"):
        gui_model.probe_target({"host": "10.0.0.9", "port": "not-a-number"})
    with pytest.raises(ValueError, match="between 1 and 65535"):
        gui_model.probe_target({"host": "10.0.0.9", "port": 70000})
    with pytest.raises(ValueError, match="HSMS mode"):
        gui_model.probe_target(
            {"host": "10.0.0.9", "port": 5051, "hsms_mode": "sideways"}
        )


def test_probe_target_defaults_match_the_config_loader():
    """Drift here would make the panel test a different link than the
    service opens for the same row."""
    target = gui_model.probe_target({"host": "10.0.0.9", "port": 5051})

    assert target.hsms_bind_address == "0.0.0.0"
    assert target.secs_device_id == 0
    assert target.hsms_mode == "active"


def test_probe_target_carries_exactly_what_the_probe_reads():
    """probe_machine touches these six attributes and nothing else."""
    target = gui_model.probe_target({"host": "10.0.0.9", "port": 5051})
    for attribute in (
        "endpoint_id", "host", "port",
        "secs_device_id", "hsms_mode", "hsms_bind_address",
    ):
        assert hasattr(target, attribute), attribute


# ----- CLI and panel report the same verdict -----

def test_probe_result_keeps_the_documented_output_format():
    """docs/TWO_VM_FABNET_TEST_SETUP.md and NEXGEN_MG_TWO_VM_END_TO_END.md
    quote these lines as real captured output."""
    ok = probe.ProbeResult(
        endpoint_id="TOOL_02", host="192.168.102.129", port=5050,
        device_id=0, ok=True, identity=["DaVinci200", "DaVinci200 Version 4.9.3"],
    )
    assert ok.as_line() == (
        "secs-ok: TOOL_02 192.168.102.129:5050 device_id=0 "
        "identity=['DaVinci200', 'DaVinci200 Version 4.9.3']"
    )

    bad = probe.ProbeResult(
        endpoint_id="TOOL_02", host="192.168.102.129", port=5050,
        device_id=0, ok=False, error="timed out",
    )
    assert bad.as_line() == "secs-fail: TOOL_02 192.168.102.129:5050 timed out"


def test_the_cli_and_the_panel_share_one_probe():
    """Two implementations would let `test-machine` and the panel's Test
    connection disagree, leaving an operator no way to tell which lies."""
    cli = (ROOT / "eap_middleware" / "cli.py").read_text(encoding="utf-8")
    app = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")

    assert "from eap_middleware.probe import probe_machines" in cli
    assert "from eap_middleware.probe import probe_machine" in app
    # The old inline copy must be gone from the CLI, not just unused.
    assert "waitfor_communicating" not in cli


def test_probe_reports_failure_rather_than_raising():
    """Every failure an operator can cause - wrong IP, closed port, both
    ends passive - has to arrive as a result the caller can print."""

    # A closed local port, not an unroutable one: a black-hole address makes
    # the kernel sit on the SYN for the full connect timeout, which turned
    # this test into a 75-second wait. Refused is just as failed, instantly.
    import socket

    scout = socket.socket()
    scout.bind(("127.0.0.1", 0))
    closed_port = scout.getsockname()[1]
    scout.close()

    class Unreachable:
        endpoint_id = "TOOL_09"
        host = "127.0.0.1"
        port = closed_port
        secs_device_id = 0
        hsms_mode = "active"
        hsms_bind_address = "0.0.0.0"

    result = probe.probe_machine(Unreachable(), timeout=0.2)

    assert not result.ok
    assert result.error
    assert result.as_line().startswith(f"secs-fail: TOOL_09 127.0.0.1:{closed_port}")


# ----- the panel must say what is actually collecting data -----

def test_service_state_distinguishes_stopped_from_running(tmp_path):
    """Every runtime column reads "-" whether the service is stopped or
    merely idle, so without this the panel gives no way to tell a working
    install from a dead one."""
    state, sentence = gui_model.service_state(tmp_path)
    assert state == gui_model.SERVICE_STOPPED
    assert "not running" in sentence
    # It must also say why enabling a machine appeared to do nothing.
    assert "Enabling a machine only records the choice" in sentence

    (tmp_path / "runtime_status.json").write_text("{}", encoding="utf-8")
    state, sentence = gui_model.service_state(tmp_path)
    assert state == gui_model.SERVICE_EXTERNAL

    state, _ = gui_model.service_state(tmp_path, owned=True)
    assert state == gui_model.SERVICE_LOCAL

    state, sentence = gui_model.service_state(tmp_path, busy="starting")
    assert state == gui_model.SERVICE_BUSY
    assert "Starting" in sentence


def test_a_missing_https_route_is_not_reported_as_a_missing_token():
    """The check fires purely on linkstuffs_http.enabled being off, but the
    message said "route/device_tokens" - so an operator who had already
    entered the token went back to check the token, forever."""
    import yaml
    from eap_middleware.config import ConfigError, service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"] = [m for m in raw["machines"] if m["endpoint_id"] == "TOOL_02"]
    machine = raw["machines"][0]
    machine.update(enabled=True, offline_test_mode=False)
    # A token IS present; only the per-machine route is off.
    machine["linkstuffs_http"] = {"enabled": False, "device_token": "a-real-token"}

    with pytest.raises(ConfigError) as caught:
        service_config_from_dict(raw)

    message = str(caught.value)
    assert "Enable machine HTTPS" in message
    assert "not about the device token" in message
    # And it must name the escape hatch, which nothing used to mention.
    assert "Offline test mode" in message


def test_a_missing_token_says_how_to_run_without_one():
    import yaml
    from eap_middleware.config import ConfigError, service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"] = [m for m in raw["machines"] if m["endpoint_id"] == "TOOL_02"]
    raw["machines"][0].update(enabled=True, offline_test_mode=False)

    with pytest.raises(ConfigError, match="Offline test mode"):
        service_config_from_dict(raw)


def test_offline_test_mode_is_the_documented_way_to_skip_telemetry():
    """It is what lets a link-only install validate at all, and it must
    keep exempting a machine from the upstream requirement."""
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"] = [m for m in raw["machines"] if m["endpoint_id"] == "TOOL_02"]
    raw["machines"][0].update(enabled=True, offline_test_mode=True)

    config = service_config_from_dict(raw)
    assert [m.endpoint_id for m in config.machines if m.enabled] == ["TOOL_02"]


def test_raw_secsgem_states_are_translated(monkeypatch):
    """NOT_COMMUNICATING tells an operator nothing about whether that is
    normal (nobody has dialled in) or wrong (the peer is refusing)."""
    waiting = sim_model.link_state_sentence("NOT_COMMUNICATING", "passive")
    assert "normal until the middleware connects" in waiting

    dialling = sim_model.link_state_sentence("NOT_COMMUNICATING", "active")
    assert "still dialling" in dialling

    assert "CONNECTED" in sim_model.link_state_sentence("COMMUNICATING", "passive")
    # An unmapped state must still render, not vanish.
    assert "WHATEVER" in sim_model.link_state_sentence("WHATEVER", "passive")


def test_neither_panel_joins_a_worker_on_the_ui_thread():
    """Joining a runner or a service teardown from a widget callback stops
    the window repainting for as long as the teardown takes - which looked
    like a hang at exactly the moment the user asked it to close."""
    for path in (
        ROOT / "simulator_gui" / "app.py",
        ROOT / "gui" / "app.py",
    ):
        source = path.read_text(encoding="utf-8")
        close = source[source.index("def _on_close"):]
        close = close[: close.index("\n    def ", 1)] if "\n    def " in close else close
        assert ".join(" not in close, f"{path.name} blocks the UI thread on close"
        # The replacement is a polled wait driven by the event loop.
        assert "_await" in close, path.name


# ----- the Linkstuffs/ThingsBoard endpoint -----

def test_base_url_default_is_the_server_origin():
    """The publisher appends /api/v1/<token>/<suffix>, so the template must
    ship the origin alone or every install starts with a doubled path.

    It must also ship https. The device token is *in the URL path*, so a
    plaintext origin puts a write credential on the wire in clear - and if
    the server redirects http->https, urllib re-issues the POST as a GET with
    the body dropped, the POST-only telemetry endpoint answers 405, and
    _post reads any 4xx as permanent. That combination dead-letters every
    telemetry row while the CSV files keep being written.
    """
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    base = service_config_from_dict(raw).linkstuffs_http.base_url

    assert base.startswith("https://"), (
        f"the shipped default must be an https origin; got {base!r}"
    )
    assert "/api/v1" not in base
    assert base.rstrip("/") == base, "no trailing slash - the suffix adds one"


def test_the_default_resolves_to_the_thingsboard_device_endpoint():
    """Pin the shape ThingsBoard's device HTTP API actually documents:
    POST /api/v1/{ACCESS_TOKEN}/telemetry."""
    import yaml
    from eap_middleware.config import service_config_from_dict
    from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    base = service_config_from_dict(raw).linkstuffs_http.base_url

    holder = type("H", (), {"config": type("C", (), {"base_url": base})()})()
    url = LinkstuffsHttpPublisher._url_for(holder, "TOKEN123", "telemetry")

    assert url == f"{base}/api/v1/TOKEN123/telemetry"
    assert url.startswith("https://"), (
        "the resolved endpoint must be https, or the token in its path is "
        "sent in clear"
    )


def test_a_full_endpoint_url_pasted_into_base_url_is_rejected():
    """The Linkstuffs UI shows the complete endpoint, token included, so
    that is what gets pasted. Appending to it 404s every publish while CSVs
    keep being written - which reads as a server fault, not a config one."""
    import yaml
    from eap_middleware.config import ConfigError, service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["linkstuffs_http"]["base_url"] = (
        "http://astar-monitoring.linkstuffs.com:8080/api/v1/SOMETOKEN/telemetry"
    )

    with pytest.raises(ConfigError) as caught:
        service_config_from_dict(raw)

    message = str(caught.value)
    assert "origin only" in message
    # It must hand back the corrected value rather than just complaining.
    assert "http://astar-monitoring.linkstuffs.com:8080" in message
    assert "Device token" in message


def test_a_machine_level_endpoint_paste_is_rejected_too():
    """Per-machine base_url overrides the global one and had the same trap."""
    import yaml
    from eap_middleware.config import ConfigError, service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"][0]["linkstuffs_http"] = {
        "base_url": "http://host:8080/api/v1/TOK/telemetry"
    }

    with pytest.raises(ConfigError, match="origin only"):
        service_config_from_dict(raw)


def test_the_device_token_never_appears_in_a_log_line():
    """Logs rotate on disk under C:/SECSGEM_EAP/logs, and the token is a
    bearer credential for writing telemetry."""
    from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher

    redacted = LinkstuffsHttpPublisher._redact(
        "http://astar-monitoring.linkstuffs.com:8080/api/v1/SECRETTOKEN/telemetry"
    )
    assert "SECRETTOKEN" not in redacted
    assert redacted.endswith("/api/v1/***/telemetry")


def test_no_live_device_token_is_committed_to_the_repo():
    """The template is a reviewed release artifact and the build refuses to
    package a modified one precisely so live credentials cannot ship."""
    template = (ROOT / "config" / "production.yaml").read_text(encoding="utf-8")
    import yaml

    tokens = yaml.safe_load(template)["linkstuffs_http"]["device_tokens"]
    assert all(not str(value).strip() for value in tokens.values()), tokens


# ----- the address dropdown finds the peer instead of being told it -----

def test_local_interfaces_carry_the_network_not_just_the_address():
    """Without a prefix length there is no way to know which addresses are
    on the same network as this machine, so nothing can be discovered."""
    for interface in netinfo.local_interfaces():
        assert 0 < interface.prefix_length <= 32, interface
        assert interface.network is not None
        assert ipaddress.IPv4Address(interface.address) in interface.network


def test_arp_parsing_handles_both_platform_formats():
    """Windows prints a three-column table per interface; BSD/macOS prints
    "? (10.0.0.1) at aa:bb:...". Scanning for IPv4 literals covers both."""
    windows = (
        "Interface: 192.168.102.132 --- 0xb\n"
        "  Internet Address      Physical Address      Type\n"
        "  192.168.102.1         a2-9a-8e-25-4e-65     dynamic\n"
        "  192.168.102.129       00-0c-29-68-47-32     dynamic\n"
    )
    macos = (
        "? (172.16.89.1) at a2:9a:8e:25:4e:65 on bridge101 [bridge]\n"
        "? (172.16.89.131) at 0:c:29:68:47:32 on bridge101 [bridge]\n"
    )
    assert "192.168.102.129" in netinfo.parse_arp_table(windows)
    assert "172.16.89.131" in netinfo.parse_arp_table(macos)


def test_arp_parsing_drops_what_is_not_a_peer():
    """Windows lists broadcast and multicast rows alongside real neighbours;
    offering 224.0.0.22 as an equipment address would be nonsense."""
    noisy = (
        "  192.168.102.129       00-0c-29-68-47-32     dynamic\n"
        "  192.168.102.255       ff-ff-ff-ff-ff-ff     static\n"
        "  224.0.0.22            01-00-5e-00-00-16     static\n"
        "  239.255.255.250       01-00-5e-7f-ff-fa     static\n"
        "  169.254.4.4           00-00-00-00-00-00     dynamic\n"
    )
    network = ipaddress.ip_network("192.168.102.0/24")

    found = netinfo.parse_arp_table(noisy, [network])

    assert found == ["192.168.102.129"], found


def test_a_stale_neighbour_off_our_networks_is_not_offered():
    """An entry for a network since disconnected is not reachable."""
    text = "  10.9.9.9   aa-bb-cc-dd-ee-ff   dynamic\n"
    assert netinfo.parse_arp_table(text) == ["10.9.9.9"]
    assert netinfo.parse_arp_table(
        text, [ipaddress.ip_network("192.168.102.0/24")]
    ) == []


def test_own_addresses_are_always_marked():
    """Pointing a machine at this PC's own address is the mistake that
    leaves a link permanently not connecting, with nothing to see."""
    hosts = netinfo.discover_hosts(port=5051, include_neighbours=False)
    mine = set(netinfo.local_ipv4_addresses())

    assert {h.address for h in hosts} == mine
    for host in hosts:
        assert host.is_self
        assert "this pc" in host.label


def test_a_peer_on_this_machine_says_both_things():
    """The single-box 'Both' install role puts the peer on this same PC, and
    saying only 'this pc' would hide what the scan just proved."""
    host = netinfo.DiscoveredHost(
        "10.0.0.5", is_self=True, listening=True, port=5051
    )
    assert "this pc" in host.label
    assert "listening on 5051" in host.label


def test_every_label_gives_its_address_back():
    """The label explains; the config must hold an address."""
    for host in (
        netinfo.DiscoveredHost("10.0.0.5", is_self=True),
        netinfo.DiscoveredHost("10.0.0.9", listening=True, port=5051),
        netinfo.DiscoveredHost("10.0.0.1", seen=True),
        netinfo.DiscoveredHost("10.0.0.2"),
    ):
        assert netinfo.address_from_choice(host.label) == host.address


def test_a_label_left_in_the_box_still_saves_as_an_address():
    """Selecting strips the note, but a pasted or hand-edited label must not
    reach production.yaml either."""
    assert gui_model.parse_value(
        "address", "10.0.0.9  (listening on 5051)"
    ) == "10.0.0.9"
    assert gui_model.parse_value("address", "  10.0.0.9  ") == "10.0.0.9"
    assert gui_model.parse_value("address", "") == ""


def test_a_scan_refuses_a_network_too_big_to_be_a_rig():
    """A /8 is somebody's corporate LAN, not a local segment. Sweeping it would
    take minutes and generate traffic nobody asked for."""
    huge = ipaddress.ip_network("10.0.0.0/8")

    assert netinfo.scan_for_listeners([huge], 5051, timeout=0.01) == []


def test_a_scan_finds_a_real_listener():
    """The point of the whole feature: name the peer without being told."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)
    try:
        assert netinfo._port_open("127.0.0.1", port, 0.5)
        server.close()
        assert not netinfo._port_open("127.0.0.1", port, 0.2)
    finally:
        server.close()


def test_a_scan_reports_only_the_hosts_that_answered(monkeypatch):
    """Sweep logic, without depending on what happens to be on the LAN."""
    # .5 is a host of this /29; .0 and .7 are network and broadcast.
    answering = {"192.0.2.5"}
    monkeypatch.setattr(
        netinfo, "_port_open",
        lambda address, port, timeout: address in answering,
    )

    found = netinfo.scan_for_listeners(
        [ipaddress.ip_network("192.0.2.0/29")], 5051
    )

    assert found == ["192.0.2.5"]


def test_a_scan_can_be_cancelled_promptly(monkeypatch):
    """Queued work cannot be cancelled once a worker picks it up, and the
    executor joins every running task on exit - so submitting the whole
    sweep up front made "stop" wait for all of it."""
    import time as _time

    probed = []

    def slow_probe(address, port, timeout):
        probed.append(address)
        _time.sleep(0.05)
        return False

    monkeypatch.setattr(netinfo, "_port_open", slow_probe)

    started = _time.perf_counter()
    stopped = netinfo.scan_for_listeners(
        [ipaddress.ip_network("192.0.2.0/24")], 5051,
        should_stop=lambda: True,
    )
    elapsed = _time.perf_counter() - started

    assert stopped == []
    assert not probed, "nothing should be probed when stopped up front"
    assert elapsed < 1.0, f"cancellation took {elapsed:.2f}s"


# ----- the settings whose names do not carry their meaning -----

def test_runtime_mode_is_explained_in_the_form():
    """'simulated' runs a simulator INSIDE the middleware and ignores
    host/port. It reads as 'this machine is a simulator', which is how a
    two-machine installation ends up pointed at nothing."""
    help_text = gui_model.FIELD_HELP["runtime_mode"]

    assert "INSIDE the middleware" in help_text
    assert "ignore host/port" in help_text
    assert "two-machine installation, " in help_text
    assert "use real" in help_text


def test_every_explained_field_actually_exists():
    """A help line keyed to a renamed field silently disappears."""
    known = {path for path, _label, _kind in gui_model.MACHINE_FIELDS}
    known |= {path for path, _label, _kind in gui_model.HTTP_FIELDS}
    for path in gui_model.FIELD_HELP:
        assert path in known, path


def test_simulated_machines_are_caught_before_the_service_starts(monkeypatch):
    """A middleware-only install carries no simulator, so such a machine
    fails at start with an import error, one machine at a time, in a log."""
    import builtins
    import sys as _sys
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"] = raw["machines"][:1]
    raw["machines"][0].update(
        enabled=True, offline_test_mode=True, runtime_mode="simulated"
    )
    config = service_config_from_dict(raw)

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "simulator" or name.startswith("simulator."):
            raise ImportError("No module named 'simulator'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(_sys.modules, "simulator", raising=False)

    assert gui_model.simulator_unavailable_machines(config) == ["TOOL_01"]


def test_a_real_machine_is_never_flagged_as_needing_a_simulator():
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["machines"] = raw["machines"][:1]
    raw["machines"][0].update(
        enabled=True, offline_test_mode=True, runtime_mode="real"
    )

    assert gui_model.simulator_unavailable_machines(
        service_config_from_dict(raw)
    ) == []


# ----- the service must be able to write where it is told to -----

def test_unwritable_service_folders_are_named(tmp_path):
    """sqlite3 says "unable to open database file" and names neither the
    file nor the cause. The installer creates these as Administrator; a
    panel started normally is not elevated."""
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    _deny_writes(blocked)
    raw["paths"] = {
        **raw["paths"],
        "data_dir": str(blocked / "data"),
        "control_dir": str(blocked / "control"),
        "log_dir": str(tmp_path / "log"),
        "outbox_db": str(tmp_path / "o.sqlite3"),
        "http_outbox_db": str(tmp_path / "h.sqlite3"),
        "legacy_api_outbox_db": str(tmp_path / "l.sqlite3"),
    }
    try:
        problems = gui_model.writable_path_problems(
            service_config_from_dict(raw)
        )
        assert any("control" in line for line in problems), problems
        assert any("status and command files" in line for line in problems)
    finally:
        blocked.chmod(0o700)


def test_writable_folders_report_no_problem(tmp_path):
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    raw["paths"] = {
        **raw["paths"],
        "data_dir": str(tmp_path / "data"),
        "log_dir": str(tmp_path / "log"),
        "outbox_db": str(tmp_path / "o.sqlite3"),
        "http_outbox_db": str(tmp_path / "h.sqlite3"),
        "legacy_api_outbox_db": str(tmp_path / "l.sqlite3"),
    }

    assert gui_model.writable_path_problems(service_config_from_dict(raw)) == []


def test_the_template_csv_path_is_writable_on_a_stock_vm():
    """D: is often a read-only optical drive on Windows, and csv_store does not
    wrap the local mkdir - so every event failed with WinError 5 and no CSV
    was ever written."""
    import yaml

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    for machine in raw["machines"]:
        local = machine["storage"]["local_csv_path"]
        assert not local.upper().startswith("D:"), local
        assert local.startswith("C:/SECSGEM_EAP"), local
        # An unreachable UNC mirror is noise on a site that has no fileserver.
        assert machine["storage"]["network_csv_path"] == ""


def test_the_ingress_journal_follows_the_configured_data_dir():
    """Its default is a hardcoded C:/SECSGEM_EAP path that does not follow
    data_dir, so relocating the data directory left it pointing at the old
    location and the service died with "unable to open database file" -
    naming neither the file nor the reason."""
    import yaml

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())

    assert "ingress_journal_db" in raw["paths"], (
        "absent from the template, so it silently keeps its hardcoded default"
    )


def test_every_database_the_service_opens_is_preflighted(tmp_path):
    """One missed path is a start failure with an opaque sqlite3 message."""
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    _deny_writes(unwritable)
    try:
        for key in (
            "outbox_db", "http_outbox_db",
            "legacy_api_outbox_db", "ingress_journal_db",
        ):
            paths = {
                **raw["paths"],
                "install_dir": str(tmp_path),
                "data_dir": str(tmp_path / "data"),
                "log_dir": str(tmp_path / "log"),
                "outbox_db": str(tmp_path / "o.sqlite3"),
                "http_outbox_db": str(tmp_path / "h.sqlite3"),
                "legacy_api_outbox_db": str(tmp_path / "l.sqlite3"),
                "ingress_journal_db": str(tmp_path / "j.sqlite3"),
            }
            paths[key] = str(unwritable / "sub" / "db.sqlite3")
            config = service_config_from_dict({**raw, "paths": paths})

            problems = gui_model.writable_path_problems(config)

            assert problems, f"{key} is not preflighted"
    finally:
        unwritable.chmod(0o700)


def test_the_instance_lock_location_is_preflighted(tmp_path):
    """middleware.lock is written directly into install_dir, not into any
    of the subfolders the installer grants access to."""
    import yaml
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    locked = tmp_path / "root"
    locked.mkdir()
    _deny_writes(locked)
    try:
        config = service_config_from_dict({
            **raw,
            "paths": {
                **raw["paths"],
                "install_dir": str(locked),
                "data_dir": str(tmp_path / "data"),
                "log_dir": str(tmp_path / "log"),
                "outbox_db": str(tmp_path / "o.sqlite3"),
                "http_outbox_db": str(tmp_path / "h.sqlite3"),
                "legacy_api_outbox_db": str(tmp_path / "l.sqlite3"),
                "ingress_journal_db": str(tmp_path / "j.sqlite3"),
            },
        })

        problems = gui_model.writable_path_problems(config)

        assert any("single-instance lock" in line for line in problems), problems
    finally:
        locked.chmod(0o700)


def test_a_failed_start_releases_the_single_instance_lock():
    """start() takes the lock before anything else, so a failure after that
    point left it held with no owner - and every later attempt then failed
    on the lock instead of the original cause."""
    source = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")

    run = source[source.index("    def _on_run_service"):]
    run = run[: run.index("    def _on_stop_service")]
    failure = run[run.index("except Exception as exc:"):]

    assert "service.stop()" in failure, "a half-started service is not unwound"


def test_the_scan_worker_reports_its_own_failure():
    """A dead worker left the previous result in place and said nothing."""
    source = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")

    scan = source[source.index("    def _on_scan_network"):]
    scan = scan[: scan.index("    def _poll_scan")]

    assert "except Exception as exc:" in scan
    assert "search failed" in scan


def test_setup_resolves_python_after_installing_it():
    """The setup window starts before install.ps1 puts Python on the PATH,
    and a running process does not inherit later PATH changes - so
    "Open the control panel" could not find pythonw.exe on a first install."""
    setup = (ROOT / "deploy" / "Setup.ps1").read_text(encoding="utf-8")

    assert 'GetEnvironmentVariable("Path", "Machine")' in setup
    assert "Get-Command pythonw.exe" in setup
    assert r"Program Files\Python*" in setup


def test_setup_reads_the_log_once_more_after_the_process_exits():
    """The install writes the lines saying why it failed between the last
    tick and the process ending; stopping the timer dropped exactly those."""
    setup = (ROOT / "deploy" / "Setup.ps1").read_text(encoding="utf-8")

    tick = setup[setup.index("$timer.Add_Tick({"):]
    after_stop = tick[tick.index("$timer.Stop()"):]

    assert "Read-NewLogLines" in after_stop


def test_icacls_cannot_abort_the_install():
    """$ErrorActionPreference is Stop for this script, and a native command
    writing to stderr under 2>&1 becomes a TERMINATING error. icacls writes
    to stderr for every file it skips - which /C tells it to keep doing."""
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    grant = install[install.index("Establishing the ASTAR operator trust boundary"):]
    grant = grant[: grant.index("# 3) Copy source code")]

    assert '$ErrorActionPreference = "Continue"' in grant
    assert "finally {" in grant
    assert "$previousPreference" in grant
