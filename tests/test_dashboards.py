"""API tests for dashboards — analyst authoring, publishing, and client viewing.

Covers the pin flow, per-chart descriptions (manual + AI), publication-record
semantics (mirroring published_reports), the client run path with its parallel
execution and short-TTL cache, per-chart failure isolation, RBAC, and cleanup on
user hard-delete.
"""

import importlib
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_dashboards_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "dashboards-test-secret-key-long-enough-12345")
os.environ.setdefault("APP_SECRET_KEY", "dashboards-test-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import (  # noqa: E402
    Dashboard,
    DashboardItem,
    DatabaseConnection,
    PublishedDashboard,
    Role,
    SavedChart,
    User,
)
from app_db.security import encrypt_secret  # noqa: E402

ROWS = [{"month": "2026-01", "revenue": 100}, {"month": "2026-02", "revenue": 120}]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _register(client, email: str) -> None:
    assert client.post("/auth/register",
                       json={"email": email, "password": "password123"}).status_code in (201, 409)


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _set_roles(email: str, roles: list[str]) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == n).one() for n in roles]
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    _register(client, "dash-analyst@test.com")
    _register(client, "dash-client@test.com")
    _set_roles("dash-analyst@test.com", ["analyst"])
    _set_roles("dash-client@test.com", ["user"])
    analyst_token = _login(client, "dash-analyst@test.com")
    client_token = _login(client, "dash-client@test.com")

    with SessionLocal() as db:
        analyst = db.query(User).filter(User.email == "dash-analyst@test.com").one()
        conn = DatabaseConnection(
            user_id=analyst.id, name="Sales", engine="mysql", host="localhost",
            username="u", password_encrypted=encrypt_secret("p"), database="sales",
        )
        db.add(conn)
        db.commit()
        conn_id = conn.id
        analyst_id = analyst.id

    return {"analyst": analyst_token, "client": client_token,
            "conn": conn_id, "analyst_id": analyst_id}


@pytest.fixture(autouse=True)
def _clean(world):
    with SessionLocal() as db:
        db.query(PublishedDashboard).delete()
        db.query(DashboardItem).delete()
        db.query(Dashboard).delete()
        db.query(SavedChart).delete()
        db.commit()
    yield


def _make_chart(client, world, title="Revenue", sql="SELECT month, revenue FROM sales"):
    res = client.post("/charts", json={
        "title": title, "sql": sql, "chart_type": "bar", "nl_query": "monthly revenue",
        "database_connection_id": world["conn"],
    }, headers=_auth(world["analyst"]))
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _make_dashboard(client, world, title="Q1 Review"):
    res = client.post("/dashboards", json={"title": title},
                      headers=_auth(world["analyst"]))
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _stub_query(rows=None, fail=False):
    """Patch the live-DB layer used by chart_runtime."""
    def _connect(*a, **k):
        if fail:
            return None
        return object()

    def _execute(_conn, _sql):
        return ROWS if rows is None else rows

    return patch("dbbuddy_core.db.connect_db", _connect), \
        patch("dbbuddy_core.query.execute_query", _execute)


# ── RBAC ─────────────────────────────────────────────────────────────────────

def test_client_cannot_author_dashboards(client, world):
    res = client.post("/dashboards", json={"title": "Nope"}, headers=_auth(world["client"]))
    assert res.status_code == 403


def test_unauthenticated_is_refused(client, world):
    assert client.get("/dashboards").status_code == 401


# ── Authoring ────────────────────────────────────────────────────────────────

def test_pin_chart_to_dashboard(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    res = client.post(f"/dashboards/{dash}/items", json={"chart_id": chart},
                      headers=_auth(world["analyst"]))
    assert res.status_code == 201, res.text
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["chart_id"] == chart
    assert items[0]["title"] == "Revenue"


def test_repinning_the_same_chart_updates_instead_of_duplicating(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    res = client.post(f"/dashboards/{dash}/items",
                      json={"chart_id": chart, "description": "Second pin"}, headers=hdr)
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["description"] == "Second pin"


def test_cannot_pin_another_users_chart(client, world):
    dash = _make_dashboard(client, world)
    _register(client, "dash-other@test.com")
    _set_roles("dash-other@test.com", ["analyst"])
    other = _login(client, "dash-other@test.com")
    res = client.post("/charts", json={"title": "Theirs", "sql": "SELECT 1", "chart_type": "bar"},
                      headers=_auth(other))
    foreign_chart = res.json()["id"]
    res = client.post(f"/dashboards/{dash}/items", json={"chart_id": foreign_chart},
                      headers=_auth(world["analyst"]))
    assert res.status_code == 404


def test_another_analysts_dashboard_is_not_reachable(client, world):
    dash = _make_dashboard(client, world)
    _register(client, "dash-other2@test.com")
    _set_roles("dash-other2@test.com", ["analyst"])
    other = _login(client, "dash-other2@test.com")
    assert client.patch(f"/dashboards/{dash}", json={"title": "Hijacked"},
                        headers=_auth(other)).status_code == 404


def test_description_is_per_pin_not_per_chart(client, world):
    # The same chart in two dashboards carries a different narrative in each.
    chart = _make_chart(client, world)
    a, b = _make_dashboard(client, world, "A"), _make_dashboard(client, world, "B")
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{a}/items", json={"chart_id": chart, "description": "In A"}, headers=hdr)
    res = client.post(f"/dashboards/{b}/items",
                      json={"chart_id": chart, "description": "In B"}, headers=hdr)
    assert res.json()["items"][0]["description"] == "In B"
    other = client.get(f"/dashboards/{a}", headers=hdr).json()
    assert other["items"][0]["description"] == "In A"


def test_unpin_closes_the_position_gap(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    ids = []
    for i in range(3):
        chart = _make_chart(client, world, title=f"C{i}")
        body = client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr).json()
        ids = [it["id"] for it in body["items"]]
    res = client.delete(f"/dashboards/{dash}/items/{ids[0]}", headers=hdr)
    assert [i["position"] for i in res.json()["items"]] == [0, 1]


def test_reorder_requires_the_exact_item_set(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    for i in range(2):
        client.post(f"/dashboards/{dash}/items",
                    json={"chart_id": _make_chart(client, world, title=f"C{i}")}, headers=hdr)
    body = client.get(f"/dashboards/{dash}", headers=hdr).json()
    ids = [i["id"] for i in body["items"]]

    # A partial list is rejected — a half-applied order is worse than none.
    assert client.post(f"/dashboards/{dash}/reorder", json={"item_ids": ids[:1]},
                       headers=hdr).status_code == 400

    res = client.post(f"/dashboards/{dash}/reorder",
                      json={"item_ids": list(reversed(ids))}, headers=hdr)
    assert [i["id"] for i in res.json()["items"]] == list(reversed(ids))


def test_deleting_a_chart_removes_its_pin(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    assert client.delete(f"/charts/{chart}", headers=hdr).status_code in (200, 204)
    assert client.get(f"/dashboards/{dash}", headers=hdr).json()["items"] == []


def test_oversized_description_is_rejected(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    res = client.post(f"/dashboards/{dash}/items",
                      json={"chart_id": chart, "description": "x" * 5000},
                      headers=_auth(world["analyst"]))
    assert res.status_code == 422


# ── Publishing (publication-record semantics) ────────────────────────────────

def test_publishing_an_empty_dashboard_is_refused(client, world):
    dash = _make_dashboard(client, world)
    res = client.post(f"/dashboards/{dash}/publish", headers=_auth(world["analyst"]))
    assert res.status_code == 409


def test_publish_then_client_sees_it(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    assert client.post(f"/dashboards/{dash}/publish", headers=hdr).status_code == 200

    listed = client.get("/dashboards/published/list", headers=_auth(world["client"]))
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()] == [dash]


def test_republishing_reuses_the_same_record(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    client.post(f"/dashboards/{dash}/publish", headers=hdr)
    client.post(f"/dashboards/{dash}/unpublish", headers=hdr)
    client.post(f"/dashboards/{dash}/publish", headers=hdr)
    with SessionLocal() as db:
        assert db.query(PublishedDashboard).filter_by(dashboard_id=dash).count() == 1


def test_unpublish_hides_it_from_clients_without_deleting_the_record(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    client.post(f"/dashboards/{dash}/publish", headers=hdr)
    client.post(f"/dashboards/{dash}/unpublish", headers=hdr)

    assert client.get("/dashboards/published/list", headers=_auth(world["client"])).json() == []
    with SessionLocal() as db:
        row = db.query(PublishedDashboard).filter_by(dashboard_id=dash).one()
        assert row.status == "revoked"  # revoked, not deleted — history survives


def test_edits_to_the_draft_reach_a_published_dashboard(client, world):
    # The publication is a record, not a copy: the client always sees the live draft.
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    client.post(f"/dashboards/{dash}/publish", headers=hdr)
    client.patch(f"/dashboards/{dash}", json={"title": "Renamed"}, headers=hdr)

    seen = client.get("/dashboards/published/list", headers=_auth(world["client"])).json()
    assert seen[0]["title"] == "Renamed"


def test_unpublished_dashboard_is_not_reachable_by_a_client(client, world):
    chart = _make_chart(client, world)
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    assert client.get(f"/dashboards/{dash}", headers=_auth(world["client"])).status_code == 404


# ── Running (live refresh) ───────────────────────────────────────────────────

def test_run_returns_live_rows_for_every_chart(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    for i in range(3):
        client.post(f"/dashboards/{dash}/items",
                    json={"chart_id": _make_chart(client, world, title=f"C{i}")}, headers=hdr)

    connect_p, execute_p = _stub_query()
    with connect_p, execute_p:
        res = client.post(f"/dashboards/{dash}/run", headers=hdr)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["charts"]) == 3
    assert all(c["ok"] for c in body["charts"])
    assert body["charts"][0]["rows"] == ROWS
    assert body["charts"][0]["columns"] == ["month", "revenue"]
    assert body["charts"][0]["fetched_at"]  # freshness is always reported


def test_charts_are_returned_in_analyst_order_not_completion_order(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    titles = ["First", "Second", "Third"]
    for t in titles:
        client.post(f"/dashboards/{dash}/items",
                    json={"chart_id": _make_chart(client, world, title=t)}, headers=hdr)
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p:
        body = client.post(f"/dashboards/{dash}/run", headers=hdr).json()
    assert [c["title"] for c in body["charts"]] == titles


def test_one_failing_chart_does_not_blank_the_dashboard(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    good = _make_chart(client, world, title="Good")
    bad = _make_chart(client, world, title="Bad", sql="SELECT boom FROM nowhere")
    client.post(f"/dashboards/{dash}/items", json={"chart_id": good}, headers=hdr)
    client.post(f"/dashboards/{dash}/items", json={"chart_id": bad}, headers=hdr)

    def _execute(_conn, sql):
        if "boom" in sql:
            raise RuntimeError("table does not exist")
        return ROWS

    with patch("dbbuddy_core.db.connect_db", lambda *a, **k: object()), \
         patch("dbbuddy_core.query.execute_query", _execute):
        body = client.post(f"/dashboards/{dash}/run", headers=hdr).json()

    by_title = {c["title"]: c for c in body["charts"]}
    assert by_title["Good"]["ok"] is True and by_title["Good"]["rows"] == ROWS
    assert by_title["Bad"]["ok"] is False
    assert by_title["Bad"]["needs_attention"] is True


def test_a_write_query_is_blocked_on_refresh(client, world):
    # Defence in depth: a client-triggered refresh must only ever issue a read.
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    chart = _make_chart(client, world, title="Bad", sql="DELETE FROM sales")
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p:
        body = client.post(f"/dashboards/{dash}/run", headers=hdr).json()
    assert body["charts"][0]["ok"] is False
    assert "not read-only" in body["charts"][0]["message"]


def test_a_chart_whose_connection_is_gone_needs_attention(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    res = client.post("/charts", json={"title": "Orphan", "sql": "SELECT 1", "chart_type": "bar"},
                      headers=hdr)  # no database_connection_id
    client.post(f"/dashboards/{dash}/items", json={"chart_id": res.json()["id"]}, headers=hdr)
    body = client.post(f"/dashboards/{dash}/run", headers=hdr).json()
    assert body["charts"][0]["needs_attention"] is True
    assert "no longer available" in body["charts"][0]["message"]


def test_analyst_can_preview_an_unpublished_dashboard(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items",
                json={"chart_id": _make_chart(client, world)}, headers=hdr)
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p:
        assert client.post(f"/dashboards/{dash}/run", headers=hdr).status_code == 200


def test_client_cannot_run_an_unpublished_dashboard(client, world):
    dash = _make_dashboard(client, world)
    client.post(f"/dashboards/{dash}/items",
                json={"chart_id": _make_chart(client, world)},
                headers=_auth(world["analyst"]))
    assert client.post(f"/dashboards/{dash}/run",
                       headers=_auth(world["client"])).status_code == 404


# ── AI descriptions ──────────────────────────────────────────────────────────

def test_ai_description_is_suggested_but_not_saved(client, world):
    # An AI sentence must never land in a published dashboard without a human
    # reading it, so /describe returns a draft and saves nothing.
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    body = client.post(f"/dashboards/{dash}/items",
                       json={"chart_id": _make_chart(client, world)}, headers=hdr).json()
    item_id = body["items"][0]["id"]

    from dbbuddy_core.ai_providers import ProviderRuntimeConfig

    chain = [ProviderRuntimeConfig(adapter="openai_compatible", model="m",
                                   base_url="http://x", api_key="k", name="P")]
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p, \
         patch("app_db.ai_runtime.resolve_active_provider_chain", lambda org: chain), \
         patch("dbbuddy_core.ai_providers.OpenAICompatibleProvider.generate",
               lambda self, p, **k: '{"summary": "Revenue rose from 100 to 120."}'):
        res = client.post(f"/dashboards/{dash}/items/{item_id}/describe",
                          json={}, headers=hdr)

    assert res.status_code == 200, res.text
    assert res.json()["description"] == "Revenue rose from 100 to 120."
    # Nothing persisted until the analyst accepts it.
    assert client.get(f"/dashboards/{dash}", headers=hdr).json()["items"][0]["description"] is None


def test_ai_description_inherits_the_insights_guardrails(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    body = client.post(f"/dashboards/{dash}/items",
                       json={"chart_id": _make_chart(client, world)}, headers=hdr).json()
    item_id = body["items"][0]["id"]

    from dbbuddy_core.ai_providers import ProviderRuntimeConfig

    chain = [ProviderRuntimeConfig(adapter="openai_compatible", model="m",
                                   base_url="http://x", api_key="k", name="P")]
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p, \
         patch("app_db.ai_runtime.resolve_active_provider_chain", lambda org: chain), \
         patch("dbbuddy_core.ai_providers.OpenAICompatibleProvider.generate",
               lambda self, p, **k: '{"summary": "Revenue rose because of good weather."}'):
        res = client.post(f"/dashboards/{dash}/items/{item_id}/describe", json={}, headers=hdr)

    assert res.json()["description"] == "I cannot determine that from the available data."


def test_accepted_description_is_tagged_as_ai_and_editing_makes_it_manual(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    body = client.post(f"/dashboards/{dash}/items",
                       json={"chart_id": _make_chart(client, world)}, headers=hdr).json()
    item_id = body["items"][0]["id"]

    res = client.post(f"/dashboards/{dash}/items/{item_id}/describe/accept",
                      json={"description": "Revenue rose 20%."}, headers=hdr)
    assert res.json()["items"][0]["description_source"] == "ai"

    res = client.patch(f"/dashboards/{dash}/items/{item_id}",
                       json={"description": "My own words."}, headers=hdr)
    assert res.json()["items"][0]["description_source"] == "manual"


# ── Cleanup ──────────────────────────────────────────────────────────────────

def test_hard_deleting_a_user_removes_their_dashboards(client, world):
    _register(client, "dash-doomed@test.com")
    _set_roles("dash-doomed@test.com", ["analyst"])
    doomed = _login(client, "dash-doomed@test.com")
    chart = client.post("/charts", json={"title": "T", "sql": "SELECT 1", "chart_type": "bar"},
                        headers=_auth(doomed)).json()["id"]
    dash = client.post("/dashboards", json={"title": "Doomed"},
                       headers=_auth(doomed)).json()["id"]
    client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=_auth(doomed))
    client.post(f"/dashboards/{dash}/publish", headers=_auth(doomed))

    with SessionLocal() as db:
        target = db.query(User).filter(User.email == "dash-doomed@test.com").one()
        from app_db.routers.admin import _purge_user_owned_data

        _purge_user_owned_data(db, target.id)
        db.delete(target)
        db.commit()

    with SessionLocal() as db:
        assert db.query(Dashboard).filter_by(id=dash).count() == 0
        assert db.query(DashboardItem).filter_by(dashboard_id=dash).count() == 0
        assert db.query(PublishedDashboard).filter_by(dashboard_id=dash).count() == 0


# ── QA regressions (stress pass) ─────────────────────────────────────────────

def test_a_runaway_chart_is_capped_and_says_so(client, world):
    # Measured before the cap: a 200k-row chart produced a 47 MB response, which
    # a dashboard would then multiply by its chart count.
    from app_db.chart_runtime import MAX_CHART_ROWS

    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items",
                json={"chart_id": _make_chart(client, world, sql="SELECT * FROM huge")},
                headers=hdr)

    big = [{"id": i, "v": i} for i in range(MAX_CHART_ROWS + 500)]
    with patch("dbbuddy_core.db.connect_db", lambda *a, **k: object()), \
         patch("dbbuddy_core.query.execute_query", lambda *a: big):
        chart = client.post(f"/dashboards/{dash}/run", headers=hdr).json()["charts"][0]

    assert chart["ok"] is True
    assert len(chart["rows"]) == MAX_CHART_ROWS
    # Truncation is disclosed, never silent: the true total comes back too.
    assert chart["truncated"] is True
    assert chart["row_count"] == MAX_CHART_ROWS + 500


def test_a_result_within_the_cap_is_not_marked_truncated(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items",
                json={"chart_id": _make_chart(client, world)}, headers=hdr)
    connect_p, execute_p = _stub_query()
    with connect_p, execute_p:
        chart = client.post(f"/dashboards/{dash}/run", headers=hdr).json()["charts"][0]
    assert chart["truncated"] is False
    assert chart["row_count"] == len(ROWS) == len(chart["rows"])


def test_oversized_results_are_not_written_to_the_cache(client, world):
    # Redis is a latency optimization, not a blob store — a few huge entries would
    # evict everything useful.
    from app_db import chart_runtime

    job = chart_runtime.ChartJob(
        chart_id="c1", sql="SELECT * FROM huge", chart_type="bar", config=None,
        engine="mysql", host="h", port=None, database="d", username="u", password="p",
    )
    big = [{"v": i} for i in range(chart_runtime.MAX_CACHEABLE_ROWS + 1)]
    writes = []

    class _Cache:
        connected = True

        def get(self, *a, **k):
            return None

        def set(self, *a, **k):
            writes.append(a)
            return True

    with patch("dbbuddy_core.db.connect_db", lambda *a, **k: object()), \
         patch("dbbuddy_core.query.execute_query", lambda *a: big), \
         patch("app_db.chart_runtime._get_cache", lambda: _Cache()):
        result = chart_runtime.execute_chart(job)

    assert result.ok is True
    assert writes == []  # computed and returned, but not cached


def test_a_dashboard_cannot_grow_without_bound(client, world):
    # A dashboard's chart count *is* its cost against the customer database on
    # every open, so it is capped where it grows.
    from app_db.routers.dashboards import MAX_CHARTS_PER_DASHBOARD

    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    with patch("app_db.routers.dashboards.MAX_CHARTS_PER_DASHBOARD", 3):
        for i in range(3):
            res = client.post(f"/dashboards/{dash}/items",
                              json={"chart_id": _make_chart(client, world, title=f"c{i}")},
                              headers=hdr)
            assert res.status_code == 201
        res = client.post(f"/dashboards/{dash}/items",
                          json={"chart_id": _make_chart(client, world, title="over")},
                          headers=hdr)
    assert res.status_code == 409
    assert "at most" in res.json()["detail"]
    assert MAX_CHARTS_PER_DASHBOARD > 0


def test_repinning_an_existing_chart_still_works_at_the_cap(client, world):
    # The cap must not block *updating* a pin that is already there.
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    chart = _make_chart(client, world)
    with patch("app_db.routers.dashboards.MAX_CHARTS_PER_DASHBOARD", 1):
        assert client.post(f"/dashboards/{dash}/items", json={"chart_id": chart},
                           headers=hdr).status_code == 201
        res = client.post(f"/dashboards/{dash}/items",
                          json={"chart_id": chart, "description": "Updated"}, headers=hdr)
    assert res.status_code == 201
    assert res.json()["items"][0]["description"] == "Updated"


def test_reorder_rejects_a_list_with_repeats(client, world):
    # Comparing sets alone accepted [a, b, b] — the last occurrence won and left a
    # sparse order like [0, 2].
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    for i in range(2):
        body = client.post(f"/dashboards/{dash}/items",
                           json={"chart_id": _make_chart(client, world, title=f"c{i}")},
                           headers=hdr).json()
    ids = [i["id"] for i in body["items"]]
    res = client.post(f"/dashboards/{dash}/reorder",
                      json={"item_ids": [ids[0], ids[1], ids[1]]}, headers=hdr)
    assert res.status_code == 400
    # Order is untouched by the rejected request.
    after = client.get(f"/dashboards/{dash}", headers=hdr).json()
    assert [i["position"] for i in after["items"]] == [0, 1]


def test_repeated_pins_of_one_chart_never_duplicate(client, world):
    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    chart = _make_chart(client, world)
    for _ in range(5):
        client.post(f"/dashboards/{dash}/items", json={"chart_id": chart}, headers=hdr)
    with SessionLocal() as db:
        assert db.query(DashboardItem).filter_by(dashboard_id=dash).count() == 1


def test_published_list_route_is_not_shadowed_by_the_id_route(client, world):
    # /dashboards/published/list must not be parsed as /dashboards/{id}.
    res = client.get("/dashboards/published/list", headers=_auth(world["analyst"]))
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_running_a_wide_dashboard_does_not_issue_a_query_per_chart(client, world):
    # Guards against an N+1 regression: items and their charts are eager-loaded,
    # so app-DB round-trips must not scale with the chart count.
    from sqlalchemy import event

    from app_db.database import engine as app_engine

    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    for i in range(12):
        client.post(f"/dashboards/{dash}/items",
                    json={"chart_id": _make_chart(client, world, title=f"c{i}")}, headers=hdr)

    counter = {"n": 0}

    def _count(*_a, **_k):
        counter["n"] += 1

    event.listen(app_engine, "before_cursor_execute", _count)
    try:
        connect_p, execute_p = _stub_query()
        with connect_p, execute_p:
            client.post(f"/dashboards/{dash}/run", headers=hdr)
    finally:
        event.remove(app_engine, "before_cursor_execute", _count)

    # A per-chart lookup would put this in the 20s; eager loading keeps it flat.
    assert counter["n"] < 15, f"{counter['n']} app-DB queries for 12 charts — N+1 regression"


def test_a_saturated_target_reads_as_busy_not_broken(client, world):
    # A healthy database that is simply saturated by our own traffic must not be
    # reported as a broken chart — the operator response is completely different.
    from dbbuddy_core import erp_concurrency

    dash = _make_dashboard(client, world)
    hdr = _auth(world["analyst"])
    client.post(f"/dashboards/{dash}/items",
                json={"chart_id": _make_chart(client, world)}, headers=hdr)

    def _busy(*a, **k):
        raise erp_concurrency.ERPBusy("This database is handling too many requests right now")

    with patch("dbbuddy_core.erp_concurrency.query_slot", _busy):
        chart = client.post(f"/dashboards/{dash}/run", headers=hdr).json()["charts"][0]

    assert chart["ok"] is False
    assert "too many requests" in chart["message"]


def test_cached_results_never_cross_an_organization_boundary(client, world):
    # Two orgs with identical connection details would read identical rows, so
    # sharing is *technically* fine — but "can org A see org B's cached data?"
    # should answer no, not "no, because of an argument about connection tuples".
    from app_db.chart_runtime import ChartJob, _cache_key

    common = dict(chart_id="c", sql="SELECT 1", chart_type="bar", config=None,
                  engine="mysql", host="h", port=3306, database="d", username="u")
    assert _cache_key(ChartJob(organization_id="org-a", **common)) != \
        _cache_key(ChartJob(organization_id="org-b", **common))
    # …and the same org still shares, so the cache remains useful.
    assert _cache_key(ChartJob(organization_id="org-a", **common)) == \
        _cache_key(ChartJob(organization_id="org-a", **common))
