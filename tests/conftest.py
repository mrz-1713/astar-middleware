"""pytest session configuration.

Patches secsgem's TcpServerConnection at import time to swallow the
OSError(57) that fires during test teardown when the passive-mode server
socket is already closed before secsgem's server thread calls shutdown().
This is an upstream secsgem bug (no try/except around the SHUT_RDWR call
in tcp_server_connection.py line ~160).
"""

from __future__ import annotations

import socket
import threading
from typing import Any


def _patch_secsgem_server_shutdown() -> None:
    """Replace TcpServerConnection's private server thread with a version
    that wraps socket.shutdown() in a try/except so OSError(57) on teardown
    doesn't leak as an unhandled thread exception."""
    try:
        import secsgem.common.tcp_server_connection as _mod
    except ImportError:
        return  # secsgem not installed; passive-mode tests will skip

    cls = _mod.TcpServerConnection
    def _safe_server_thread(self: Any) -> None:
        import select as _select

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        from secsgem.common.tcp_server_connection import is_windows  # type: ignore[attr-defined]
        if not is_windows():
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self._server_sock.bind((self._settings.address, self._settings.port))
        self._server_sock.listen(1)

        while not self._stop_server_thread:
            try:
                select_result = _select.select(
                    [self._server_sock], [], [], self.select_timeout
                )
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.debug("select exception", exc_info=exc)
                break

            if not select_result[0]:
                continue

            accept_result = self._server_sock.accept()
            if accept_result is None:
                continue

            (self._sock, (_, _)) = accept_result
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._socket.setblocking(0)
            self._connected = True
            self._start_receiver()

            try:
                self.on_connected({"source": self})
            except Exception:  # pylint: disable=broad-except
                self._logger.exception(
                    "ignoring exception for on_connection_established handler"
                )

            # upstream bug: socket may already be disconnected at this point
            # during test teardown — swallow OSError instead of crashing the
            # server thread (which becomes an unhandled thread exception).
            try:
                self._server_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._server_sock.close()
            return

        self._stop_server_thread = False

    cls._TcpServerConnection__server_thread = _safe_server_thread  # type: ignore[attr-defined]


_patch_secsgem_server_shutdown()


def pytest_configure(config: Any) -> None:  # noqa: ARG001 - pytest hook
    """Make sure Tk objects are collected on the main thread.

    tkinter's `Variable.__del__` calls back into the Tcl interpreter. When a
    GUI test's widgets outlive their root and are collected later - during a
    cycle that happens to trigger on one of the simulator tests' worker
    threads - Tcl aborts the whole process with

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    which kills the run and pops a macOS crash reporter, at a point unrelated
    to the test that created the widget. Collecting after every test forces
    those finalisers to run here, on the main thread, where the worst case is
    an ignorable "main thread is not in main loop" unraisable.
    """
    import gc

    class _CollectOnMainThread:
        @staticmethod
        def pytest_runtest_teardown() -> None:
            if threading.current_thread() is threading.main_thread():
                gc.collect()

    config.pluginmanager.register(_CollectOnMainThread(), "tk-main-thread-gc")
