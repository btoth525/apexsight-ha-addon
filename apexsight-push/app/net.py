"""Best-effort real client IP.

Behind a Cloudflare Tunnel / reverse proxy the socket peer is the proxy, so we
prefer the standard forwarding headers. NOTE: these headers are only trustworthy
if the origin is reachable *only* through your proxy — lock the add-on's host
port to the tunnel (don't expose 3421 directly) or a direct caller can spoof
them. See SECURITY.md.
"""
from fastapi import Request


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
