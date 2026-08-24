"""Unified AI provider management.

Two layers:
  * engine registry (`dbbuddy_core.ai_providers`) — adapter dispatch, capability
    flags, and the fallback chain, with no app DB;
  * the per-org `/ai-providers` CRUD API — isolation, gating, activate (single
    default), duplicate (clones the key), fallback-cycle rejection, and the
    healthcheck-based connection test.
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


# ── Engine registry (no app DB) ───────────────────────────────────────────────

from dbbuddy_core import ai_providers as aip  # noqa: E402


def test_get_provider_dispatches_by_adapter():
    oai = aip.get_provider(aip.ProviderRuntimeConfig(adapter="openai_compatible", model="m", base_url="http://x", api_key="k"))
    oll = aip.get_provider(aip.ProviderRuntimeConfig(adapter="ollama", model="m"))
    assert isinstance(oai, aip.OpenAICompatibleProvider)
    assert isinstance(oll, aip.OllamaProvider)


def test_unknown_adapter_raises():
    with pytest.raises(ValueError):
        aip.get_provider(aip.ProviderRuntimeConfig(adapter="does-not-exist", model="m"))


def test_capability_is_declared():
    assert aip.OpenAICompatibleProvider.capabilities.supports_json_mode is True
    assert aip.OpenAICompatibleProvider.capabilities.supports_embeddings is False


def test_chain_falls_through_on_recoverable_error(monkeypatch):
    """First provider fails with a recoverable error → the second answers."""
    calls: list[str] = []

    class _Fake:
        def __init__(self, cfg):
            self.cfg = cfg

        def generate(self, prompt):
            calls.append(self.cfg.name)
            if self.cfg.name == "primary":
                raise aip.RecoverableProviderError("primary down")
            return '{"users.id": "identifier"}'

    monkeypatch.setattr(aip, "get_provider", lambda cfg: _Fake(cfg))
    chain = [
        aip.ProviderRuntimeConfig(adapter="ollama", model="m", name="primary"),
        aip.ProviderRuntimeConfig(adapter="openai_compatible", model="m", name="secondary"),
    ]
    out = aip.classify_columns(["users.id"], {"users": ["id"]}, chain)
    assert calls == ["primary", "secondary"]
    assert out["users.id"]["term"] == "identifier"
    assert out["users.id"]["provider"] == "secondary"


def test_chain_exhausted_degrades_to_rule_based(monkeypatch):
    class _AlwaysFail:
        def __init__(self, cfg):
            self.cfg = cfg

        def generate(self, prompt):
            raise aip.RecoverableProviderError("down")

    monkeypatch.setattr(aip, "get_provider", lambda cfg: _AlwaysFail(cfg))
    chain = [aip.ProviderRuntimeConfig(adapter="ollama", model="m", name="only")]
    out = aip.classify_columns(["orders.created_at"], {"orders": ["created_at"]}, chain)
    # Rule-based fallback: no AI provenance, a normalized non-empty term (the
    # table-prefix strip happens later, in ai_refine).
    assert out["orders.created_at"]["provider"] is None
    assert out["orders.created_at"]["term"] and "created at" in out["orders.created_at"]["term"]


def test_empty_chain_is_rule_based():
    out = aip.classify_columns(["t.a"], {"t": ["a"]}, [])
    assert out["t.a"]["provider"] is None


def test_ollama_non_json_response_is_recoverable(monkeypatch):
    """A 200 with a non-JSON body must fail over cleanly (like the OpenAI-compatible
    sibling), not let a raw ValueError escape the transport."""
    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(aip.requests, "post", lambda *a, **k: _Resp())
    provider = aip.OllamaProvider(aip.ProviderRuntimeConfig(adapter="ollama", model="m"))
    with pytest.raises(aip.RecoverableProviderError):
        provider.generate("hi")


# ── API / CRUD ────────────────────────────────────────────────────────────────

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_ai_providers.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "ai-providers-secret-key-long-enough-123456")
os.environ.setdefault("APP_SECRET_KEY", "ai-providers-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Organization, Role, User  # noqa: E402
from app_db.slug import unique_slug  # noqa: E402


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _register(client, email: str) -> None:
    r = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code in (201, 409), r.text


def _login(client, email: str) -> str:
    r = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _set_roles(email: str, roles: list[str], org_id: str | None = None) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == n).one() for n in roles]
        if org_id is not None:
            u.organization_id = org_id
        db.commit()


def _make_org(name: str) -> str:
    with SessionLocal() as db:
        o = Organization(name=name, slug=unique_slug(db, name))
        db.add(o)
        db.commit()
        return o.id


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def analyst(client):
    _register(client, "aipadmin@x.io")  # analyst → settings:ai
    return _login(client, "aipadmin@x.io")


def _create(client, token, **over):
    body = {
        "name": over.get("name", "OpenRouter"),
        "adapter": over.get("adapter", "openai_compatible"),
        "base_url": over.get("base_url", "https://openrouter.ai/api/v1"),
        "model": over.get("model", "anthropic/claude-sonnet-4"),
        "api_key": over.get("api_key", "sk-test"),
    }
    if "fallback_provider_id" in over:
        body["fallback_provider_id"] = over["fallback_provider_id"]
    return client.post("/ai-providers", json=body, headers=_auth(token))


def test_bootstrap_created_local_ollama(client, analyst):
    rows = client.get("/ai-providers", headers=_auth(analyst)).json()
    local = [r for r in rows if r["name"] == "Local Ollama"]
    assert local and local[0]["adapter"] == "ollama"


def test_bootstrap_activates_local_ollama_for_a_fresh_org():
    """The activation half of the contract, on an org of its own.

    Asserting it through the shared client made the test order-dependent: the
    default org is shared with every other provider test, activating a provider
    there is exactly what those tests do, and `bootstrap_ai_providers` only
    activates Local Ollama when the org has *no* active provider. Under
    `pytest --random-order` this failed whenever an activation ran first — a
    defect in the test's isolation, not in the bootstrap.
    """
    from app_db.database import SessionLocal
    from app_db.models import AIProviderConfig, Organization
    from app_db.seed import bootstrap_ai_providers

    db = SessionLocal()
    try:
        tag = os.urandom(4).hex()
        org = Organization(name=f"bootstrap-probe-{tag}", slug=f"bootstrap-probe-{tag}")
        db.add(org)
        db.flush()
        bootstrap_ai_providers(db, org)
        local = (db.query(AIProviderConfig)
                 .filter_by(organization_id=org.id, name="Local Ollama").one())
        assert local.adapter == "ollama"
        assert local.priority == 1  # active default from bootstrap
    finally:
        db.rollback()
        db.close()


def test_crud_and_key_never_returned(client, analyst):
    r = _create(client, analyst, name="CRUD One")
    assert r.status_code == 201, r.text
    body = r.json()
    assert "api_key" not in body and body["has_key"] is True and body["credentials_ok"] is True
    pid = body["id"]

    # Update: blank api_key keeps the stored key.
    u = client.patch(f"/ai-providers/{pid}", json={"model": "gpt-5"}, headers=_auth(analyst))
    assert u.status_code == 200 and u.json()["model"] == "gpt-5" and u.json()["has_key"] is True

    client.delete(f"/ai-providers/{pid}", headers=_auth(analyst))
    assert all(x["id"] != pid for x in client.get("/ai-providers", headers=_auth(analyst)).json())


def test_activate_yields_single_default(client, analyst):
    a = _create(client, analyst, name="Active A").json()
    b = _create(client, analyst, name="Active B").json()
    client.post(f"/ai-providers/{a['id']}/activate", headers=_auth(analyst))
    client.post(f"/ai-providers/{b['id']}/activate", headers=_auth(analyst))
    actives = [x for x in client.get("/ai-providers", headers=_auth(analyst)).json() if x["is_active"]]
    assert len(actives) == 1 and actives[0]["id"] == b["id"]


def test_duplicate_clones_key(client, analyst):
    src = _create(client, analyst, name="Dup Source").json()
    d = client.post(f"/ai-providers/{src['id']}/duplicate", headers=_auth(analyst))
    assert d.status_code == 201, d.text
    assert d.json()["name"] == "Dup Source (copy)" and d.json()["has_key"] is True
    assert d.json()["is_active"] is False


def test_fallback_cycle_rejected(client, analyst):
    a = _create(client, analyst, name="Cycle A").json()
    b = _create(client, analyst, name="Cycle B", fallback_provider_id=a["id"]).json()
    # a → b → a would be a cycle.
    r = client.patch(f"/ai-providers/{a['id']}", json={"fallback_provider_id": b["id"]}, headers=_auth(analyst))
    assert r.status_code == 400


def test_delete_nulls_inbound_fallback(client, analyst):
    target = _create(client, analyst, name="FB Target").json()
    src = _create(client, analyst, name="FB Source", fallback_provider_id=target["id"]).json()
    client.delete(f"/ai-providers/{target['id']}", headers=_auth(analyst))
    refreshed = client.get(f"/ai-providers/{src['id']}", headers=_auth(analyst)).json()
    assert refreshed["fallback_provider_id"] is None


def test_validation(client, analyst):
    assert client.post("/ai-providers", json={"name": "x", "adapter": "nope", "model": "m"}, headers=_auth(analyst)).status_code == 422
    assert client.post("/ai-providers", json={"name": "x", "adapter": "openai_compatible", "model": "m"}, headers=_auth(analyst)).status_code == 422


def test_duplicate_name_still_conflicts(client, analyst):
    """The flush guard was narrowed to IntegrityError; a real per-org duplicate
    name must still map to 409 (not slip through, not become a 500)."""
    assert _create(client, analyst, name="Unique Name").status_code == 201
    dup = _create(client, analyst, name="Unique Name")
    assert dup.status_code == 409, dup.text


def test_test_endpoint_uses_healthcheck(client, analyst, monkeypatch):
    p = _create(client, analyst, name="Probe").json()

    class _Fake:
        def healthcheck(self):
            return (True, None)

    monkeypatch.setattr("dbbuddy_core.ai_providers.get_provider", lambda cfg: _Fake())
    r = client.post(f"/ai-providers/{p['id']}/test", headers=_auth(analyst))
    assert r.status_code == 200 and r.json()["ok"] is True

    class _Bad:
        def healthcheck(self):
            return (False, "bad key")

    monkeypatch.setattr("dbbuddy_core.ai_providers.get_provider", lambda cfg: _Bad())
    r2 = client.post(f"/ai-providers/{p['id']}/test", headers=_auth(analyst))
    assert r2.status_code == 200 and r2.json()["ok"] is False and r2.json()["error"] == "bad key"


def test_requires_settings_ai_permission(client):
    _register(client, "aipviewer@x.io")
    _set_roles("aipviewer@x.io", ["user"])  # no settings:ai
    viewer = _login(client, "aipviewer@x.io")
    assert client.get("/ai-providers", headers=_auth(viewer)).status_code == 403
    assert _create(client, viewer, name="Nope").status_code == 403


def test_ai_metrics_endpoint(client, analyst):
    r = client.get("/ai-metrics", headers=_auth(analyst))
    assert r.status_code == 200 and "providers" in r.json()
    # gated by settings:ai
    _register(client, "aipnometrics@x.io")
    _set_roles("aipnometrics@x.io", ["user"])
    viewer = _login(client, "aipnometrics@x.io")
    assert client.get("/ai-metrics", headers=_auth(viewer)).status_code == 403


def test_org_isolation(client, analyst):
    mine = _create(client, analyst, name="Org Iso Mine").json()

    other_org = _make_org("AIP Other Org")
    _register(client, "aipother@x.io")
    _set_roles("aipother@x.io", ["analyst"], org_id=other_org)
    other = _login(client, "aipother@x.io")

    # The other org cannot see or fetch my provider.
    names = [x["name"] for x in client.get("/ai-providers", headers=_auth(other)).json()]
    assert "Org Iso Mine" not in names
    assert client.get(f"/ai-providers/{mine['id']}", headers=_auth(other)).status_code == 404
