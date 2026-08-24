"""Egress validation for operator-supplied outbound URLs (AI provider ``base_url``).

**The rules now live in** :mod:`dbbuddy_core.net_guard`. This module is the
backend's door to them, kept so the write path (`POST`/`PATCH /ai-providers`) can
reject a bad endpoint while someone is still looking at the form, rather than at
generation time.

They moved because of where the danger actually is. This module validated a URL;
the outbound request is made by the *engine*, which cannot import the backend — so
validation and connection lived in different places, and a name could resolve to
something acceptable here and something else at connect time. That is DNS
rebinding, and this file used to say so about itself:

    Note the limit honestly: this validates the URL, not the socket.

The engine now resolves once, validates every address that lookup returned, and
dials that address (:func:`dbbuddy_core.net_guard.resolve_and_pin`, used by
:mod:`dbbuddy_core.safe_http`). So the check that matters runs at the moment of
connection, and this one is the early, friendly failure — not the boundary.

One deliberate difference between the two: a hostname that does not resolve
*passes* here, because DNS being briefly unavailable should not stop a provider
being saved. At connect time the same name is refused, because there is no address
to pin and therefore nothing safe to reach.
"""

from dbbuddy_core.net_guard import (  # noqa: F401 — re-exported as this module's API
    ALLOWED_SCHEMES,
    block_private_networks,
    validate_outbound_url,
)

__all__ = ["ALLOWED_SCHEMES", "block_private_networks", "validate_outbound_url"]
