"""Client-facing published reports (read-only).

A Client/User sees reports published to their organization and can run them to
get **live** data — the stored SQL is re-executed against the chart owner's
connection, server-side. The client never receives the SQL or any credentials,
and only read-only queries are ever executed. On any failure the report is
flagged "needs attention" rather than returning stale data.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_permission, write_audit
from ..models import DatabaseConnection, PublishedReport, User
from ..schemas import ReportOut, ReportRunOut

router = APIRouter(prefix="/reports", tags=["reports"])


def _report_out(r: PublishedReport) -> ReportOut:
    return ReportOut(
        id=r.id, chart_id=r.chart_id, title=r.chart.title, chart_type=r.chart.chart_type,
        nl_query=r.chart.nl_query, visibility=r.visibility, published_at=r.published_at,
    )


def _visible_or_404(db: Session, report_id: str, user: User) -> PublishedReport:
    """An active report in the caller's org, respecting visibility. Else 404."""
    r = db.get(PublishedReport, report_id)
    if (
        r is None
        or r.status != "active"
        or r.organization_id != user.organization_id
        or (r.visibility == "private" and r.published_by != user.id)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return r


@router.get("", response_model=list[ReportOut])
def list_reports(db: Session = Depends(get_db), user: User = Depends(require_permission("report:view"))):
    rows = (
        db.query(PublishedReport)
        .filter(
            PublishedReport.status == "active",
            PublishedReport.organization_id == user.organization_id,
            or_(PublishedReport.visibility == "organization", PublishedReport.published_by == user.id),
        )
        .order_by(PublishedReport.published_at.desc())
        .all()
    )
    return [_report_out(r) for r in rows]


@router.get("/{report_id}", response_model=ReportOut)
def get_report(report_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("report:view"))):
    return _report_out(_visible_or_404(db, report_id, user))


@router.post("/{report_id}/run", response_model=ReportRunOut)
def run_report(report_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("report:view"))):
    report = _visible_or_404(db, report_id, user)
    chart = report.chart
    conn_cfg = (
        db.get(DatabaseConnection, chart.database_connection_id)
        if chart.database_connection_id else None
    )
    result = _execute_report(chart, conn_cfg, report.organization_id)
    # Audit the view (client-engagement signal) with the outcome, then commit.
    write_audit(db, user_id=user.id, action="run", entity_type="report", entity_id=report.id,
                organization_id=user.organization_id,
                detail={"chart_id": report.chart_id, "ok": result.ok})
    db.commit()
    return result


def _execute_report(chart, conn_cfg, organization_id: str | None = None) -> ReportRunOut:
    """Re-run a chart's stored SQL live against the owner's connection (read-only).

    Delegates to :mod:`app_db.chart_runtime`, the single chart-execution path
    shared with dashboards — read-only enforcement, connection handling, and the
    "needs attention" contract live there once rather than in two places that can
    drift. A single report is run with the cache **disabled**: opening one report
    is not the burst that the short-TTL cache exists to absorb, and a client
    asking for one chart should get it live.
    """
    from cryptography.fernet import InvalidToken

    from ..chart_runtime import ChartJob, execute_chart
    from ..security import decrypt_secret

    password = None
    if conn_cfg is not None:
        try:
            password = decrypt_secret(conn_cfg.password_encrypted)
        except (InvalidToken, Exception):  # noqa: BLE001 — stale key → dead source
            conn_cfg = None

    job = ChartJob(
        chart_id=chart.id, sql=chart.sql, chart_type=chart.chart_type,
        config=chart.config, title=chart.title,
        organization_id=organization_id,
        engine=conn_cfg.engine if conn_cfg else None,
        host=conn_cfg.host if conn_cfg else None,
        port=conn_cfg.port if conn_cfg else None,
        database=conn_cfg.database if conn_cfg else None,
        username=conn_cfg.username if conn_cfg else None,
        password=password,
    )
    result = execute_chart(job, use_cache=False)
    return ReportRunOut(
        ok=result.ok, columns=result.columns, rows=result.rows,
        needs_attention=result.needs_attention, message=result.message,
        chart_type=result.chart_type, config=result.config,
    )
