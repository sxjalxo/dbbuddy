"""Per-request correlation id, propagated via a context variable.

A middleware sets the id at the start of each request; ``write_audit`` reads it
so every event from one request shares a ``request_id`` and a whole flow can be
traced as a unit. ContextVars are copied into the threadpool that runs sync
endpoints, so the value is visible there too.
"""

import contextvars
import uuid

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def new_request_id() -> str:
    return str(uuid.uuid4())


def set_request_id(rid: str | None) -> None:
    _request_id.set(rid)


def get_request_id() -> str | None:
    return _request_id.get()
