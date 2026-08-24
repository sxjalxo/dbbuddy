"""Per-account saved charts (Infographics). Stores config only — the frontend
re-runs the SQL live when a chart is opened."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_permission, write_audit
from ..models import DashboardItem, PublishedReport, SavedChart, User
from ..schemas import ChartIn, ChartOut, ChartUpdate, PublicationOut, PublishRequest

router = APIRouter(prefix="/charts", tags=["charts"])

VALID_VISIBILITY = {"organization", "private"}


@router.get("", response_model=list[ChartOut])
def list_charts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return (
        db.query(SavedChart)
        .filter(SavedChart.user_id == user.id)
        .order_by(SavedChart.created_at.desc())
        .all()
    )


@router.post("", response_model=ChartOut, status_code=status.HTTP_201_CREATED)
def create_chart(
    req: ChartIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("chart:save")),
):
    chart = SavedChart(
        user_id=user.id,
        database_connection_id=req.database_connection_id,
        title=req.title,
        nl_query=req.nl_query,
        sql=req.sql,
        chart_type=req.chart_type,
        config=req.config,
        schema_fingerprint=req.schema_fingerprint,
        # New charts are always drafts. "published" is reached only via the
        # publish flow, which also writes the PublishedReport record — accepting
        # a client-supplied status here would let a chart claim to be published
        # with nothing actually published to clients.
        status="draft",
    )
    db.add(chart)
    db.flush()
    write_audit(db, user_id=user.id, action="create", entity_type="chart", entity_id=chart.id,
                organization_id=user.organization_id)
    db.commit()
    db.refresh(chart)
    return chart


@router.patch("/{chart_id}", response_model=ChartOut)
def update_chart(
    chart_id: str,
    req: ChartUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("chart:save")),
):
    """Update a chart's visual attributes (type / colors / title) from the
    Infographics customizer. SQL and connection are immutable here. Editing a
    published chart is allowed — the client report renders live from the chart,
    so the change propagates to viewers on their next open."""
    chart = db.get(SavedChart, chart_id)
    if chart is None or chart.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chart not found")

    fields = req.model_dump(exclude_unset=True)
    if "title" in fields and fields["title"] is not None:
        chart.title = fields["title"]
    if "chart_type" in fields and fields["chart_type"] is not None:
        chart.chart_type = fields["chart_type"]
    if "config" in fields:
        chart.config = fields["config"]  # may be None to reset to defaults

    write_audit(db, user_id=user.id, action="update", entity_type="chart", entity_id=chart.id,
                organization_id=user.organization_id)
    db.commit()
    db.refresh(chart)
    return chart


@router.post("/{chart_id}/publish", response_model=PublicationOut)
def publish_chart(
    chart_id: str,
    req: PublishRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("chart:publish")),
):
    """Publish a chart to the owner's organization. Idempotent: re-publishing an
    already-published chart updates its single active publication record rather
    than creating duplicates. The draft (SavedChart) stays editable."""
    chart = db.get(SavedChart, chart_id)
    if chart is None or chart.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chart not found")

    visibility = (req.visibility if req else "organization")
    if visibility not in VALID_VISIBILITY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid visibility: {visibility}")

    report = (
        db.query(PublishedReport)
        .filter(PublishedReport.chart_id == chart.id, PublishedReport.status == "active")
        .first()
    )
    if report is None:
        report = PublishedReport(
            chart_id=chart.id, published_by=user.id, organization_id=user.organization_id,
            visibility=visibility, status="active",
        )
        db.add(report)
    else:
        report.published_by = user.id
        report.visibility = visibility  # onupdate refreshes published_at

    chart.status = "published"
    db.flush()
    write_audit(db, user_id=user.id, action="publish", entity_type="report", entity_id=report.id,
                organization_id=user.organization_id, detail={"chart_id": chart.id, "visibility": visibility})
    db.commit()
    db.refresh(report)
    return report


@router.post("/{chart_id}/unpublish", status_code=status.HTTP_204_NO_CONTENT)
def unpublish_chart(
    chart_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("chart:publish")),
):
    """Revoke a chart's active publication(s) and return it to draft. The record
    is kept (status=revoked) rather than deleted, preserving publication history."""
    chart = db.get(SavedChart, chart_id)
    if chart is None or chart.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chart not found")

    revoked = (
        db.query(PublishedReport)
        .filter(PublishedReport.chart_id == chart.id, PublishedReport.status == "active")
        .all()
    )
    for r in revoked:
        r.status = "revoked"
    chart.status = "draft"
    write_audit(db, user_id=user.id, action="unpublish", entity_type="report", entity_id=chart.id,
                organization_id=user.organization_id)
    db.commit()


@router.delete("/{chart_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chart(chart_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    chart = db.get(SavedChart, chart_id)
    if chart is None or chart.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chart not found")
    # Unpin it from every dashboard first. SQLite does not enforce the FK, so
    # relying on the declared cascade would leave an orphaned dashboard_item
    # pointing at a chart that no longer exists — which renders as a 500 the next
    # time that dashboard is opened.
    db.query(DashboardItem).filter(DashboardItem.chart_id == chart_id).delete(
        synchronize_session=False
    )
    db.delete(chart)
    write_audit(db, user_id=user.id, action="delete", entity_type="chart", entity_id=chart_id,
                organization_id=user.organization_id)
    db.commit()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_charts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    chart_ids = [row[0] for row in db.query(SavedChart.id).filter(SavedChart.user_id == user.id).all()]
    if chart_ids:
        # Same reason as the single-chart delete: unpin before deleting.
        db.query(DashboardItem).filter(DashboardItem.chart_id.in_(chart_ids)).delete(
            synchronize_session=False
        )
    db.query(SavedChart).filter(SavedChart.user_id == user.id).delete(synchronize_session=False)
    write_audit(db, user_id=user.id, action="delete_all", entity_type="chart",
                organization_id=user.organization_id)
    db.commit()
