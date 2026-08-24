"""Insights Engine — evidence-bound explanations of executed query results.

* ``POST /insights/generate`` — analyze a result set the client has already run.
* ``POST /insights/ask`` — answer a follow-up against that same result.
* ``GET  /insights/settings`` — what the UI needs to render the panel.

The client posts the rows it already has rather than the server re-running the
query: the deterministic engine has *already* executed it, and re-executing to
explain it would double the load on the customer database and risk explaining
different data than the user is looking at.

Gated on ``schema:analyze`` — the analyst-only permission, matching the relation
graph — so the whole feature is analyst-only server-side, not just UI-hidden.

Nothing here writes SQL, and no field of these requests ever reaches the SQL
path; ``sql`` is carried for provenance and cache identity only.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dbbuddy_core.insights import context as insights_context
from dbbuddy_core.insights.formatter import to_markdown
from dbbuddy_core.insights.models import InsightBundle, settings as insights_settings
from dbbuddy_core.insights.prompts import PROMPT_VERSION, suggested_questions
from dbbuddy_core.insights.service import (
    InsightsDisabled,
    answer_followup,
    generate_insights,
)

from ..ai_runtime import resolve_active_provider_chain
from ..database import get_db
from ..deps import require_permission, write_audit
from ..models import DatabaseConnection, InsightCache, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["insights"])

# The analyst role is the only one granted schema:analyze.
_analyst = require_permission("schema:analyze")

# A hard ceiling on posted rows, independent of the body-size limit. Statistics
# are computed over every row supplied, so this bounds that work too.
MAX_POSTED_ROWS = 5000


class InsightRequest(BaseModel):
    connection_id: str | None = None
    question: str = ""
    sql: str = ""
    database: str = ""
    rows: list[dict] = Field(default_factory=list)
    chart_type: str | None = None
    # Skip the cache read and overwrite the entry — the UI's Regenerate button.
    regenerate: bool = False


class FollowupRequest(BaseModel):
    connection_id: str | None = None
    question: str = ""          # the original NL question, for context
    followup: str               # what the user is asking now
    sql: str = ""
    database: str = ""
    rows: list[dict] = Field(default_factory=list)
    chart_type: str | None = None
    history: list[dict] = Field(default_factory=list)


def _resolve_connection_label(db: Session, user: User, connection_id: str | None,
                              fallback: str) -> str:
    """Verify ownership of a referenced connection and return its display label.

    A connection the caller does not own is a 404 — the same shape the relation
    endpoints use, so probing for foreign connection ids reveals nothing.
    """
    if not connection_id:
        return fallback or "database"
    conn = db.get(DatabaseConnection, connection_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn.name or conn.database or fallback


def _build_context(req, database_label: str):
    if len(req.rows) > MAX_POSTED_ROWS:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Too many rows for analysis ({len(req.rows)}); the limit is {MAX_POSTED_ROWS}. "
            "Aggregate or filter the query first.",
        )
    return insights_context.build_context(
        question=req.question, sql=req.sql, database=database_label,
        rows=req.rows, chart_type=req.chart_type,
    )


def _cache_key(*, sql: str, result_hash: str, connection_id: str | None,
               provider_label: str, user_id: str) -> str:
    """Phase 7 identity. ``PROMPT_VERSION`` is a component, so changing the prompt
    or the guardrails invalidates every entry without a purge step.

    ``user_id`` is part of the key because the *lookup* is user-scoped. Without it
    the two disagree: two analysts in one org running the same query with no saved
    connection (``connection_id`` is None) derive the same key, each miss the
    other's row on read, and the second insert violates the unique constraint —
    a 500 on a perfectly ordinary request. Key scope and read scope must match.
    """
    blob = "\x1f".join([
        sql or "", result_hash, connection_id or "", PROMPT_VERSION, provider_label, user_id,
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_fresh(row: InsightCache) -> bool:
    """Whether a cached entry is still within ``INSIGHTS_CACHE_TTL_HOURS``.

    A stale row is re-generated in place rather than deleted, so the TTL bounds
    how old an explanation can be without the table needing a sweeper. A
    non-positive TTL disables expiry (entries live until the prompt version or the
    data changes).
    """
    ttl = insights_settings.cache_ttl_hours
    if ttl <= 0:
        return True
    created = row.created_at
    if created is None:
        return False
    # Rows written before this column carried a timezone read back naive on some
    # backends; assume UTC rather than raising on the comparison.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(hours=ttl)


def _chain_label(chain) -> str:
    """Identify the provider chain in the cache key.

    The *whole* chain, not just the active provider: swapping the fallback changes
    which model may answer, and a cached bundle attributed to a provider that is no
    longer in the chain would be misleading provenance.
    """
    if not chain:
        return "none"
    return "|".join(f"{c.name or c.adapter}:{c.model}" for c in chain)


@router.get("/settings")
def get_settings(user: User = Depends(_analyst)):
    """What the panel needs to render itself (Phase 8). Never exposes credentials —
    provider *identity* is reported, provider secrets are not."""
    chain = resolve_active_provider_chain(user.organization_id)
    return {
        "enabled": insights_settings.enabled,
        "configured": bool(chain),
        "provider": (chain[0].name or chain[0].adapter) if chain else None,
        "prompt_version": PROMPT_VERSION,
        "temperature": insights_settings.temperature,
        "max_sample_rows": insights_settings.max_sample_rows,
        "max_rows": MAX_POSTED_ROWS,
    }


@router.post("/generate")
def generate(
    req: InsightRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_analyst),
):
    """Analyze an executed result set, serving from cache when nothing changed."""
    label = _resolve_connection_label(db, user, req.connection_id, req.database)
    ctx = _build_context(req, label)
    chain = resolve_active_provider_chain(user.organization_id)

    key = _cache_key(
        sql=req.sql, result_hash=insights_context.result_hash(ctx),
        connection_id=req.connection_id, provider_label=_chain_label(chain),
        user_id=user.id,
    )

    row = db.query(InsightCache).filter(
        InsightCache.cache_key == key, InsightCache.user_id == user.id,
    ).one_or_none()

    if row is not None and not req.regenerate and _is_fresh(row):
        bundle = InsightBundle.from_dict(row.bundle)
        bundle.cached = True
        return {
            "insights": bundle.to_dict(),
            "markdown": to_markdown(bundle),
            "suggested_questions": suggested_questions(ctx),
            "row_count": ctx.row_count,
        }

    try:
        bundle = generate_insights(ctx, chain)
    except InsightsDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # Only a bundle an actual provider produced is worth storing. Caching the
    # "no provider reachable" placeholder would pin a transient outage in place
    # until the entry expired.
    if bundle.provider:
        if row is None:
            row = InsightCache(cache_key=key, user_id=user.id)
            db.add(row)
        row.connection_id = req.connection_id
        row.organization_id = user.organization_id
        row.prompt_version = bundle.prompt_version
        row.provider = bundle.provider
        row.bundle = bundle.to_dict()
        # Overwriting an expired entry has to restart its clock, or a stale row
        # would re-generate on every request forever and the TTL would silently
        # become "never cache".
        row.created_at = datetime.now(timezone.utc)

    write_audit(db, user_id=user.id, action="insights_generate", entity_type="db_query",
                entity_id=req.connection_id, organization_id=user.organization_id,
                detail={"row_count": ctx.row_count, "provider": bundle.provider,
                        "regenerate": req.regenerate})
    db.commit()

    return {
        "insights": bundle.to_dict(),
        "markdown": to_markdown(bundle),
        "suggested_questions": suggested_questions(ctx),
        "row_count": ctx.row_count,
    }


@router.post("/ask")
def ask(
    req: FollowupRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_analyst),
):
    """Answer a follow-up against the same result. Not cached — a follow-up is
    conversational and carries client-held history, so an identical question in a
    different conversation is a genuinely different request."""
    label = _resolve_connection_label(db, user, req.connection_id, req.database)
    ctx = _build_context(req, label)
    chain = resolve_active_provider_chain(user.organization_id)

    try:
        result = answer_followup(ctx, req.followup, chain, history=req.history)
    except InsightsDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    write_audit(db, user_id=user.id, action="insights_ask", entity_type="db_query",
                entity_id=req.connection_id, organization_id=user.organization_id,
                detail={"row_count": ctx.row_count, "provider": result.get("provider")})
    db.commit()
    return result
