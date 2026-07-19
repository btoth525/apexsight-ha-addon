from app.security import RateLimiter, safe_equals


def test_allows_up_to_max_attempts_then_blocks():
    t = [0.0]
    limiter = RateLimiter(max_attempts=3, window_seconds=60, now=lambda: t[0])
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False   # 4th within the window exceeds the cap


def test_window_resets_after_expiry():
    t = [0.0]
    limiter = RateLimiter(max_attempts=1, window_seconds=10, now=lambda: t[0])
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    t[0] = 11.0
    assert limiter.allow("k") is True   # old hit has aged out of the window


def test_keys_are_independent():
    t = [0.0]
    limiter = RateLimiter(max_attempts=1, window_seconds=60, now=lambda: t[0])
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True    # separate bucket — not affected by "a"
    assert limiter.allow("a") is False


def test_safe_equals_matches_and_mismatches():
    assert safe_equals("LULLA-ABCD-1234", "LULLA-ABCD-1234") is True
    assert safe_equals("LULLA-ABCD-1234", "LULLA-WRONG-000") is False
    assert safe_equals("", "") is True
