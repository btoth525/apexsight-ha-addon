"""Single source of truth for trusted-proxy / real-IP resolution.

Extracted out of main.py so admin.py's login lockout can share the EXACT same anti-spoofing
logic as main.py's rate limiter, instead of admin.py maintaining its own copy that can silently
drift out of sync (which is exactly what happened before this file existed: admin.py trusted
cf-connecting-ip/x-forwarded-for unconditionally, so a caller could forge a fresh fake IP on
every login attempt and the fail2ban-style lockout would never accumulate against any one key).
"""
import ipaddress

from fastapi import Request


def is_trusted_proxy(request: Request) -> bool:
    """Whether the SOCKET peer is the local Cloudflare tunnel / same-host proxy — i.e. a
    private/loopback address. The tunnel is the only way in (the host port isn't exposed to
    untrusted devices), and it connects from the local network, so only a private-peer request
    may set the cf-connecting-ip / x-forwarded-for we trust. A public socket peer can't have
    transited the tunnel, so its forwarding headers are ignored — an attacker can't spoof them
    to rotate a rate-limit/lockout key or masquerade as internal."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost",)
    return ip.is_private or ip.is_loopback


def real_ip(request: Request) -> str:
    """The caller's IP for rate limiting / login lockouts. Trust forwarding headers only from
    the local tunnel/proxy; otherwise use the socket peer so headers can't be spoofed."""
    if is_trusted_proxy(request):
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
