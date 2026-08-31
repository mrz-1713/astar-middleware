"""One-shot HSMS reachability check, shared by the CLI and the control panel.

`test-machine` on the command line and "Test connection" in the panel have
to agree. An operator who gets secs-ok from one and a failure from the
other has no way to tell which is lying, so both call this.

The panel additionally needs a check that works with no Windows service
installed: on a fresh install the service does not exist yet, and a
control panel that can only ask a service to test something can verify
nothing at all.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 5.0


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """The outcome of a single S1F1/S1F2 identity exchange."""

    endpoint_id: str
    host: str
    port: int
    device_id: int
    ok: bool
    identity: Optional[Any] = None
    error: str = ""

    def as_line(self) -> str:
        """The exact line `test-machine` has always printed.

        Docs quote this verbatim as expected output, so the format is part
        of the contract, not a detail of the CLI.
        """
        where = f"{self.endpoint_id} {self.host}:{self.port}"
        if self.ok:
            return (
                f"secs-ok: {where} device_id={self.device_id} "
                f"identity={self.identity!r}"
            )
        return f"secs-fail: {where} {self.error}"


def probe_machine(
    machine: Any, timeout: float = DEFAULT_TIMEOUT_SEC
) -> ProbeResult:
    """Bring one HSMS link up, ask who is there, and tear it down.

    Never raises: every failure mode an operator can cause (wrong IP, port
    closed, firewall, peer in the same HSMS mode) arrives as a ProbeResult
    with ok=False, because both callers want to report it rather than
    crash on it.
    """
    host = None
    try:
        # Imported here, not at module scope: gateway pulls in secsgem, and
        # this module is imported by the panel on machines where a broken
        # secsgem install should still leave the panel usable.
        from gateway.host import GatewayHost, create_host_settings

        settings = create_host_settings(
            host=machine.host,
            port=machine.port,
            device_id=machine.secs_device_id,
            mode=machine.hsms_mode,
            bind_address=machine.hsms_bind_address,
            # ProbeTarget (the panel's connection-only view) has no timers;
            # a full MachineConfig does. Probing with different timers from
            # the ones the service will use would prove the wrong thing.
            timers=getattr(machine, "hsms_timers", None),
        )
        host = GatewayHost(settings=settings, tool_id=machine.endpoint_id)
        host.enable()
        if not host.waitfor_communicating(timeout=timeout):
            raise TimeoutError("HSMS connected but did not reach COMMUNICATING")
        response = host.are_you_there()
        if response is None:
            raise TimeoutError("S1F1 identity request received no S1F2")
        identity = host.settings.streams_functions.decode(response).get()
        return ProbeResult(
            endpoint_id=machine.endpoint_id,
            host=machine.host,
            port=machine.port,
            device_id=machine.secs_device_id,
            ok=True,
            identity=identity,
        )
    except Exception as exc:
        # Some secsgem/socket errors carry an empty message, which rendered
        # as "secs-fail: TOOL_02 10.0.0.4:5000 " - a failure with no reason.
        return ProbeResult(
            endpoint_id=machine.endpoint_id,
            host=machine.host,
            port=machine.port,
            device_id=machine.secs_device_id,
            ok=False,
            error=str(exc).strip() or type(exc).__name__,
        )
    finally:
        if host is not None:
            try:
                host.disable()
            except Exception as exc:
                # Not just `pass`: disable() is the one teardown call the
                # runtime itself distrusts (secsgem can leave the socket open),
                # so a probe that fails to close its link must say so rather
                # than report secs-ok while its socket occupies the tool's only
                # HSMS peer slot.
                logger.warning(
                    "Probe teardown for %s failed; the HSMS connection may "
                    "still be open and could block the next connect: %s",
                    machine.endpoint_id, exc,
                )


def probe_machines(
    machines: Sequence[Any], timeout: float = DEFAULT_TIMEOUT_SEC
) -> List[ProbeResult]:
    return [probe_machine(machine, timeout) for machine in machines]
