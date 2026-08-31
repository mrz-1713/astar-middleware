"""Exceptions raised by the service."""


from __future__ import annotations


class SimulatorUnavailableError(RuntimeError):
    """`runtime_mode: simulated` on an install that has no simulator package.

    Raised instead of an ImportError so the caller can report it against the one
    machine that asked for it. Every other machine keeps running: a missing
    optional package must not take a 22-machine service down.
    """


class StaleSessionError(RuntimeError):
    """Raised to refuse equipment traffic arriving on a retired session."""
