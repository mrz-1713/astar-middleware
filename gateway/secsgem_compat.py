"""Narrow compatibility fixes for the application's pinned secsgem 0.3.0."""

from __future__ import annotations

import logging
import select
import socket
import threading
from importlib.metadata import PackageNotFoundError, version
from typing import Any

logger = logging.getLogger(__name__)
_PATCH_LOCK = threading.Lock()


def install_secsgem_030_thread_cleanup() -> None:
    """Fix pinned secsgem dispatcher and passive-listener races.

    secsgem 0.3.0 only signals and joins ``_receiver_thread``. Its separate
    ``_dispatcher_thread`` remains blocked forever and a reconnect overwrites
    the sole reference to it. Its passive listener also starts receiving data
    before it fires the TCP-connected callback, allowing an immediate HSMS
    Select Request to be dispatched while the protocol is still in
    ``NOT_CONNECTED``. Apply version-pinned process-wide fixes before any
    handlers are constructed. Future versions are left untouched and must be
    evaluated independently.
    """
    try:
        if version("secsgem") != "0.3.0":
            return
    except PackageNotFoundError:
        return

    from secsgem.common.protocol_dispatcher import ProtocolDispatcher
    from secsgem.common.helpers import is_windows
    from secsgem.common.tcp_server_connection import TcpServerConnection

    with _PATCH_LOCK:
        if not getattr(ProtocolDispatcher.stop, "_astar_cleanup_patch", False):

            def stop(self: Any) -> None:
                receiver = getattr(self, "_receiver_thread", None)
                dispatcher = getattr(self, "_dispatcher_thread", None)

                self._stop_receiver_thread = True
                self._stop_dispatcher_thread = True
                self._receiver_thread_trigger.set()
                self._dispatcher_thread_trigger.set()

                current = threading.current_thread()
                for worker in (receiver, dispatcher):
                    if (
                        worker is not None
                        and worker is not current
                        and worker.is_alive()
                    ):
                        worker.join(timeout=5.0)
                        if worker.is_alive():
                            logger.warning(
                                "secsgem dispatcher worker did not stop: %s",
                                worker.name,
                            )

            stop._astar_cleanup_patch = True  # type: ignore[attr-defined]
            ProtocolDispatcher.stop = stop

        server_method_name = "_TcpServerConnection__server_thread"
        server_thread = getattr(TcpServerConnection, server_method_name)
        if not getattr(server_thread, "_astar_cleanup_patch", False):

            def safe_server_thread(self: Any) -> None:
                """Accept one peer without exposing data before on_connected."""
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_sock = listener
                try:
                    if not is_windows():
                        listener.setsockopt(
                            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
                        )
                    listener.bind(
                        (self._settings.address, self._settings.port)
                    )
                    listener.listen(1)

                    while not self._stop_server_thread:
                        try:
                            readable = select.select(
                                [listener], [], [], self.select_timeout
                            )[0]
                        except (OSError, ValueError) as exc:
                            if self._stop_server_thread:
                                break
                            self._logger.debug(
                                "select exception", exc_info=exc
                            )
                            raise
                        if not readable:
                            continue

                        try:
                            accepted, _address = listener.accept()
                        except OSError:
                            if self._stop_server_thread:
                                break
                            raise
                        self._sock = accepted
                        self._socket.setsockopt(
                            socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1
                        )
                        self._socket.setblocking(False)
                        self._connected = True

                        # The upstream implementation starts the receiver
                        # first. A fast peer can then deliver SelectReq before
                        # HsmsProtocol._on_connected advances its state. Fire
                        # the callback first; buffered TCP data is read as soon
                        # as the receiver starts immediately afterward.
                        try:
                            self.on_connected({"source": self})
                        except Exception:
                            self._logger.exception(
                                "ignoring exception for "
                                "on_connection_established handler"
                            )
                        self._start_receiver()
                        return
                finally:
                    try:
                        listener.close()
                    except OSError:
                        pass
                    self._server_sock = None
                    if self._stop_server_thread:
                        self._stop_server_thread = False

            safe_server_thread._astar_cleanup_patch = True  # type: ignore[attr-defined]
            setattr(
                TcpServerConnection, server_method_name, safe_server_thread
            )


def prepare_secsgem_030_passive_shutdown(handler: Any, context: str) -> None:
    """Stop a pinned secsgem passive listener before its unsafe close path.

    secsgem 0.3.0 can close the listening socket while its server thread is
    returning from ``select()``, causing an uncaught ``OSError`` on shutdown.
    Stop and join that thread first. Active connections do not use it.
    """
    try:
        if version("secsgem") != "0.3.0" or handler.settings.is_active:
            return
    except PackageNotFoundError:
        return

    try:
        connection = handler.protocol._connection
        server_thread = getattr(connection, "_server_thread", None)
        if server_thread is None or not server_thread.is_alive():
            return
        connection._enabled = False
        connection._stop_server_thread = True
        server_thread.join(timeout=2.0)
        if server_thread.is_alive():
            logger.warning(
                "[%s] secsgem passive listener did not stop promptly", context
            )
        server_socket = getattr(connection, "_server_sock", None)
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass
    except Exception:
        logger.debug(
            "[%s] secsgem passive listener pre-stop failed",
            context,
            exc_info=True,
        )
