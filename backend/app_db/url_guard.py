"""Egress guard for operator-supplied outbound URLs (AI provider ``base_url``).

An AI provider record carries a ``base_url`` that this server then makes HTTP
requests to, with the org's API key attached. That is a server-side request
forgery primitive: whoever can write a provider record chooses a destination the
*server* reaches, from inside the deployment's network, and reads the answer back
through ``POST /ai-providers/{id}/test`` and through labeled schema terms.

Two rules, deliberately split by how much they cost a legitimate user:

**Always blocked.** The cloud instance-metadata addresses (169.254.169.254 and
the IPv6 equivalent) and the rest of link-local space. On any cloud VM these hand
out IAM credentials to anything that can issue a plain HTTP GET, so reaching them
is never a legitimate provider configuration and always a credential-theft
attempt. Non-http(s) schemes (``file://``, ``gopher://``) are blocked here too —
they address no LLM endpoint and only exist in this field as an exploit.

**Blocked only in strict mode** (``AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1``).
Loopback and RFC1918 space. DB Buddy's *default* deployment runs Ollama on
localhost and self-hosted models on the LAN — the bootstrap seeds
``http://localhost:11434`` — so blocking private ranges by default would break the
product's normal configuration. Turn it on for a hosted/multi-tenant deployment,
where a tenant admin is untrusted relative to the network the server sits in.

Note the limit honestly: this validates the URL, not the socket. A hostname that
resolves to a blocked address (DNS rebinding, or an attacker-controlled A record)
still passes. Closing that needs resolve-then-pin-the-IP at connect time in the
HTTP client. This raises the floor; it is not a complete SSRF defense.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def block_private_networks() -> bool:
    """Read at call time, not import time, so tests and deploys can toggle it."""
    return os.getenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", "").strip().lower() in {"1", "true", "yes"}


def _classify(host: str) -> list[ipaddress._BaseAddress]:
    """Every IP ``host`` denotes: itself if literal, else its resolved addresses.

    Resolution failure yields an empty list — an unresolvable hostname is not
    evidence of an attack, and rejecting on it would make provider creation fail
    whenever DNS is briefly unavailable.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
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


def validate_outbound_url(url: str | None) -> str | None:
    """Return the URL if it is a permissible egress target, else raise ValueError.

    ``None``/blank passes through unchanged — the "no endpoint" case is handled by
    the adapter's own required-field validation, not here.
    """
    if url is None or not url.strip():
        return url
    url = url.strip()

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(
            f"base_url must use http or https (got {parsed.scheme or 'no scheme'!r})."
        )
    host = parsed.hostname
    if not host:
        raise ValueError("base_url must include a host.")

    strict = block_private_networks()
    for ip in _classify(host):
        if ip.is_link_local:
            # Covers 169.254.169.254 / fe80::  — cloud metadata and link-local.
            raise ValueError(
                "base_url resolves to a link-local address (cloud instance metadata "
                "range). This is never a valid AI provider endpoint."
            )
        if strict and (ip.is_loopback or ip.is_private or ip.is_reserved):
            raise ValueError(
                "base_url resolves to a private or loopback address, which this "
                "deployment does not permit (AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1)."
            )
    return url
