"""URL and action policy for the browser tools.

The model is not trusted to police itself: anything that can leave the agent's
machine (SSRF, ``file://`` reads) or change the outside world (submitting an
order) is checked here, in code.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_BLOCKED_SCHEMES = frozenset(
    {
        "about",
        "chrome",
        "chrome-extension",
        "data",
        "file",
        "ftp",
        "javascript",
        "vbscript",
        "view-source",
        "ws",
        "wss",
    }
)

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")

_RISKY_ACTION = re.compile(
    r"\b("
    r"buy|purchase|order|checkout|pay|donate|subscribe|unsubscribe|"
    r"delete|remove|destroy|send|submit|post|publish|share|"
    r"confirm|book|reserve|cancel|transfer|withdraw"
    r")\b",
    re.IGNORECASE,
)


class BrowserPolicyError(Exception):
    """Raised when a URL or action is not allowed by policy."""


def normalize_url(raw: str) -> str:
    """Validate a URL from the model and return it in canonical form."""
    value = (raw or "").strip().strip("<>\"'")
    if not value:
        raise BrowserPolicyError("No web address was provided.")

    if value.startswith("//"):
        value = f"https:{value}"

    prefix = value.split(":", 1)[0].strip().lower()
    if "://" not in value and prefix in _BLOCKED_SCHEMES:
        raise BrowserPolicyError(
            "I can only open normal web pages (http or https), not that kind of address."
        )

    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        shown = scheme or "that kind of"
        raise BrowserPolicyError(
            f"I can only open normal web pages (http or https), not {shown} addresses."
        )

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BrowserPolicyError("That web address is missing a site name.")

    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise BrowserPolicyError("I can't open local or internal addresses.")

    if parts.username or parts.password:
        raise BrowserPolicyError(
            "I can't open addresses that have a username or password baked into them."
        )

    return value


def host_in_allowlist(host: str, allowlist: tuple[str, ...]) -> bool:
    """Return True when ``host`` is the allowlisted domain or a subdomain of it."""
    host = host.rstrip(".").lower()
    for entry in allowlist:
        candidate = entry.strip().lstrip(".").rstrip(".").lower()
        if candidate and (host == candidate or host.endswith(f".{candidate}")):
            return True
    return False


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str) -> list[str]:
    def _lookup() -> list[str]:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return list({info[4][0] for info in infos})

    return await asyncio.to_thread(_lookup)


async def assert_url_allowed(
    url: str,
    *,
    allowlist: tuple[str, ...] = (),
    allow_private_hosts: bool = False,
) -> str:
    """Normalize ``url`` and reject anything that could reach the local network.

    DNS is resolved here, before navigation, and again on the final URL after
    redirects. A hostname that fails to resolve is allowed through so Playwright
    can report the real connection error.
    """
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = (parts.hostname or "").rstrip(".").lower()

    if allowlist and not host_in_allowlist(host, allowlist):
        raise BrowserPolicyError(
            f"I'm only allowed to browse {', '.join(allowlist)} right now."
        )

    if allow_private_hosts:
        return normalized

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public_address(str(literal)):
            raise BrowserPolicyError("I can't open private or local network addresses.")
        return normalized

    try:
        addresses = await _resolve(host)
    except OSError:
        # Let the browser surface the real DNS/connection failure.
        return normalized

    if addresses and not any(_is_public_address(address) for address in addresses):
        raise BrowserPolicyError("I can't open private or local network addresses.")

    return normalized


def is_risky_action(name: str | None) -> bool:
    """Return True when clicking a control with this accessible name needs a yes."""
    return bool(name and _RISKY_ACTION.search(name))


def describe_consequence(verb: str, name: str | None, host: str) -> str:
    label = (name or "that control").strip()
    if len(label) > 90:
        label = f"{label[:87]}..."
    return f'{verb} "{label}" on {host}'


__all__ = [
    "BrowserPolicyError",
    "assert_url_allowed",
    "describe_consequence",
    "host_in_allowlist",
    "is_risky_action",
    "normalize_url",
]
