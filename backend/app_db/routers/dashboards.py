"""Dashboards — analyst-authored collections of charts with narrative.

A dashboard is a report in everything but name; the client's term is "dashboard",
so that is what it is called end to end.

Two audiences, one resource:

* **Analysts** (``chart:save``) author dashboards: create, pin a chart, write or
  AI-generate a per-chart description, reorder, unpin, publish.
* **Clients** (``report:view``) open published dashboards, which re-run every
  chart live — see :mod:`app_db.chart_runtime` for the parallel + short-TTL-cache
  execution model.

Publishing mirrors ``published_reports`` exactly: a ``PublishedDashboard`` is a
*record*, not a copy. The dashboard stays the editable draft, clients always
render from it live, re-publishing updates the existing record rather than
duplicating it, and unpublish revokes rather than deletes.
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..chart_runtime import CACHE_TTL_SECONDS, ChartJob, execute_charts
from ..database import get_db
from ..deps import require_permission, write_audit
from ..models import (
    Dashboard,
    DashboardItem,
    DatabaseConnection,
    PublishedDashboard,
    SavedChart,
    User,
)
from ..schemas import (
    DashboardDescribeIn,
    DashboardIn,
    DashboardItemIn,
    DashboardItemPatch,
    DashboardOut,
    DashboardPatch,
    DashboardReorderIn,
    DashboardRunOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# Opening a dashboard re-runs every chart it contains, so its chart count is
# directly its cost against the customer database and its response size. Capped
# where a dashboard grows rather than left to be discovered as a slow request.
MAX_CHARTS_PER_DASHBOARD = int(os.getenv("DASHBOARD_MAX_CHARTS", "40"))

_author = require_permission("chart:save")
_viewer = require_permission("report:view")
_publisher = require_permission("chart:publish")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _owned_or_404(db: Session, dashboard_id: str, user: User) -> Dashboard:
    d = db.get(Dashboard, dashboard_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard not found")
    return d


def _published_or_404(db: Session, dashboard_id: str, user: User) -> Dashboard:
    """A dashboard a *client* may see: published, active, same org, visible.

    Resolved through the publication record rather than `Dashboard.status`, so
    revoking a publication immediately hides it even if the draft still says
    "published".
    """
    pub = (
        db.query(PublishedDashboard)
        .filter(
            PublishedDashboard.dashboard_id == dashboard_id,
            PublishedDashboard.status == "active",
            PublishedDashboard.organization_id == user.organization_id,
        )
        .first()
    )
    if pub is None or (pub.visibility == "private" and pub.published_by != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard not found")
    return pub.dashboard


def _visible_dashboard(db: Session, dashboard_id: str, user: User) -> Dashboard:
    """The dashboard for a *run*, from whichever side the caller comes.

    An analyst previews their own draft; a client opens a published one. Trying
    the owned path first means an analyst never has to publish to preview.
    """
    d = db.get(Dashboard, dashboard_id)
    if d is not None and d.user_id == user.id:
        return d
    return _published_or_404(db, dashboard_id, user)


def _out(d: Dashboard) -> DashboardOut:
    # An item whose chart has vanished is skipped rather than dereferenced. The
    # delete paths unpin explicitly (SQLite does not enforce the FK), so this
    # should not happen — but a dashboard that 500s because one row was orphaned
    # by some future path would be a bad way to find that out.
    return DashboardOut(
        id=d.id,
        title=d.title,
        description=d.description,
        status=d.status,
        created_at=d.created_at,
        updated_at=d.updated_at,
        items=[
            {
                "id": i.id,
                "chart_id": i.chart_id,
                "title": i.chart.title,
                "chart_type": i.chart.chart_type,
                "nl_query": i.chart.nl_query,
                "description": i.description,
                "description_source": i.description_source,
                "position": i.position,
            }
            for i in d.items
            if i.chart is not None
        ],
    )


def _build_jobs(db: Session, dashboard: Dashboard) -> list[ChartJob]:
    """Resolve a dashboard's items into thread-safe execution jobs.

    Every ORM access and every credential decryption happens **here**, on the
    request thread, because :mod:`chart_runtime` runs the queries on a pool and a
    lazy-load inside a worker would be a latent, load-dependent bug.
    """
    from cryptography.fernet import InvalidToken

    from ..security import decrypt_secret

    jobs: list[ChartJob] = []
    for item in dashboard.items:
        chart: SavedChart = item.chart
        conn = (
            db.get(DatabaseConnection, chart.database_connection_id)
            if chart.database_connection_id else None
        )
        password = None
        if conn is not None:
            try:
                password = decrypt_secret(conn.password_encrypted)
            except (InvalidToken, Exception):  # noqa: BLE001
                # An undecryptable credential is a dead source for this purpose;
                # the chart comes back "needs attention" like any other failure
                # rather than 500-ing the whole dashboard.
                conn = None
        jobs.append(ChartJob(
            chart_id=chart.id, sql=chart.sql, chart_type=chart.chart_type,
            config=chart.config, title=chart.title,
            organization_id=dashboard.organization_id,
            engine=conn.engine if conn else None,
            host=conn.host if conn else None,
            port=conn.port if conn else None,
            database=conn.database if conn else None,
            username=conn.username if conn else None,
            password=password,
        ))
    return jobs


# ── Analyst: authoring ────────────────────────────────────────────────────────

@router.get("", response_model=list[DashboardOut])
def list_dashboards(db: Session = Depends(get_db), user: User = Depends(_author)):
    rows = (
        db.query(Dashboard)
        .filter(Dashboard.user_id == user.id)
        .order_by(Dashboard.updated_at.desc())
        .all()
    )
    return [_out(d) for d in rows]


@router.post("", response_model=DashboardOut, status_code=status.HTTP_201_CREATED)
def create_dashboard(body: DashboardIn, db: Session = Depends(get_db),
                     user: User = Depends(_author)):
    d = Dashboard(user_id=user.id, organization_id=user.organization_id,
                  title=body.title, description=body.description)
    db.add(d)
    write_audit(db, user_id=user.id, action="create", entity_type="dashboard",
                organization_id=user.organization_id, detail={"title": body.title})
    db.commit()
    db.refresh(d)
    return _out(d)


@router.get("/{dashboard_id}", response_model=DashboardOut)
def get_dashboard(dashboard_id: str, db: Session = Depends(get_db),
                  user: User = Depends(_viewer)):
    return _out(_visible_dashboard(db, dashboard_id, user))


@router.patch("/{dashboard_id}", response_model=DashboardOut)
def update_dashboard(dashboard_id: str, body: DashboardPatch,
                     db: Session = Depends(get_db), user: User = Depends(_author)):
    d = _owned_or_404(db, dashboard_id, user)
    if body.title is not None:
        d.title = body.title
    if body.description is not None:
        d.description = body.description
    db.commit()
    db.refresh(d)
    return _out(d)


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(dashboard_id: str, db: Session = Depends(get_db),
                     user: User = Depends(_author)):
    d = _owned_or_404(db, dashboard_id, user)
    write_audit(db, user_id=user.id, action="delete", entity_type="dashboard",
                entity_id=d.id, organization_id=user.organization_id)
    db.delete(d)  # items + publication records cascade
    db.commit()


@router.post("/{dashboard_id}/items", response_model=DashboardOut,
             status_code=status.HTTP_201_CREATED)
def pin_chart(dashboard_id: str, body: DashboardItemIn,
              db: Session = Depends(get_db), user: User = Depends(_author)):
    """Pin a chart to a dashboard.

    Re-pinning an already-pinned chart updates its description instead of
    stacking a duplicate — "Pin to dashboard" twice is a correction, not a
    request for two copies of the same chart.
    """
    d = _owned_or_404(db, dashboard_id, user)

    chart = db.get(SavedChart, body.chart_id)
    if chart is None or chart.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chart not found")

    existing = next((i for i in d.items if i.chart_id == chart.id), None)
    if existing is None and len(d.items) >= MAX_CHARTS_PER_DASHBOARD:
        # Opening a dashboard runs every chart it holds, so its size *is* its cost
        # against the customer database. Bounded here, at the only place that
        # grows one, rather than discovered later as a slow request.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A dashboard can hold at most {MAX_CHARTS_PER_DASHBOARD} charts. "
            "Split this into more than one dashboard.",
        )
    if existing is not None:
        if body.description is not None:
            existing.description = body.description
            existing.description_source = "manual"
    else:
        d.items.append(DashboardItem(
            chart_id=chart.id, description=body.description,
            description_source="manual" if body.description else None,
            position=len(d.items),
        ))

    write_audit(db, user_id=user.id, action="pin", entity_type="dashboard",
                entity_id=d.id, organization_id=user.organization_id,
                detail={"chart_id": chart.id})
    db.commit()
    db.refresh(d)
    return _out(d)


@router.patch("/{dashboard_id}/items/{item_id}", response_model=DashboardOut)
def update_item(dashboard_id: str, item_id: str, body: DashboardItemPatch,
                db: Session = Depends(get_db), user: User = Depends(_author)):
    d = _owned_or_404(db, dashboard_id, user)
    item = next((i for i in d.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard item not found")
    if body.description is not None:
        item.description = body.description
        # An analyst editing an AI description makes it theirs.
        item.description_source = "manual"
    db.commit()
    db.refresh(d)
    return _out(d)


@router.delete("/{dashboard_id}/items/{item_id}", response_model=DashboardOut)
def unpin_chart(dashboard_id: str, item_id: str, db: Session = Depends(get_db),
                user: User = Depends(_author)):
    d = _owned_or_404(db, dashboard_id, user)
    item = next((i for i in d.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard item not found")
    d.items.remove(item)
    # Close the gap so positions stay dense; a sparse sequence would drift after
    # repeated unpins and make "move up" arithmetic wrong.
    for index, remaining in enumerate(d.items):
        remaining.position = index
    db.commit()
    db.refresh(d)
    return _out(d)


@router.post("/{dashboard_id}/reorder", response_model=DashboardOut)
def reorder(dashboard_id: str, body: DashboardReorderIn,
            db: Session = Depends(get_db), user: User = Depends(_author)):
    """Set item order from a full list of item ids.

    The submitted list must be exactly the dashboard's items — a partial or
    unknown list is rejected rather than silently applied, since a half-applied
    order is worse than none.
    """
    d = _owned_or_404(db, dashboard_id, user)
    current = {i.id for i in d.items}
    # Length is compared as well as membership: comparing sets alone accepted a
    # list with repeats (``[a, b, b]`` has the same *set* as ``{a, b}``), and the
    # last occurrence won, leaving a sparse order like [0, 2].
    if len(body.item_ids) != len(current) or set(body.item_ids) != current:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The submitted order must list exactly this dashboard's items, once each.",
        )
    order = {item_id: index for index, item_id in enumerate(body.item_ids)}
    for item in d.items:
        item.position = order[item.id]
    db.commit()
    db.refresh(d)
    return _out(d)


# ── Analyst: AI description ───────────────────────────────────────────────────

@router.post("/{dashboard_id}/items/{item_id}/describe")
def describe_item(dashboard_id: str, item_id: str, body: DashboardDescribeIn,
                  db: Session = Depends(get_db), user: User = Depends(_author)):
    """Draft a description for a pinned chart with the Insights Engine.

    Runs the chart, hands the *result* to the same evidence-bound engine the
    Insights panel uses, and returns its summary as a suggested description. The
    analyst chooses whether to keep it: this endpoint **does not save**, because
    an AI-written sentence appearing in a published dashboard without anyone
    reading it first is exactly the failure mode the engine exists to avoid.

    Same guardrails apply — the summary cannot invent a cause, so a description
    is either grounded in the chart's data or says it cannot determine one.
    """
    from dbbuddy_core.insights import context as insights_context
    from dbbuddy_core.insights.service import InsightsDisabled, generate_insights

    from ..ai_runtime import resolve_active_provider_chain

    d = _owned_or_404(db, dashboard_id, user)
    item = next((i for i in d.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard item not found")

    jobs = [j for j in _build_jobs(db, d) if j.chart_id == item.chart_id]
    if not jobs:
        raise HTTPException(status.HTTP_409_CONFLICT, "This chart is no longer available.")

    result = execute_charts(jobs, use_cache=not body.refresh)[0]
    if not result.ok:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            result.message or "The chart's data could not be read, so no description was written.",
        )

    # ``result.rows`` is already capped by chart_runtime.MAX_CHART_ROWS, so the
    # context builder's per-row statistics work over a bounded set no matter what
    # the underlying query returns.
    ctx = insights_context.build_context(
        question=item.chart.nl_query or item.chart.title,
        sql=item.chart.sql, database=item.chart.title,
        rows=result.rows, chart_type=result.chart_type,
    )
    try:
        bundle = generate_insights(ctx, resolve_active_provider_chain(user.organization_id))
    except InsightsDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    write_audit(db, user_id=user.id, action="describe", entity_type="dashboard",
                entity_id=d.id, organization_id=user.organization_id,
                detail={"chart_id": item.chart_id, "provider": bundle.provider})
    db.commit()
    return {
        "description": bundle.summary,
        "provider": bundle.provider,
        "limitations": bundle.limitations,
    }


@router.post("/{dashboard_id}/items/{item_id}/describe/accept", response_model=DashboardOut)
def accept_description(dashboard_id: str, item_id: str, body: DashboardItemPatch,
                       db: Session = Depends(get_db), user: User = Depends(_author)):
    """Save a reviewed AI description, tagged as AI-written for provenance."""
    d = _owned_or_404(db, dashboard_id, user)
    item = next((i for i in d.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dashboard item not found")
    if body.description is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No description supplied.")
    item.description = body.description
    item.description_source = "ai"
    db.commit()
    db.refresh(d)
    return _out(d)


# ── Publishing (mirrors published_reports) ────────────────────────────────────

@router.post("/{dashboard_id}/publish", response_model=DashboardOut)
def publish(dashboard_id: str, db: Session = Depends(get_db),
            user: User = Depends(_publisher)):
    d = _owned_or_404(db, dashboard_id, user)
    if not d.items:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Pin at least one chart before publishing this dashboard.")

    record = (
        db.query(PublishedDashboard)
        .filter(PublishedDashboard.dashboard_id == d.id)
        .first()
    )
    if record is None:
        record = PublishedDashboard(dashboard_id=d.id, published_by=user.id,
                                    organization_id=user.organization_id)
        db.add(record)
    # Re-publishing reactivates the same record rather than creating a second one.
    record.status = "active"
    d.status = "published"

    write_audit(db, user_id=user.id, action="publish", entity_type="dashboard",
                entity_id=d.id, organization_id=user.organization_id,
                detail={"charts": len(d.items)})
    db.commit()
    db.refresh(d)
    return _out(d)


@router.post("/{dashboard_id}/unpublish", response_model=DashboardOut)
def unpublish(dashboard_id: str, db: Session = Depends(get_db),
              user: User = Depends(_publisher)):
    d = _owned_or_404(db, dashboard_id, user)
    record = (
        db.query(PublishedDashboard)
        .filter(PublishedDashboard.dashboard_id == d.id)
        .first()
    )
    if record is not None:
        record.status = "revoked"  # revoked, not deleted — the history stays
    d.status = "draft"
    write_audit(db, user_id=user.id, action="unpublish", entity_type="dashboard",
                entity_id=d.id, organization_id=user.organization_id)
    db.commit()
    db.refresh(d)
    return _out(d)


# ── Client: published dashboards ──────────────────────────────────────────────

@router.get("/published/list", response_model=list[DashboardOut])
def list_published(db: Session = Depends(get_db), user: User = Depends(_viewer)):
    """Dashboards published to the caller's organization.

    Path is ``/published/list`` rather than a query flag so it can never collide
    with a dashboard id in ``/dashboards/{id}``.
    """
    rows = (
        db.query(PublishedDashboard)
        .filter(
            PublishedDashboard.status == "active",
            PublishedDashboard.organization_id == user.organization_id,
            or_(PublishedDashboard.visibility == "organization",
                PublishedDashboard.published_by == user.id),
        )
        .order_by(PublishedDashboard.published_at.desc())
        .all()
    )
    return [_out(r.dashboard) for r in rows]


@router.post("/{dashboard_id}/run", response_model=DashboardRunOut)
def run_dashboard(dashboard_id: str, refresh: bool = False,
                  db: Session = Depends(get_db), user: User = Depends(_viewer)):
    """Re-run every chart in a dashboard and return the fresh results.

    Charts run **concurrently** (the dominant cost of opening a dashboard is N
    round-trips, not N queries) and may be served from a short-TTL cache. Each
    result carries its own ``fetched_at`` and ``cached`` so the UI can state how
    current the data is instead of implying it is instantaneous. ``refresh=true``
    bypasses the cache.

    A chart that fails comes back ``needs_attention`` while its siblings render
    normally — one dead connection must not blank an otherwise working dashboard.
    """
    dashboard = _visible_dashboard(db, dashboard_id, user)
    jobs = _build_jobs(db, dashboard)
    results = execute_charts(jobs, use_cache=not refresh)

    by_chart = {r.chart_id: r for r in results}
    charts = []
    for item in dashboard.items:
        result = by_chart.get(item.chart_id)
        if result is None:
            continue
        payload = result.to_dict()
        payload.update({
            "item_id": item.id,
            "title": item.chart.title,
            "description": item.description,
            "description_source": item.description_source,
        })
        charts.append(payload)

    write_audit(db, user_id=user.id, action="run", entity_type="dashboard",
                entity_id=dashboard.id, organization_id=user.organization_id,
                detail={"charts": len(charts),
                        "failed": sum(1 for c in charts if not c["ok"])})
    db.commit()

    return DashboardRunOut(
        id=dashboard.id, title=dashboard.title, description=dashboard.description,
        charts=charts, cache_ttl_seconds=CACHE_TTL_SECONDS,
    )
