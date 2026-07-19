"""Minimal brute-force protection for the public-internet-facing auth endpoints
(register, admin/apns). This relay sits behind a Cloudflare Tunnel — the tunnel already
encrypts both hops (phone → Cloudflare edge via TLS; edge → this add-on via Cloudflare's
tunnel protocol; there is no directly open inbound port) — but the pairing code itself is
a shared secret, so guessing attempts against it must be slowed and compared safely.
"""
import hmac
import time
from collections import defaultdict
from typing import Callable


class RateLimiter:
    """Sliding-window limiter: at most `max_attempts` per `window_seconds` per key.
    In-memory (resets on add-on restart) — proportionate for a single-family, low-traffic
    relay; a restart-triggered reset is not a meaningful attack surface here."""

    def __init__(self, max_attempts: int, window_seconds: float, now: Callable[[], float] = time.time):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._now = now
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        t = self._now()
        cutoff = t - self.window
        hits = [h for h in self._hits[key] if h > cutoff]
        hits.append(t)
        self._hits[key] = hits
        return len(hits) <= self.max_attempts


def safe_equals(a: str, b: str) -> bool:
    """Constant-time string compare — a plain `==` on a secret can leak it one byte at a
    time via response-timing differences; this closes that side channel."""
    return hmac.compare_digest(a.encode(), b.encode())
