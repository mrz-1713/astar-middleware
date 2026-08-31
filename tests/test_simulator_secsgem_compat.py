"""Regression tests for version-pinned secsgem lifecycle fixes."""

from __future__ import annotations

import socket
import struct
import time

import secsgem.hsms
from secsgem.hsms.connection_state_machine import ConnectionState

from gateway.secsgem_compat import install_secsgem_030_thread_cleanup


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _recv_exact(peer: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = peer.recv(size - len(data))
        assert chunk
        data.extend(chunk)
    return bytes(data)


def _receive_block(peer: socket.socket) -> secsgem.hsms.HsmsBlock:
    length_data = _recv_exact(peer, 4)
    remaining = struct.unpack(">L", length_data)[0]
    payload = _recv_exact(peer, remaining)
    block = secsgem.hsms.HsmsBlock.decode(length_data + payload)
    assert block is not None
    return block


def test_fast_select_request_waits_for_passive_connected_state() -> None:
    """An immediate SelectReq must not race the passive TCP callback."""
    install_secsgem_030_thread_cleanup()
    settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=_free_port(),
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    protocol = secsgem.hsms.HsmsProtocol(settings)

    # Widen the vulnerable interval deterministically. With upstream ordering,
    # the TCP receiver dispatches SelectReq during this pause while the HSMS
    # state is NOT_CONNECTED. The compatibility fix does not start receiving
    # until this callback has advanced the state.
    def delayed_connected(_data: dict[str, object]) -> None:
        protocol._connected = True
        protocol._thread.start()
        time.sleep(0.1)
        protocol._connection_state.connect()
        protocol.events.fire("connected", {"connection": protocol})

    protocol._on_connected = delayed_connected
    peer: socket.socket | None = None
    try:
        protocol.enable()
        peer = socket.create_connection(
            (settings.address, settings.port), timeout=2
        )
        peer.settimeout(2)
        request = secsgem.hsms.HsmsMessage(
            secsgem.hsms.HsmsSelectReqHeader(123), b""
        )
        peer.sendall(request.blocks[0].encode())

        response = _receive_block(peer)
        assert response.header.s_type == secsgem.hsms.HsmsSType.SELECT_RSP
        deadline = time.monotonic() + 2
        while (
            time.monotonic() < deadline
            and protocol.connection_state.current
            != ConnectionState.CONNECTED_SELECTED
        ):
            time.sleep(0.01)
        assert (
            protocol.connection_state.current
            == ConnectionState.CONNECTED_SELECTED
        )
    finally:
        try:
            protocol.disable()
        finally:
            if peer is not None:
                peer.close()
