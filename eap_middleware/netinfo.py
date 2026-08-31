"""Local IPv4 discovery and firewall helpers for the setup panels.

An operator otherwise reads an address off `ipconfig` and retypes it into a
form on another machine, which is where a two-machine installation
acquires its typos. Both control panels offer a pick-list instead, and both build their
firewall command from the same place so the rule they open is the port they
are actually listening on.

Pure stdlib on purpose: the offline deploy package installs no networking
dependency, and this has to work before anything else does.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import ipaddress
import json
import re
import socket
import subprocess
import sys
from typing import Callable, List, Optional, Sequence, Tuple

# UI label for an explicit operator bind choice, not an implicit listener.
ALL_INTERFACES = "0.0.0.0"  # nosec B104
ALL_INTERFACES_LABEL = "0.0.0.0 - every network adapter on this machine"

# Probing an address never sends a packet: connect() on a UDP socket only
# fixes the peer, which is enough for the OS to reveal which local interface
# it would route through. TEST-NET-1 is unallocated, so this cannot leak
# traffic even if a route does exist.
_PROBE_TARGETS: Tuple[Tuple[str, int], ...] = (
    ("192.0.2.1", 9),
    ("8.8.8.8", 53),
)


def _is_usable(address: str) -> bool:
    """Reject addresses never worth offering as a bind target or a peer.

    ipaddress rather than string prefixes because an ARP table also carries
    broadcast and multicast rows - Windows `arp -a` lists 224.0.0.22 and
    x.x.x.255 alongside real neighbours, and offering those as a peer would
    be nonsense.
    """
    if not address:
        return False
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if (
        parsed.is_loopback
        or parsed.is_multicast
        or parsed.is_unspecified      # 0.0.0.0
        or parsed.is_reserved
    ):
        return False
    # 169.254/16 means DHCP failed. Binding there produces a listener no peer
    # can reach, and the failure looks like a firewall problem for an hour.
    if parsed.is_link_local:
        return False
    return address != "255.255.255.255"


def primary_ipv4() -> str:
    """The address a peer on the same LAN would reach this machine on.

    Returns "" on an isolated host with no route, which the callers render
    as "no network adapter found" rather than guessing.
    """
    for target in _PROBE_TARGETS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(target)
            address = str(probe.getsockname()[0])
        except OSError:
            continue
        finally:
            probe.close()
        if _is_usable(address):
            return address
    return ""


def local_ipv4_addresses() -> List[str]:
    """Every usable local IPv4, the routable one first.

    The primary comes first because a Windows host often has several
    adapters present and only one of them is the one the peer machine
    can see.
    """
    primary = primary_ipv4()
    others: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if _is_usable(address) and address != primary and address not in others:
                others.append(address)
    except (OSError, UnicodeError):
        # An unresolvable hostname is normal on a freshly imaged host; the
        # probe result above is still good.
        pass
    # getaddrinfo does not promise a stable order, and a pick-list that
    # reshuffles between launches is one an operator stops trusting.
    others.sort()
    return ([primary] if primary else []) + others


def bind_address_choices() -> List[str]:
    """Values for a passive listener's address box.

    0.0.0.0 leads because it is the choice that cannot be wrong, and a
    simulator that binds the wrong one of two adapters is the single most
    common reason a new installation never connects.
    """
    return [ALL_INTERFACES, *local_ipv4_addresses()]


def peer_address_choices() -> List[str]:
    """Values for an active dialler's address box.

    Local addresses are offered because the peer is usually the other
    machine the operator just set up, but 0.0.0.0 is never a destination.
    """
    return local_ipv4_addresses()


def reachable_address(bind_address: str) -> str:
    """What to tell the peer to connect to, given what we bound.

    0.0.0.0 is a correct bind and a meaningless destination, so resolve it
    to something the operator can actually type on the other machine.
    """
    if bind_address and bind_address != ALL_INTERFACES:
        return bind_address
    return primary_ipv4() or ALL_INTERFACES


def firewall_rule_name(port: int) -> str:
    return f"ASTAR SECS-GEM inbound {int(port)}"


def firewall_command(port: int) -> str:
    """The PowerShell that opens an inbound TCP port, idempotently.

    Built here rather than in the panel so the rule always names the port
    the panel is about to listen on, and so it can be asserted in a test
    without a display.
    """
    name = firewall_rule_name(port)
    return (
        f"$n='{name}';"
        "if (Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)"
        " { Remove-NetFirewallRule -DisplayName $n };"
        f"New-NetFirewallRule -DisplayName $n -Direction Inbound -Action Allow"
        f" -Protocol TCP -LocalPort {int(port)} -Profile Any | Out-Null;"
        f"Write-Host 'Opened inbound TCP {int(port)}.'"
    )


def elevated_powershell_argv(command: str) -> Sequence[str]:
    """Run `command` in a PowerShell that prompts for admin via UAC.

    Adding a firewall rule needs elevation, and a panel launched from a
    desktop shortcut has none. Re-launching through Start-Process -Verb
    RunAs puts the consent prompt in front of the operator instead of
    failing with an access-denied buried in a log pane.
    """
    inner = command.replace("'", "''")
    return (
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        # -PassThru + $p.ExitCode: without propagating the elevated child's
        # exit code, the outer powershell returns 0 even when the rule was
        # never added, so the panel reported success for a failure.
        f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
        f"'-Command','{inner}'; exit $p.ExitCode",
    )


# ---------------------------------------------------------------------------
# Network discovery
#
# An operator picking a peer address should not have to know it. These build
# the pick-list: which networks this machine is on, which addresses are its
# own, and - when asked - which host on those networks is actually listening
# on the HSMS port. On a two-machine setup that last one names the peer
# outright.
# ---------------------------------------------------------------------------

# Anything wider than this is not a local segment, it is someone's
# corporate LAN, and
# sweeping it would take minutes and generate noise nobody asked for.
MAX_SCAN_HOSTS = 1024
SCAN_TIMEOUT_SEC = 0.35
SCAN_WORKERS = 64

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _run(command: Sequence[str], timeout: float = 6.0) -> str:
    """Run a helper command, returning "" rather than raising.

    Discovery is a convenience: every failure here degrades the pick-list,
    and none of them should surface as an error in a panel.
    """
    kwargs = {}
    if sys.platform == "win32":
        # Without this every probe flashes a console window over the panel.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            list(command), capture_output=True, text=True,
            timeout=timeout, **kwargs
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


@dataclasses.dataclass(frozen=True)
class LocalInterface:
    """One local IPv4 and the network it sits on."""

    address: str
    prefix_length: int
    name: str = ""

    @property
    def network(self) -> Optional[ipaddress.IPv4Network]:
        try:
            network = ipaddress.ip_interface(
                f"{self.address}/{self.prefix_length}"
            ).network
            return network if isinstance(network, ipaddress.IPv4Network) else None
        except ValueError:
            return None


def _windows_interfaces() -> List[LocalInterface]:
    """Get-NetIPAddress is the only source on Windows that carries a mask.

    socket alone cannot report a prefix length, and without one there is no
    way to know which addresses are on the same network as this machine.
    """
    raw = _run([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Select-Object IPAddress,PrefixLength,InterfaceAlias | "
        "ConvertTo-Json -Compress",
    ])
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    # ConvertTo-Json emits a bare object when there is exactly one result.
    entries = parsed if isinstance(parsed, list) else [parsed]
    found: List[LocalInterface] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = str(entry.get("IPAddress", ""))
        try:
            prefix = int(entry.get("PrefixLength", 0))
        except (TypeError, ValueError):
            continue
        if _is_usable(address) and 0 < prefix <= 32:
            found.append(
                LocalInterface(address, prefix, str(entry.get("InterfaceAlias", "")))
            )
    return found


def _posix_interfaces() -> List[LocalInterface]:
    """Linux `ip -o -4 addr` gives CIDR; macOS `ifconfig` gives a hex mask."""
    found: List[LocalInterface] = []
    raw = _run(["ip", "-o", "-4", "addr", "show"])
    if raw:
        for line in raw.splitlines():
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if match and _is_usable(match.group(1)):
                parts = line.split()
                name = parts[1] if len(parts) > 1 else ""
                found.append(
                    LocalInterface(match.group(1), int(match.group(2)), name)
                )
        if found:
            return found
    raw = _run(["ifconfig"])
    name = ""
    for line in raw.splitlines():
        header = re.match(r"^(\S+):", line)
        if header:
            name = header.group(1)
        match = re.search(
            r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)",
            line,
        )
        if not match or not _is_usable(match.group(1)):
            continue
        mask = match.group(2)
        try:
            bits = int(mask, 16) if mask.startswith("0x") else int(
                ipaddress.IPv4Address(mask)
            )
        except ValueError:
            continue
        found.append(LocalInterface(match.group(1), bin(bits).count("1"), name))
    return found


def local_interfaces() -> List[LocalInterface]:
    """Every usable local IPv4 with the network it belongs to."""
    found = (
        _windows_interfaces() if sys.platform == "win32" else _posix_interfaces()
    )
    if found:
        return found
    # No mask source: assume the common /24 so discovery still works.
    return [LocalInterface(address, 24) for address in local_ipv4_addresses()]


def parse_arp_table(
    text: str,
    networks: Sequence[ipaddress.IPv4Network] = (),
) -> List[str]:
    """Pull neighbour addresses out of `arp -a` output.

    Kept pure and format-agnostic: Windows prints a bare three-column table
    per interface, BSD/macOS prints "? (10.0.0.1) at aa:bb:...". Scanning
    for IPv4 literals handles both without parsing either layout, and the
    filtering below drops the broadcast and multicast rows Windows includes.

    `networks` optionally restricts the result to networks this machine is
    actually on, so a stale entry for a network since disconnected is not
    offered as a reachable peer.
    """
    found = [
        address for address in dict.fromkeys(_IPV4_RE.findall(text))
        if _is_usable(address)
    ]
    if not networks:
        return found
    kept: List[str] = []
    for address in found:
        parsed = ipaddress.IPv4Address(address)
        for network in networks:
            if parsed in network:
                # A network's own broadcast address answers ARP but is not
                # a host anybody can connect to.
                if parsed != network.broadcast_address:
                    kept.append(address)
                break
    return kept


def arp_neighbours(
    networks: Sequence[ipaddress.IPv4Network] = (),
) -> List[str]:
    """Addresses this machine has recently exchanged traffic with.

    Free and instant - the OS already has them - and on a small network it
    usually names the peer without scanning anything.
    """
    return parse_arp_table(_run(["arp", "-a"]), networks)


def _port_open(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_for_listeners(
    networks: Sequence[ipaddress.IPv4Network],
    port: int,
    timeout: float = SCAN_TIMEOUT_SEC,
    max_hosts: int = MAX_SCAN_HOSTS,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """Which hosts on these networks accept a TCP connection on `port`.

    Deliberately narrow: one port, only on networks this machine is directly
    attached to, and capped at max_hosts. That is enough to find the peer on
    a local segment and is not a general-purpose network sweep.
    """
    targets: List[str] = []
    for network in networks:
        if network.num_addresses - 2 > max_hosts:
            continue
        targets.extend(
            str(host) for host in network.hosts() if _is_usable(str(host))
        )
    targets = list(dict.fromkeys(targets))[:max_hosts]
    if not targets:
        return []

    listening: List[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        # Submitted a batch at a time rather than all at once. Queued work
        # cannot be cancelled once a worker picks it up, and the executor
        # joins every running task on exit - so submitting 254 probes up
        # front made "stop" wait for all of them. One batch bounds the
        # cancellation delay to a single connect timeout.
        for start in range(0, len(targets), SCAN_WORKERS):
            if should_stop is not None and should_stop():
                break
            batch = targets[start : start + SCAN_WORKERS]
            futures = {
                pool.submit(_port_open, address, port, timeout): address
                for address in batch
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    if future.result():
                        listening.append(futures[future])
                except Exception:
                    continue
    return sorted(listening, key=lambda a: ipaddress.IPv4Address(a))


@dataclasses.dataclass(frozen=True)
class DiscoveredHost:
    """One address the panel can offer, and why it is being offered."""

    address: str
    is_self: bool = False
    listening: bool = False
    seen: bool = False
    port: int = 0

    @property
    def note(self) -> str:
        # Both can be true when the peer runs on this same box - the
        # single-machine "Both" install role - and saying only "this pc"
        # there would hide the very thing the scan just proved.
        if self.is_self and self.listening:
            return f"this pc - listening on {self.port}"
        if self.is_self:
            return "this pc"
        if self.listening:
            return f"listening on {self.port}"
        if self.seen:
            return "on this network"
        return ""

    @property
    def label(self) -> str:
        """What the dropdown shows. The address stays first so the value can
        always be recovered by taking the first token."""
        note = self.note
        return f"{self.address}  ({note})" if note else self.address


def discover_hosts(
    port: int = 0,
    listeners: Sequence[str] = (),
    include_neighbours: bool = True,
) -> List[DiscoveredHost]:
    """Everything worth offering in an address dropdown.

    `listeners` comes from a scan the caller ran (scanning blocks, so the
    panel owns when that happens). Own addresses always come first and are
    always marked, because picking one by mistake is the error that makes a
    link silently never connect.
    """
    mine = local_ipv4_addresses()
    heard = set(listeners)
    hosts: List[DiscoveredHost] = [
        DiscoveredHost(
            address, is_self=True, listening=address in heard, port=port
        )
        for address in mine
    ]
    known = set(mine)
    for address in listeners:
        if address not in known:
            hosts.append(DiscoveredHost(address, listening=True, port=port))
            known.add(address)
    if include_neighbours:
        networks = [i.network for i in local_interfaces() if i.network]
        for address in arp_neighbours(networks):
            if address not in known:
                hosts.append(DiscoveredHost(address, seen=True))
                known.add(address)
    return hosts


def address_from_choice(text: str) -> str:
    """Recover the bare address from a dropdown label.

    The label carries an explanation, but the config must hold an address.
    Applied on selection and again on save, so a label can never be written
    to the file.
    """
    return str(text or "").strip().split()[0] if str(text or "").strip() else ""
