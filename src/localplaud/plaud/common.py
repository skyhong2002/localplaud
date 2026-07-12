"""Shared safety primitives for official Plaud transports."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_MAX_AUDIO_BYTES = 2 * 1024 * 1024 * 1024


class PlaudError(RuntimeError):
    pass


class PlaudAuthError(PlaudError):
    pass


def _ext_from_url(url: str, default: str = "mp3") -> str:
    path = url.split("?", 1)[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return default


def _assert_safe_fetch_url(url: str) -> None:
    """Reject non-HTTPS or non-public fetch targets before signed-URL access."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise PlaudError(f"refusing to fetch non-https URL: {parsed.scheme}://…")
    host = parsed.hostname
    if not host:
        raise PlaudError("fetch URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise PlaudError(f"cannot resolve fetch host {host!r}: {exc}") from exc
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise PlaudError(f"refusing to fetch URL resolving to non-public IP {ip}")
