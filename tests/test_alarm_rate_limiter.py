"""AlarmRateLimiter regression tests (v2 Track A)."""

from __future__ import annotations

from eap_middleware.alarms import AlarmRateLimiter


def test_alarms_below_threshold_pass_through():
    rl = AlarmRateLimiter(max_per_window=5, window_sec=1.0)
    t = 1000.0
    for _ in range(5):
        assert rl.admit("M1", now=t) is True
    assert rl.drain_drops() == {}


def test_alarms_above_threshold_throttled_and_counted():
    rl = AlarmRateLimiter(max_per_window=3, window_sec=1.0)
    t = 1000.0
    results = [rl.admit("M1", now=t) for _ in range(10)]
    # First 3 admit, next 7 drop
    assert results == [True, True, True] + [False] * 7
    assert rl.drain_drops() == {"M1": 7}
    # drain resets the counter
    assert rl.drain_drops() == {}


def test_sliding_window_releases_capacity_over_time():
    rl = AlarmRateLimiter(max_per_window=2, window_sec=1.0)
    assert rl.admit("M1", now=100.0) is True
    assert rl.admit("M1", now=100.1) is True
    assert rl.admit("M1", now=100.2) is False  # at cap
    # Advance time past the window - the first admit ages out
    assert rl.admit("M1", now=101.5) is True


def test_per_machine_isolation():
    """A storm on machine A doesn't throttle machine B."""
    rl = AlarmRateLimiter(max_per_window=2, window_sec=1.0)
    t = 1000.0
    # Saturate M1
    rl.admit("M1", now=t)
    rl.admit("M1", now=t)
    assert rl.admit("M1", now=t) is False
    # M2 has full capacity
    assert rl.admit("M2", now=t) is True
    assert rl.admit("M2", now=t) is True
    drops = rl.drain_drops()
    assert drops == {"M1": 1}


def test_clears_and_safety_categories_are_never_throttled():
    limiter = AlarmRateLimiter(max_per_window=1, window_sec=1.0)
    assert limiter.admit("M1", now=1.0, alarm={"alid": 10, "alcd": 3, "is_set": True})
    assert limiter.admit("M1", now=1.0, alarm={"alid": 10, "alcd": 3, "is_set": False})
    assert limiter.admit("M1", now=1.0, alarm={"alid": 11, "alcd": 1, "is_set": True})
    assert limiter.admit("M1", now=1.0, alarm={"alid": 12, "alcd": 2, "is_set": True})


def test_drop_details_retain_alarm_identity():
    limiter = AlarmRateLimiter(max_per_window=1, window_sec=1.0)
    limiter.admit("M1", now=1.0, alarm={"alid": 10, "alcd": 3, "is_set": True})
    assert not limiter.admit("M1", now=1.0, alarm={"alid": 20, "alcd": 3, "is_set": True})
    assert limiter.drain_drop_details() == {
        "M1": {"count": 1, "alids": {"20": 1}}
    }
