"""Background jobs (Milestone 6).

Two layers, intentionally decoupled so the executor can be swapped later (e.g.
for Celery) without touching callers:

* ``execute_job`` — the *executor*: runs one job by type, records a JobRun and a
  Notification, and updates the job's last_* fields. Pure and synchronous; the
  API's "Run now" calls it inline and the scheduler calls it on a thread.
* ``JobScheduler`` — a thin wrapper over APScheduler (in-process, no broker)
  that maps a ScheduledJob's cron/interval to a trigger and calls back into
  ``execute_job``. A single process-wide ``scheduler`` instance is started at
  app startup (unless DBBUDDY_DISABLE_SCHEDULER=1, e.g. in tests).
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import or_
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .database import SessionLocal
from .models import DatabaseConnection, JobRun, Notification, PublishedReport, ScheduledJob

logger = logging.getLogger(__name__)

JOB_TYPES = {"report_refresh", "context_rebuild"}
SCHEDULE_KINDS = {"interval", "daily", "weekly", "manual"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Executor ──────────────────────────────────────────────────────────────────

def _run_report_refresh(db, job: ScheduledJob) -> str:
    """Re-run a published report's SQL live to validate it and warm its data."""
    from .routers.reports import _execute_report  # lazy: avoids an import cycle

    report = db.get(PublishedReport, job.target_ref)
    if report is None or report.status != "active":
        raise RuntimeError("The report is no longer published.")
    chart = report.chart
    conn_cfg = (
        db.get(DatabaseConnection, chart.database_connection_id)
        if chart.database_connection_id else None
    )
    # Pass the org so a scheduled refresh derives the same cache key as a live
    # view of the same report; omitting it would key this path under org=None and
    # quietly halve the hit rate.
    result = _execute_report(chart, conn_cfg, report.organization_id)
    if not result.ok:
        raise RuntimeError(result.message or "Report refresh failed.")
    return f"Refreshed '{chart.title}' — {len(result.rows)} row(s)."


def _run_context_rebuild(db, job: ScheduledJob) -> str:
    """Rebuild a connection's prepared semantic context so queries stay warm."""
    conn = db.get(DatabaseConnection, job.target_ref)
    if conn is None:
        raise RuntimeError("The connection no longer exists.")

    from dbbuddy_core import context_store
    from dbbuddy_core.models import DBConfig

    from .security import decrypt_secret

    config = DBConfig(
        host=conn.host, user=conn.username, password=decrypt_secret(conn.password_encrypted),
        database=conn.database, engine=conn.engine, port=conn.port,
        db_schema=conn.db_schema,
    )
    context_store.rebuild(config)
    return f"Rebuilt query context for '{conn.name}'."


_EXECUTORS = {"report_refresh": _run_report_refresh, "context_rebuild": _run_context_rebuild}


def execute_job(db, job: ScheduledJob) -> JobRun:
    """Run one job, record history + a notification, update its status. Commits."""
    started = _now()
    job.last_run_at = started
    job.last_status = "running"
    try:
        message = _EXECUTORS[job.job_type](db, job)
        status, level, job.last_error = "success", "success", None
    except Exception as exc:  # noqa: BLE001 — any failure is captured, never raised to the caller
        message, status, level = str(exc), "error", "error"
        job.last_error = message
        logger.warning("Job %s (%s) failed: %s", job.id, job.job_type, message)

    job.last_status = status
    run = JobRun(job_id=job.id, status=status, message=message, started_at=started, finished_at=_now())
    db.add(run)
    db.add(Notification(
        user_id=job.user_id, organization_id=job.organization_id,
        title=f"{job.name}: {status}", body=message, level=level,
    ))
    db.commit()
    db.refresh(run)
    return run


# How close together two fires of the same job must be to count as the *same*
# fire by different workers. Comfortably longer than the clock skew and startup
# jitter between processes, comfortably shorter than the shortest useful
# schedule interval.
FIRE_DEDUPE_SECONDS = int(os.getenv("JOB_FIRE_DEDUPE_SECONDS", "30"))


def _claim_fire(db, job_id: str) -> ScheduledJob | None:
    """Atomically claim this firing of a job, or return None if someone else has.

    Every worker process runs its own in-process scheduler, so with N workers each
    schedule fires N times — N queries against the customer database, N
    notifications, N history rows. That is a correctness bug, not a performance
    one, and it appears only in the deployment shape (multi-worker) that is least
    likely to be tested.

    Rather than electing a leader, the *execution* is made idempotent: a
    conditional UPDATE stamps ``last_run_at`` only if it has not just been
    stamped. Exactly one worker's UPDATE matches a row; the losers see 0 rows
    affected and stand down. This needs no new table and — because the guarantee
    lives in the database — it holds however many schedulers happen to exist.

    Not a substitute for running the scheduler as a single process (see
    ``docs/PRE_DEPLOYMENT_REVIEW.md``); it is the safety net for when it is not.
    """
    now = _now()
    cutoff = now - timedelta(seconds=FIRE_DEDUPE_SECONDS)
    claimed = (
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.id == job_id,
            ScheduledJob.enabled.is_(True),
            # Unclaimed, or last claimed long enough ago to be a different fire.
            or_(ScheduledJob.last_run_at.is_(None), ScheduledJob.last_run_at < cutoff),
        )
        .update({ScheduledJob.last_run_at: now, ScheduledJob.last_status: "running"},
                synchronize_session=False)
    )
    db.commit()
    if not claimed:
        logger.debug("Job %s was already claimed by another worker for this fire.", job_id)
        return None
    return db.get(ScheduledJob, job_id)


def _run_job_by_id(job_id: str) -> None:
    """Scheduler entrypoint — runs on a background thread with its own session."""
    with SessionLocal() as db:
        job = _claim_fire(db, job_id)
        if job is not None:
            execute_job(db, job)


# ── Scheduler (APScheduler wrapper) ───────────────────────────────────────────

class JobScheduler:
    def __init__(self) -> None:
        self._sched: BackgroundScheduler | None = None

    @property
    def running(self) -> bool:
        return self._sched is not None

    def start(self) -> None:
        if self._sched is not None:
            return
        self._sched = BackgroundScheduler(daemon=True, timezone="UTC")
        self._sched.start()
        with SessionLocal() as db:
            for job in db.query(ScheduledJob).filter_by(enabled=True).all():
                self.sync_job(job)
        logger.info("Job scheduler started.")

    def shutdown(self) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=False)
            self._sched = None

    @staticmethod
    def _trigger(job: ScheduledJob):
        cfg = job.schedule_config or {}
        if job.schedule_kind == "interval":
            return IntervalTrigger(minutes=max(1, int(cfg.get("minutes", 60))))
        if job.schedule_kind == "daily":
            return CronTrigger(hour=int(cfg.get("hour", 6)), minute=int(cfg.get("minute", 0)))
        if job.schedule_kind == "weekly":
            return CronTrigger(
                day_of_week=cfg.get("day_of_week", "mon"),
                hour=int(cfg.get("hour", 6)), minute=int(cfg.get("minute", 0)),
            )
        return None  # "manual" → run-now only, never auto-scheduled

    def sync_job(self, job: ScheduledJob) -> None:
        """Register/refresh a job's trigger. No-op when the scheduler is off."""
        if self._sched is None:
            return
        self.remove_job(job.id)
        if not job.enabled:
            return
        trigger = self._trigger(job)
        if trigger is not None:
            self._sched.add_job(_run_job_by_id, trigger=trigger, args=[job.id], id=job.id, replace_existing=True)

    def remove_job(self, job_id: str) -> None:
        if self._sched is not None and self._sched.get_job(job_id) is not None:
            self._sched.remove_job(job_id)

    def next_run_at(self, job_id: str) -> datetime | None:
        if self._sched is None:
            return None
        job = self._sched.get_job(job_id)
        return job.next_run_time if job else None


scheduler = JobScheduler()
