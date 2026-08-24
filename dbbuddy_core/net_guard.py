"""Egress rules for operator-supplied outbound URLs, and the address to dial.

An AI provider record carries a ``base_url`` that this server then makes HTTP
requests to, with the org's API key attached. That is a server-side request
forgery primitive: whoever can write a provider record chooses a destination the
*server* reaches, from inside the deployment's network, and reads the answer back.

Two rules, deliberately split by how much they cost a legitimate user:

**Always blocked.** The cloud instance-metadata addresses (169.254.169.254 and the
IPv6 equivalent) and the rest of link-local space. On any cloud VM these hand out
IAM credentials to anything that can issue a plain HTTP GET, so reaching them is
never a legitimate provider configuration and always a credential-theft attempt.
Non-http(s) schemes (``file://``, ``gopher://``) are blocked here too — they
address no LLM endpoint and only exist in this field as an exploit.

**Blocked only in strict mode** (``AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1``).
Loopback and RFC1918 space. DB Buddy's *default* deployment runs Ollama on
localhost and self-hosted models on the LAN — the bootstrap seeds
``http://localhost:11434`` — so blocking private ranges by default would break the
product's normal configuration. Turn it on for a hosted/multi-tenant deployment,
where a tenant admin is untrusted relative to the network the server sits in.

## Why this lives in the engine

It used to live in ``backend/app_db/url_guard.py``, which validated the URL when a
provider record was written. But the outbound request is made *here*, in the
engine, and the engine cannot import the backend — so validation and connection
were necessarily in different places, and the guard said so about itself:

    Note the limit honestly: this validates the URL, not the socket. A hostname
    that resolves to a blocked address (DNS rebinding, or an attacker-controlled
    A record) still passes.

``resolve_and_pin`` closes that. It resolves the name **once**, checks every
address that lookup returned, and hands back the specific address to dial. There
is no second lookup, so there is no second answer that could differ. The backend
still validates on write — failing early is friendlier than failing at generation
time — but the check that matters now runs at the moment of connection.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def block_private_networks() -> bool:
    """Read at call time, not import time, so tests and deploys can toggle it."""
    return os.getenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", "").strip().lower() in {
        "1", "true", "yes",
    }


def _default_resolver(host: str) -> list:
    """Every address ``host`` currently resolves to."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def check_address(ip) -> None:
    """Raise ``ValueError`` if this address must not be reached."""
    if ip.is_link_local:
        # Covers 169.254.169.254 / fe80:: — cloud metadata and link-local.
        raise ValueError(
            "base_url resolves to a link-local address (cloud instance metadata "
            "range). This is never a valid AI provider endpoint."
        )
    if block_private_networks() and (ip.is_loopback or ip.is_private or ip.is_reserved):
        raise ValueError(
            "base_url resolves to a private or loopback address, which this "
            "deployment does not permit (AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1)."
        )


def _parse(url: str):
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(
            f"base_url must use http or https (got {parsed.scheme or 'no scheme'!r})."
        )
    if not parsed.hostname:
        raise ValueError("base_url must include a host.")
    return parsed


def validate_outbound_url(url: str | None) -> str | None:
    """Return the URL if it is a permissible egress target, else raise.

    ``None``/blank passes through unchanged — the "no endpoint" case is handled by
    the adapter's own required-field validation, not here.

    Used when a provider record is *written*, so a mistake is caught while someone
    is looking at a form rather than at generation time. It is not the security
    boundary on its own: see ``resolve_and_pin``, which is what the outbound call
    actually goes through.

    An unresolvable hostname passes here — DNS being briefly unavailable should
    not stop a provider being saved — and is refused later, at connect time, where
    there is genuinely nothing safe to dial.
    """
    if url is None or not url.strip():
        return url
    url = url.strip()
    parsed = _parse(url)

    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        addresses = _default_resolver(parsed.hostname)

    for ip in addresses:
        check_address(ip)
    return url


def resolve_and_pin(url: str, resolver=None) -> tuple[str, str]:
    """Resolve once, validate that answer, and return ``(url, address_to_dial)``.

    Every address the lookup returned must pass — picking the first acceptable one
    would let an attacker hide a metadata address behind a public one in the same
    response.

    Unlike ``validate_outbound_url``, a name that resolves to nothing is an error:
    at connect time there is no address to pin, so there is nothing safe to reach.
    """
    parsed = _parse(url)
    host = parsed.hostname

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        check_address(literal)
        return url, str(literal)

    addresses = (resolver or _default_resolver)(host)
    if not addresses:
        raise ValueError(f"base_url host {host!r} could not be resolved.")

    for ip in addresses:
        check_address(ip)

    # The first address is the one dialled; all of them were checked, so any is
    # safe, and using the resolver's own order preserves whatever preference it
    # expressed (IPv6 first on most systems).
    return url, str(addresses[0])
