"""Scheduled background jobs — user-owned report refreshes and context rebuilds.

Jobs are owned by the user who creates them (like charts/connections) and gated
by ``job:manage``. "Run now" executes inline; cron/interval schedules are driven
by the in-process scheduler.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_permission, write_audit
from ..jobs import JOB_TYPES, SCHEDULE_KINDS, execute_job, scheduler
from ..models import DatabaseConnection, JobRun, PublishedReport, ScheduledJob, User
from ..schemas import JobIn, JobOut, JobPatch, JobRunOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _validate_target(db: Session, user: User, job_type: str, target_ref: str) -> None:
    """The target must exist and belong to the caller."""
    if job_type == "report_refresh":
        report = db.get(PublishedReport, target_ref)
        if report is None or report.chart.user_id != user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Report not found or not yours.")
    elif job_type == "context_rebuild":
        conn = db.get(DatabaseConnection, target_ref)
        if conn is None or conn.user_id != user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connection not found or not yours.")


def _own_or_404(db: Session, job_id: str, user: User) -> ScheduledJob:
    job = db.get(ScheduledJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.get("", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    return (
        db.query(ScheduledJob)
        .filter(ScheduledJob.user_id == user.id)
        .order_by(ScheduledJob.created_at.desc())
        .all()
    )


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(req: JobIn, db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    if req.job_type not in JOB_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown job type: {req.job_type}")
    if req.schedule_kind not in SCHEDULE_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown schedule: {req.schedule_kind}")
    _validate_target(db, user, req.job_type, req.target_ref)

    job = ScheduledJob(
        user_id=user.id, organization_id=user.organization_id, name=req.name,
        job_type=req.job_type, target_ref=req.target_ref, schedule_kind=req.schedule_kind,
        schedule_config=req.schedule_config, enabled=req.enabled,
    )
    db.add(job)
    db.flush()
    write_audit(db, user_id=user.id, action="create", entity_type="job", entity_id=job.id,
                organization_id=user.organization_id, detail={"job_type": job.job_type, "schedule": job.schedule_kind})
    db.commit()
    db.refresh(job)

    scheduler.sync_job(job)
    job.next_run_at = scheduler.next_run_at(job.id)
    db.commit()
    db.refresh(job)
    return job


@router.patch("/{job_id}", response_model=JobOut)
def update_job(job_id: str, req: JobPatch, db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    job = _own_or_404(db, job_id, user)
    if req.name is not None:
        job.name = req.name
    if req.schedule_kind is not None:
        if req.schedule_kind not in SCHEDULE_KINDS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown schedule: {req.schedule_kind}")
        job.schedule_kind = req.schedule_kind
    if req.schedule_config is not None:
        job.schedule_config = req.schedule_config
    if req.enabled is not None:
        job.enabled = req.enabled
    db.commit()
    db.refresh(job)

    scheduler.sync_job(job)
    job.next_run_at = scheduler.next_run_at(job.id)
    write_audit(db, user_id=user.id, action="update", entity_type="job", entity_id=job.id,
                organization_id=user.organization_id, detail={"enabled": job.enabled})
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    job = _own_or_404(db, job_id, user)
    scheduler.remove_job(job.id)
    db.delete(job)
    write_audit(db, user_id=user.id, action="delete", entity_type="job", entity_id=job_id,
                organization_id=user.organization_id)
    db.commit()


@router.post("/{job_id}/run", response_model=JobRunOut)
def run_job_now(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    """Execute the job immediately (inline) and return the run record."""
    job = _own_or_404(db, job_id, user)
    write_audit(db, user_id=user.id, action="run", entity_type="job", entity_id=job.id,
                organization_id=user.organization_id)
    db.commit()
    return execute_job(db, job)


@router.get("/{job_id}/runs", response_model=list[JobRunOut])
def job_history(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("job:manage"))):
    _own_or_404(db, job_id, user)
    return (
        db.query(JobRun)
        .filter(JobRun.job_id == job_id)
        .order_by(JobRun.created_at.desc())
        .limit(50)
        .all()
    )
