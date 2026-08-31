"""Single-instance lockfile tests (v2 Track B)."""

from __future__ import annotations

import os

import pytest

from eap_middleware.single_instance import (
    InstanceAlreadyRunning,
    SingleInstanceLock,
)


def test_first_acquire_succeeds_and_writes_pid(tmp_path):
    lock = SingleInstanceLock(tmp_path / "test.lock")
    lock.acquire()
    try:
        assert (tmp_path / "test.lock").read_text() == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_in_same_process_is_idempotent(tmp_path):
    lock = SingleInstanceLock(tmp_path / "test.lock")
    lock.acquire()
    lock.acquire()  # idempotent
    lock.release()


def test_second_lock_object_in_same_process_rejected(tmp_path):
    """Two SingleInstanceLock objects pointed at the same file detect the
    other's PID and refuse - this is the protection against a second
    middleware process holding the same install_dir."""
    lock_a = SingleInstanceLock(tmp_path / "test.lock")
    lock_b = SingleInstanceLock(tmp_path / "test.lock")
    lock_a.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            lock_b.acquire()
    finally:
        lock_a.release()


def test_stale_pid_is_reclaimed(tmp_path):
    """A lockfile containing a dead PID gets reclaimed without erroring."""
    lockpath = tmp_path / "test.lock"
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 is init - it exists. Use a large unlikely-to-exist PID instead.
    lockpath.write_text("999999999")  # >max linux PID; unlikely alive

    lock = SingleInstanceLock(lockpath)
    lock.acquire()  # should succeed by reclaiming the stale lock
    try:
        assert lockpath.read_text() == str(os.getpid())
    finally:
        lock.release()


def test_corrupted_lockfile_is_reclaimed(tmp_path):
    lockpath = tmp_path / "test.lock"
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    lockpath.write_text("not-a-number")

    lock = SingleInstanceLock(lockpath)
    lock.acquire()
    try:
        assert lockpath.read_text() == str(os.getpid())
    finally:
        lock.release()


def test_release_only_removes_our_lockfile(tmp_path):
    """If another process replaces the lockfile (race), release() must NOT
    remove their PID."""
    lock = SingleInstanceLock(tmp_path / "test.lock")
    lock.acquire()
    # Simulate a race - some other process wrote its own PID
    (tmp_path / "test.lock").write_text("999999999")
    lock.release()  # must not delete the file
    assert (tmp_path / "test.lock").exists()


def test_context_manager_releases_on_exit(tmp_path):
    with SingleInstanceLock(tmp_path / "test.lock"):
        assert (tmp_path / "test.lock").exists()
    assert not (tmp_path / "test.lock").exists()
