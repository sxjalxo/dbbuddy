"""CLI ``insights`` command — wiring only, no live model or DB.

The command exists so the AI Insights feature the web app added is reachable from
the terminal too. Two modes:

* platform: run ``/query``, post the rows to ``/insights/generate`` (or
  ``/insights/ask`` with ``--ask``), exactly as the browser panel does;
* ``--local``: run the rule-based pipeline in-process, then call the insights
  service directly against a local provider chain.

These tests drive both with fakes, so the analyzer, the model and the network are
all out of scope — what is under test is that the command runs a query first and
feeds *its* rows to the right insights call.
"""

import types

import pytest

import dbbuddy.main as cli


class _FakeSession:
    def __init__(self):
        self.query_payload = None
        self.insight_payload = None
        self.ask_payload = None
        self.api_url = "http://localhost:8000"

    def me(self):
        return {"email": "u@x.io", "roles": ["analyst"], "permissions": ["query:run"]}

    def list_connections(self):
        return [{"id": "c1", "name": "prod"}]

    def query(self, payload):
        self.query_payload = payload
        return {"sql": "SELECT country, COUNT(*) FROM airport_geo GROUP BY country",
                "database": "airportdb",
                "results": [{"country": "US", "n": 1928}, {"country": "CA", "n": 205}]}

    def insights(self, payload):
        self.insight_payload = payload
        return {"markdown": "## Summary\n\nTwo countries.",
                "suggested_questions": ["Which country has the most airports?"]}

    def insights_ask(self, payload):
        self.ask_payload = payload
        return {"answer": "The United States, with 1928.", "provider": "Local Ollama"}


def _args(**over):
    base = dict(local=False, json=False, ask=None, connection="prod",
                host=None, user=None, password=None, database=None, engine=None,
                port=None, config=None, ai=None, ai_provider=None,
                question="how many airports per country")
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(cli, "_session", lambda args: session)
    monkeypatch.setattr(cli, "_require_cli_access", lambda s: s.me())
    return session


def test_platform_insights_runs_the_query_then_analyzes_its_rows(_patch_session, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.cmd_insights(_args())
    assert exc.value.code == 0

    session = _patch_session
    # The query ran first…
    assert session.query_payload["question"] == "how many airports per country"
    # …and the rows it returned are what got analyzed.
    assert session.insight_payload["rows"] == [
        {"country": "US", "n": 1928}, {"country": "CA", "n": 205}]
    assert session.insight_payload["sql"].startswith("SELECT country")
    assert session.insight_payload["connection_id"] == "c1"
    assert "Two countries" in capsys.readouterr().out


def test_platform_insights_ask_uses_the_followup_endpoint(_patch_session, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.cmd_insights(_args(ask="which country has the most?"))
    assert exc.value.code == 0

    session = _patch_session
    assert session.insight_payload is None            # not a full analysis
    assert session.ask_payload["followup"] == "which country has the most?"
    assert "1928" in capsys.readouterr().out


def test_platform_insights_bails_when_the_query_errors(_patch_session):
    _patch_session.query = lambda payload: {"error": "I couldn't match that."}
    with pytest.raises(SystemExit) as exc:
        cli.cmd_insights(_args())
    assert exc.value.code != 0


def test_local_mode_feeds_pipeline_rows_to_the_insights_service(monkeypatch, capsys):
    rows = [{"country": "US", "n": 1928}]

    monkeypatch.setattr(cli, "build_config", lambda args: types.SimpleNamespace(
        database="airportdb", engine="mysql", host="h", ai=False))
    monkeypatch.setattr("dbbuddy_core.pipeline.process_query",
                        lambda config, q, **kw: {"sql": "SELECT ...", "results": rows,
                                                 "auto_executed": True})

    captured = {}

    def _fake_generate(ctx, chain, **kw):
        captured["rows"] = ctx.row_count
        captured["question"] = ctx.question
        captured["chain"] = chain
        return types.SimpleNamespace(
            provider="Local Ollama", prompt_version="v1",
            to_dict=lambda: {}, )

    monkeypatch.setattr("dbbuddy_core.insights.service.generate_insights", _fake_generate)
    monkeypatch.setattr("dbbuddy_core.insights.formatter.to_markdown",
                        lambda bundle: "## Summary\n\nlocal ok")
    monkeypatch.setattr("dbbuddy_core.insights.prompts.suggested_questions",
                        lambda ctx: [])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_insights(_args(local=True, connection=None, host="h", database="airportdb"))
    assert exc.value.code == 0

    assert captured["rows"] == 1
    assert captured["question"] == "how many airports per country"
    assert captured["chain"][0].adapter == "ollama"       # local chain, not app-DB
    assert "local ok" in capsys.readouterr().out


def test_local_provider_chain_is_a_single_ollama_provider():
    chain = cli._local_provider_chain()
    assert len(chain) == 1
    assert chain[0].adapter == "ollama"
    assert chain[0].name == "Local Ollama"
