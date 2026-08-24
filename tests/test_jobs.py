"""Milestone 6 — background jobs & notifications.

Covers job CRUD + ownership/permission gating, "Run now" inline execution (with
the heavy work mocked) recording a JobRun + a Notification, the failure path, and
the notification read flow. The APScheduler thread is disabled here
(DBBUDDY_DISABLE_SCHEDULER=1) — we exercise the executor directly via run-now.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_jobs_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "jobs-test-secret-key-long-enough-1")
os.environ.setdefault("APP_SECRET_KEY", "jobs-test-app-secret")
os.environ["DBBUDDY_DISABLE_SCHEDULER"] = "1"  # no background thread in tests

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Role, User  # noqa: E402
from app_db.schemas import ReportRunOut  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> str:
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()["access_token"]


def _set_role(email: str, role_name: str) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == role_name).one()]
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    analyst = _register(client, "owner@jobs.io")
    analyst2 = _register(client, "other@jobs.io")
    _register(client, "viewer@jobs.io")
    _set_role("viewer@jobs.io", "user")
    viewer = client.post("/auth/login", json={"email": "viewer@jobs.io", "password": "password123"}).json()["access_token"]

    conn = client.post("/connections", json={
        "name": "erp", "engine": "mysql", "host": "h", "username": "u", "password": "p", "database": "d",
    }, headers=_auth(analyst)).json()
    chart = client.post("/charts", json={"title": "Sales", "sql": "SELECT 1", "database_connection_id": conn["id"]},
                        headers=_auth(analyst)).json()
    report = client.post(f"/charts/{chart['id']}/publish", json={}, headers=_auth(analyst)).json()

    return {"analyst": analyst, "analyst2": analyst2, "viewer": viewer,
            "conn_id": conn["id"], "report_id": report["id"]}


# ── Permission + ownership ───────────────────────────────────────────────────

def test_jobs_require_job_manage(client, world):
    assert client.get("/jobs", headers=_auth(world["viewer"])).status_code == 403


def test_create_rejects_unowned_target(client, world):
    # analyst2 cannot schedule against analyst's connection
    res = client.post("/jobs", json={
        "name": "x", "job_type": "context_rebuild", "target_ref": world["conn_id"], "schedule_kind": "manual",
    }, headers=_auth(world["analyst2"]))
    assert res.status_code == 400


def test_create_and_list_job(client, world):
    res = client.post("/jobs", json={
        "name": "Nightly context", "job_type": "context_rebuild", "target_ref": world["conn_id"],
        "schedule_kind": "daily", "schedule_config": {"hour": 2, "minute": 0},
    }, headers=_auth(world["analyst"]))
    assert res.status_code == 201, res.text
    jid = res.json()["id"]
    jobs = client.get("/jobs", headers=_auth(world["analyst"])).json()
    assert any(j["id"] == jid for j in jobs)


# ── Run now (mocked executor) ────────────────────────────────────────────────

def _make_report_job(client, world):
    return client.post("/jobs", json={
        "name": "Refresh sales", "job_type": "report_refresh", "target_ref": world["report_id"],
        "schedule_kind": "manual",
    }, headers=_auth(world["analyst"])).json()


def test_run_now_success_records_run_and_notification(client, world, monkeypatch):
    monkeypatch.setattr(
        "app_db.routers.reports._execute_report",
        lambda chart, conn_cfg, organization_id=None: ReportRunOut(ok=True, columns=["a"], rows=[{"a": 1}, {"a": 2}]),
    )
    job = _make_report_job(client, world)
    run = client.post(f"/jobs/{job['id']}/run", headers=_auth(world["analyst"]))
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "success"

    runs = client.get(f"/jobs/{job['id']}/runs", headers=_auth(world["analyst"])).json()
    assert len(runs) == 1 and runs[0]["status"] == "success"

    # job's last_status reflects the run
    job_row = next(j for j in client.get("/jobs", headers=_auth(world["analyst"])).json() if j["id"] == job["id"])
    assert job_row["last_status"] == "success"

    # a success notification was created
    notifs = client.get("/notifications", headers=_auth(world["analyst"])).json()
    assert any(n["level"] == "success" and "Refresh sales" in n["title"] for n in notifs)


def test_run_now_failure_records_error(client, world, monkeypatch):
    monkeypatch.setattr(
        "app_db.routers.reports._execute_report",
        lambda chart, conn_cfg, organization_id=None: ReportRunOut(ok=False, needs_attention=True, message="source gone"),
    )
    job = _make_report_job(client, world)
    run = client.post(f"/jobs/{job['id']}/run", headers=_auth(world["analyst"])).json()
    assert run["status"] == "error" and "source gone" in run["message"]
    notifs = client.get("/notifications", headers=_auth(world["analyst"])).json()
    assert any(n["level"] == "error" for n in notifs)


def test_run_now_is_owner_scoped(client, world):
    job = _make_report_job(client, world)
    assert client.post(f"/jobs/{job['id']}/run", headers=_auth(world["analyst2"])).status_code == 404


# ── Notifications flow ───────────────────────────────────────────────────────

def test_notifications_read_flow(client, world, monkeypatch):
    monkeypatch.setattr(
        "app_db.routers.reports._execute_report",
        lambda chart, conn_cfg, organization_id=None: ReportRunOut(ok=True, columns=[], rows=[]),
    )
    job = _make_report_job(client, world)
    client.post(f"/jobs/{job['id']}/run", headers=_auth(world["analyst"]))

    before = client.get("/notifications/unread-count", headers=_auth(world["analyst"])).json()["count"]
    assert before >= 1
    client.post("/notifications/read-all", headers=_auth(world["analyst"]))
    after = client.get("/notifications/unread-count", headers=_auth(world["analyst"])).json()["count"]
    assert after == 0


def test_delete_job(client, world):
    job = _make_report_job(client, world)
    assert client.delete(f"/jobs/{job['id']}", headers=_auth(world["analyst"])).status_code == 204
    assert all(j["id"] != job["id"] for j in client.get("/jobs", headers=_auth(world["analyst"])).json())


def test_concurrent_schedulers_fire_a_job_only_once(client, world, monkeypatch):
    """Regression: every worker process runs its own APScheduler, so with N
    workers each schedule fired N times — N queries against the customer
    database, N notifications, N history rows. The claim makes execution
    idempotent regardless of how many schedulers exist."""
    import threading

    from app_db.jobs import _run_job_by_id

    monkeypatch.setattr(
        "app_db.routers.reports._execute_report",
        lambda chart, conn_cfg, organization_id=None: ReportRunOut(
            ok=True, columns=["a"], rows=[{"a": 1}]),
    )
    job = _make_report_job(client, world)

    # Four "workers" firing the same schedule at the same instant.
    threads = [threading.Thread(target=_run_job_by_id, args=(job["id"],)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    runs = client.get(f"/jobs/{job['id']}/runs", headers=_auth(world["analyst"])).json()
    assert len(runs) == 1, f"job executed {len(runs)} times — the fire claim did not hold"


def test_a_later_fire_of_the_same_job_is_not_suppressed(client, world, monkeypatch):
    # The claim must dedupe *simultaneous* fires, not block the next scheduled one.
    from app_db.jobs import _run_job_by_id

    monkeypatch.setattr(
        "app_db.routers.reports._execute_report",
        lambda chart, conn_cfg, organization_id=None: ReportRunOut(
            ok=True, columns=["a"], rows=[{"a": 1}]),
    )
    monkeypatch.setattr("app_db.jobs.FIRE_DEDUPE_SECONDS", 0)
    job = _make_report_job(client, world)

    _run_job_by_id(job["id"])
    _run_job_by_id(job["id"])

    runs = client.get(f"/jobs/{job['id']}/runs", headers=_auth(world["analyst"])).json()
    assert len(runs) == 2
