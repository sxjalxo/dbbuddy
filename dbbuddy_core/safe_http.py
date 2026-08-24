"""Outbound HTTP that connects to the address it validated.

``requests.post(url, ...)`` resolves the hostname itself, at connect time. That
second lookup is the DNS-rebinding window: whatever ``net_guard`` approved a
moment earlier is not necessarily what the socket ends up talking to.

``post()`` here closes it by dialling the approved address directly:

* resolve the name **once** and validate every address it returned
  (``net_guard.resolve_and_pin``);
* rewrite the URL to that address, so no further lookup happens;
* keep the original hostname in the ``Host`` header, and — for TLS — as the SNI
  name and the name the certificate is checked against, so pinning changes *which
  socket is opened* and nothing about who the server has to prove it is.

That last point is what makes this safe rather than merely different: connecting
by IP without it would either break certificate verification or, worse, invite
someone to switch it off.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter

from dbbuddy_core import net_guard


class _PinnedHostAdapter(HTTPAdapter):
    """Verify TLS against the original hostname while connected to an IP.

    Without this the certificate would be checked against the address in the URL —
    which is now an IP literal, so verification fails against every correctly
    issued certificate. The usual "fix" for that is to disable verification, which
    would trade a rebinding window for a permanent one.
    """

    def __init__(self, server_hostname: str, **kwargs):
        self._server_hostname = server_hostname
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self._server_hostname
        pool_kwargs["assert_hostname"] = self._server_hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """``requests.post`` against a validated, pinned address.

    Raises ``ValueError`` when the destination is not a permissible egress target.
    That is deliberately *not* a ``requests.RequestException``: a blocked address
    is a refusal, not a transient network failure, and callers retry the latter.
    """
    validated_url, address = net_guard.resolve_and_pin(url)
    parsed = urlparse(validated_url)
    hostname = parsed.hostname

    # IPv6 literals need brackets in a URL authority.
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    pinned_url = urlunparse(parsed._replace(netloc=netloc))

    headers = dict(kwargs.pop("headers", None) or {})
    # The server still has to be addressed by name: virtual hosting, and any
    # routing that depends on it, would otherwise break.
    headers.setdefault("Host", parsed.netloc)

    session = requests.Session()
    try:
        if parsed.scheme == "https":
            session.mount("https://", _PinnedHostAdapter(hostname))
        return session.post(pinned_url, headers=headers, **kwargs)
    finally:
        session.close()
