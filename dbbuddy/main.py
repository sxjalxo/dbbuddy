"""DB Buddy command-line interface — the Analyst's power tool.

The CLI is a first-class client of the DB Buddy backend: you authenticate once,
and every action (query, analyze, save/manage connections) runs through the same
REST API the web app uses. So work done in the CLI shows up in the web app and
vice-versa — one platform, two front-ends. The CLI is intended for the Analyst
role; accounts without the ``query:run`` permission are turned away.

Auth & account:
    dbbuddy login                 Interactive email/password (+ MFA) login
    dbbuddy login --api-key KEY    Authenticate with a personal API key (or omit
                                   KEY to be prompted); ideal for scripts/CI
    dbbuddy logout                Clear cached credentials
    dbbuddy whoami                Show the signed-in account, roles & permissions
    dbbuddy keys create --name N  Mint a personal API key (shown once)
    dbbuddy keys list             List your API keys
    dbbuddy keys revoke <id>      Revoke an API key

Connections (shared with the web app):
    dbbuddy connections list
    dbbuddy connections add --name N --engine mysql --host H --user U --database D
    dbbuddy connections remove <id>

Charts / Infographics (customization is shared with the web app + published reports):
    dbbuddy charts list
    dbbuddy charts show <id>          Inspect the current type + colors
    dbbuddy charts customize <id> --type pie --palette sunset \
        --category-color "North=#ff0000" --series-color "revenue=#22c55e"
    dbbuddy charts publish <id>       Publish to your organization
    dbbuddy charts unpublish <id>

Working with data:
    dbbuddy query "Top 10 customers by revenue" --connection prod
    dbbuddy chat --connection prod
    dbbuddy analyze --connection prod
    dbbuddy query "Monthly sales" --connection prod --json > report.json
    dbbuddy engines                List supported database engines

Backend address: --api-url, else $DBBUDDY_API_URL, else the stored value, else
http://localhost:8000. ``--local`` runs the deterministic pipeline directly
against an ERP database with NO platform sync (offline/developer use only).
"""

import argparse
import getpass
import json
import logging
import os
import sys

from dbbuddy_core.ai import VALID_PROVIDERS
from dbbuddy_core.dialects import SUPPORTED_ENGINES, DatabaseEngine
from dbbuddy_core.models import DBConfig

from dbbuddy.session import AuthRequired, Session, SessionError

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    handlers=[logging.FileHandler("logs/dbbuddy.log"), logging.StreamHandler(sys.stderr)],
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


AI_FALLBACK_NOTE = (
    "AI provider unavailable.\n"
    "Using the deterministic query planner instead."
)
_ai_fallback_note_printed = False

# The single permission the CLI is built around (Analyst core). Everything the
# CLI does starts from being able to run queries.
CLI_PERMISSION = "query:run"
CLI_ACCESS_DENIED = (
    "DB Buddy CLI is only available to accounts with the 'query:run' permission "
    "(the Analyst role).\n"
    "Admin and client accounts manage the platform / view reports from the web "
    "console instead. Contact your administrator if you need CLI access."
)

LOCAL_MODE_WARNING = (
    "[!] Running in local mode.\n\n"
    "    Changes made in this session will NOT appear in the web application\n"
    "    (no shared history, saved charts, or audit). Offline/developer use only.\n"
)


# ── Output helpers ────────────────────────────────────────────────────────────
def write_output(data, output_path: str = "output.json") -> str:
    """Atomically write JSON to a file."""
    output_path = os.path.abspath(output_path)
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, output_path)
    except Exception as e:
        logger.error(f"Output write failed: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return output_path


def _print_table(rows: list, limit: int = 50) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows[:limit])) for c in cols}
    header = " | ".join(str(c).ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows[:limit]:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    if len(rows) > limit:
        print(f"... ({len(rows)} rows total, showing {limit})")


def _render_query(resp: dict, json_out: bool) -> None:
    if json_out:
        print(json.dumps(resp, indent=2, default=str))
        return

    sql = resp.get("sql")
    if sql:
        print(f"\nSQL ({resp.get('query_type', 'query')}, confidence: {resp.get('confidence', 'n/a')}):")
        print(f"  {sql}")

    if resp.get("error"):
        print(f"\n[-] {resp['error']}")
    if resp.get("warning"):
        print(f"\n[!] {resp['warning']}")
    if resp.get("requires_clarification"):
        print("\n[?] This query needs clarification before it can run safely.")

    if "results" in resp:
        print()
        _print_table(resp.get("results") or [])
    elif not resp.get("auto_executed", True) and sql and not resp.get("error"):
        print("\n(query not executed — write operations require review)")


# ── AI-fallback provenance note ────────────────────────────────────────────────

def _semantic_layer_has_ai(semantic_layer: dict | None) -> bool:
    """Return True when any semantic mapping was actually produced by AI."""
    if not isinstance(semantic_layer, dict):
        return False
    for table in semantic_layer.values():
        if not isinstance(table, dict):
            continue
        for column_info in table.values():
            if isinstance(column_info, dict) and column_info.get("source") == "ai":
                return True
    return False


def _ai_fell_back(ai_requested: bool, result: dict | None) -> bool:
    """Detect the honest-provenance case where AI was requested but unused."""
    if not ai_requested or not isinstance(result, dict):
        return False

    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        return bool(metadata.get("ai_requested")) and not bool(metadata.get("ai_used"))

    # Query responses carry an explicit flag computed over the *whole* semantic
    # layer. Prefer it: the `semantic_layer` they return is only the slice of
    # columns the query touched, so an all-rule-based slice is not evidence that
    # the database as a whole went unrefined.
    ai_labeled = result.get("ai_labeled")
    if isinstance(ai_labeled, bool):
        return not ai_labeled

    semantic_layer = result.get("semantic_layer")
    # An empty layer is no evidence of a fallback (the query may simply not have
    # touched any mapped tables); only a populated, all-rule-based layer is.
    if isinstance(semantic_layer, dict) and semantic_layer:
        return not _semantic_layer_has_ai(semantic_layer)

    return False


def maybe_print_ai_fallback_note(ai_requested: bool, result: dict | None) -> None:
    """Print the non-error AI fallback note once per CLI process."""
    global _ai_fallback_note_printed
    if _ai_fallback_note_printed or not _ai_fell_back(ai_requested, result):
        return
    print(AI_FALLBACK_NOTE, file=sys.stderr)
    _ai_fallback_note_printed = True


# ── Config resolution (local mode + inline API creds) ──────────────────────────

def _load_config_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            logger.info(f"Configuration loaded from {path}")
            return cfg
    except FileNotFoundError:
        sys.exit(f"[-] Config file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"[-] Invalid JSON in config file: {e}")


def _resolve_ai_flags(args, file_cfg: dict, default_provider: str) -> tuple[bool, str]:
    """AI is on by default; an explicit --ai/--no-ai wins, then the config file."""
    if args.ai is not None:
        ai = args.ai
    elif "ai" in file_cfg:
        ai = bool(file_cfg["ai"])
    else:
        ai = True
    provider = args.ai_provider or file_cfg.get("ai_provider", default_provider)
    if provider not in VALID_PROVIDERS:
        sys.exit(f"[-] Unsupported AI provider {provider!r}. Supported: {', '.join(VALID_PROVIDERS)}")
    return ai, provider


def _prompt(prompt: str, secret: bool = False) -> str:
    """Interactive prompt that fails fast when stdin is not a TTY.

    Without this guard, `getpass` blocks indefinitely on the Windows console
    (it reads the console directly, ignoring redirected stdin) and raises
    EOFError on POSIX — either way a headless/CI invocation that forgot a flag
    would hang or crash. Tell the user to pass the value explicitly instead.
    """
    if not sys.stdin.isatty():
        sys.exit(
            "[-] Interactive input required but stdin is not a TTY. Provide the "
            "value via a flag (e.g. --password / --api-key / --host) for "
            "non-interactive (script/CI) use."
        )
    return getpass.getpass(prompt) if secret else input(prompt)


def build_config(args) -> DBConfig:
    """Merge --config file, CLI flags, and prompts into a DBConfig (local mode)."""
    file_cfg = _load_config_file(args.config) if args.config else {}

    host = args.host or file_cfg.get("host") or (_prompt("Host [localhost]: ").strip() or "localhost")

    user = args.user or file_cfg.get("user") or ""
    while not user:
        user = _prompt("Username: ").strip()

    password = args.password or file_cfg.get("password")
    if password is None:
        password = _prompt("Password: ", secret=True)

    database = args.database or file_cfg.get("database") or ""
    while not database:
        database = _prompt("Database: ").strip()

    engine_raw = args.engine or file_cfg.get("engine") or "mysql"
    try:
        engine = DatabaseEngine.normalize(engine_raw).value
    except ValueError:
        sys.exit(f"[-] Unsupported engine {engine_raw!r}. Supported: {', '.join(SUPPORTED_ENGINES)}")

    port = args.port or file_cfg.get("port")
    ai, provider = _resolve_ai_flags(args, file_cfg, default_provider="local")

    return DBConfig(
        host=host, user=user, password=password, database=database,
        port=int(port) if port else None,
        engine=engine,
        ai=ai,
        ai_provider=provider,
        mapping_plugin=file_cfg.get("mapping_plugin", "default_mapping"),
    )


# ── Session / auth helpers ──────────────────────────────────────────────────────

def _session(args) -> Session:
    return Session(getattr(args, "api_url", None))


def _require_cli_access(session: Session) -> dict:
    """Ensure the signed-in account may use the CLI (holds ``query:run``).

    Returns the /auth/me payload so callers can reuse roles/permissions.
    """
    try:
        me = session.me()
    except AuthRequired as exc:
        sys.exit(f"[-] {exc}")
    except SessionError as exc:
        sys.exit(f"[-] {exc}")
    if CLI_PERMISSION not in (me.get("permissions") or []):
        roles = ", ".join(me.get("roles") or []) or "none"
        print(CLI_ACCESS_DENIED, file=sys.stderr)
        print(f"(signed in as {me.get('email')} — roles: {roles})", file=sys.stderr)
        sys.exit(1)
    return me


def _resolve_connection_payload(session: Session, args) -> dict:
    """Build the connection portion of a /query or /analyze request.

    Prefers a saved ``--connection <name|id>`` (results sync to the platform).
    Falls back to inline credentials (test-before-save) when provided, warning
    that they are not persisted.
    """
    ref = getattr(args, "connection", None)
    if ref:
        conns = session.list_connections()
        match = next((c for c in conns if c["id"] == ref or c["name"] == ref), None)
        if match is None:
            names = ", ".join(c["name"] for c in conns) or "(none saved)"
            sys.exit(f"[-] No saved connection named or id'd {ref!r}. Your connections: {names}")
        return {"connection_id": match["id"]}

    # Inline credentials — allowed, but not persisted.
    if args.host or args.database or args.user:
        password = args.password
        if password is None:
            password = _prompt("Password: ", secret=True)
        try:
            engine = DatabaseEngine.normalize(args.engine or "mysql").value
        except ValueError:
            sys.exit(f"[-] Unsupported engine {args.engine!r}. Supported: {', '.join(SUPPORTED_ENGINES)}")
        print("[!] Using inline credentials (test-before-save) — this connection "
              "is not saved. Use 'dbbuddy connections add' to persist it.", file=sys.stderr)
        return {
            "host": args.host or "localhost", "user": args.user or "",
            "password": password or "", "database": args.database or "",
            "engine": engine, "port": args.port,
        }

    sys.exit("[-] No connection selected. Use --connection <name|id> "
             "(see 'dbbuddy connections list') or pass inline --host/--user/--database.")


# ── Commands: auth & account ────────────────────────────────────────────────────

def cmd_login(args) -> None:
    session = _session(args)
    if args.api_key is not None:
        key = args.api_key or _prompt("API key: ", secret=True)
        try:
            session.login_api_key(key)
        except SessionError as exc:
            sys.exit(f"[-] {exc}")
    else:
        email = args.email or _prompt("Email: ").strip()
        password = _prompt("Password: ", secret=True)
        try:
            result = session.login_password(email, password)
            if result.get("mfa_required"):
                code = _prompt("MFA code: ").strip()
                session.complete_mfa(result["challenge_token"], code)
        except SessionError as exc:
            sys.exit(f"[-] {exc}")

    me = session.me()
    roles = ", ".join(me.get("roles") or []) or "none"
    print(f"[+] Signed in as {me.get('email')} (roles: {roles}) at {session.api_url}", file=sys.stderr)
    if CLI_PERMISSION not in (me.get("permissions") or []):
        print("[!] Note: this account lacks 'query:run', so query/analyze commands "
              "will be refused.", file=sys.stderr)


def cmd_logout(args) -> None:
    _session(args).logout()
    print("[+] Signed out and cleared local credentials.", file=sys.stderr)


def cmd_whoami(args) -> None:
    session = _session(args)
    try:
        me = session.me()
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")
    if args.json:
        print(json.dumps(me, indent=2, default=str))
        return
    print(f"Email:       {me.get('email')}")
    print(f"Roles:       {', '.join(me.get('roles') or []) or 'none'}")
    print(f"Permissions: {', '.join(me.get('permissions') or []) or 'none'}")
    print(f"Backend:     {session.api_url}")
    print(f"CLI access:  {'yes' if CLI_PERMISSION in (me.get('permissions') or []) else 'no (needs query:run)'}")


def cmd_keys(args) -> None:
    session = _session(args)
    try:
        if args.keys_action == "create":
            key = session.create_key(args.name)
            if args.json:
                print(json.dumps(key, indent=2, default=str))
            else:
                print("[+] API key created. Copy it now — it is shown only once:\n")
                print(f"    {key['api_key']}\n")
                print(f"    id: {key['id']}  name: {key['name']}", file=sys.stderr)
                print("Use it with:  dbbuddy login --api-key   (or set DBBUDDY_API_KEY)", file=sys.stderr)
        elif args.keys_action == "list":
            keys = session.list_keys()
            if args.json:
                print(json.dumps(keys, indent=2, default=str))
            elif not keys:
                print("(no API keys)")
            else:
                rows = [{
                    "id": k["id"], "name": k["name"], "prefix": f"dbk_{k['token_prefix']}…",
                    "last_used": k.get("last_used_at") or "never",
                    "status": "revoked" if k.get("revoked_at") else "active",
                } for k in keys]
                _print_table(rows)
        elif args.keys_action == "revoke":
            session.revoke_key(args.key_id)
            print(f"[+] Revoked API key {args.key_id}.", file=sys.stderr)
        else:
            sys.exit("[-] Usage: dbbuddy keys {create|list|revoke}")
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")


def cmd_connections(args) -> None:
    session = _session(args)
    try:
        if args.connections_action == "list":
            conns = session.list_connections()
            if args.json:
                print(json.dumps(conns, indent=2, default=str))
            elif not conns:
                print("(no saved connections) — add one with 'dbbuddy connections add'")
            else:
                _print_table([{
                    "id": c["id"], "name": c["name"], "engine": c["engine"],
                    "host": c["host"], "port": c.get("port") or "", "database": c["database"],
                } for c in conns])
        elif args.connections_action == "add":
            password = args.password
            if password is None:
                password = _prompt("Password: ", secret=True)
            payload = {
                "name": args.name, "engine": args.engine or "mysql",
                "host": args.host or "localhost", "port": args.port,
                "username": args.user or "", "password": password or "",
                "database": args.database or "",
            }
            conn = session.create_connection(payload)
            print(f"[+] Saved connection {conn['name']} (id {conn['id']}).", file=sys.stderr)
        elif args.connections_action == "remove":
            session.delete_connection(args.connection_id)
            print(f"[+] Removed connection {args.connection_id}.", file=sys.stderr)
        else:
            sys.exit("[-] Usage: dbbuddy connections {list|add|remove}")
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")


# ── Commands: charts (Infographics customization) ──────────────────────────────

# Kept in sync with the backend's VALID_CHART_TYPES / ChartRenderer palettes.
# (The CLI never renders a chart — customization is pure metadata the web app
# and published client reports render.)
CHART_TYPES = ("bar", "column", "line", "area", "pie", "doughnut", "scatter", "combo", "table")
CHART_PALETTES = ("default", "ocean", "sunset", "forest", "grape", "slate")


def _parse_color(pair: str) -> tuple[str, str]:
    """Parse a ``KEY=#hex`` override; KEY is a series (column) or category (row) name."""
    if "=" not in pair:
        sys.exit(f"[-] Invalid color {pair!r}. Use KEY=#RRGGBB (e.g. Revenue=#22c55e).")
    key, _, value = pair.partition("=")
    key, value = key.strip(), value.strip()
    if not key or not value:
        sys.exit(f"[-] Invalid color {pair!r}. Use KEY=#RRGGBB (e.g. Revenue=#22c55e).")
    return key, value


def _find_chart(session, chart_id: str) -> dict:
    """Resolve a saved chart by id (the API exposes the list, not a single GET)."""
    chart = next((c for c in session.list_charts() if c["id"] == chart_id), None)
    if chart is None:
        sys.exit(f"[-] No saved chart with id {chart_id!r} (see 'dbbuddy charts list').")
    return chart


def cmd_charts(args) -> None:
    session = _session(args)
    try:
        if args.charts_action == "list":
            charts = session.list_charts()
            if args.json:
                print(json.dumps(charts, indent=2, default=str))
            elif not charts:
                print("(no saved charts) — save one from the web app or the Chart tab.")
            else:
                _print_table([{
                    "id": c["id"], "title": c["title"], "type": c.get("chart_type", "bar"),
                    "status": c.get("status", "draft"),
                } for c in charts])

        elif args.charts_action == "show":
            chart = _find_chart(session, args.chart_id)
            if args.json:
                print(json.dumps(chart, indent=2, default=str))
                return
            config = chart.get("config") or {}
            print(f"Chart:    {chart['title']}")
            print(f"Id:       {chart['id']}")
            print(f"Type:     {str(chart.get('chart_type', 'bar')).title()}")
            print(f"Status:   {chart.get('status', 'draft')}")
            print(f"Palette:  {str(config.get('palette', 'default')).title()}")
            for label, bucket in (("Series colors", "seriesColors"),
                                  ("Category colors", "categoryColors")):
                print(f"\n{label}\n{'-' * len(label)}")
                colors = config.get(bucket) or {}
                if not colors:
                    print("(none — palette defaults apply)")
                else:
                    for key, hex_value in colors.items():
                        print(f"{key}: {hex_value}")

        elif args.charts_action == "customize":
            # Merge onto the chart's existing config so partial edits are non-destructive.
            chart = _find_chart(session, args.chart_id)
            config = dict(chart.get("config") or {})
            if args.reset_colors:
                config.pop("seriesColors", None)
                config.pop("categoryColors", None)
            if args.palette:
                config["palette"] = args.palette
            for pair in args.series_color or []:
                k, v = _parse_color(pair)
                config.setdefault("seriesColors", {})[k] = v
            for pair in args.category_color or []:
                k, v = _parse_color(pair)
                config.setdefault("categoryColors", {})[k] = v

            payload: dict = {"config": config}
            if args.type:
                payload["chart_type"] = args.type

            if not args.type and not args.palette and not args.reset_colors \
                    and not args.series_color and not args.category_color:
                sys.exit(
                    "[-] Nothing to change. Pass --type, --palette, --series-color, "
                    "--category-color, or --reset-colors."
                )

            updated = session.update_chart(args.chart_id, payload)
            if args.json:
                print(json.dumps(updated, indent=2, default=str))
            else:
                print(
                    f"[+] Updated chart {updated['title']!r}: type={updated.get('chart_type')}, "
                    f"config={json.dumps(updated.get('config') or {})}",
                    file=sys.stderr,
                )
                # The CLI can't render a live preview; point power users at the GUI.
                print("    Tip: the web app shows a live visual preview while you tweak colors.",
                      file=sys.stderr)

        elif args.charts_action == "publish":
            pub = session.publish_chart(args.chart_id, visibility=args.visibility or "organization")
            print(f"[+] Published chart {args.chart_id} ({pub.get('visibility', 'organization')}).",
                  file=sys.stderr)

        elif args.charts_action == "unpublish":
            session.unpublish_chart(args.chart_id)
            print(f"[+] Unpublished chart {args.chart_id}.", file=sys.stderr)

        else:
            sys.exit("[-] Usage: dbbuddy charts {list|show|customize|publish|unpublish}")
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")


# ── Commands: data (query / chat / analyze) ────────────────────────────────────

def cmd_engines(_args) -> None:
    print("Supported database engines:")
    for e in SUPPORTED_ENGINES:
        print(f"  - {e}")


def _local_query(args) -> None:
    from dbbuddy_core.pipeline import process_query

    print(LOCAL_MODE_WARNING, file=sys.stderr)
    config = build_config(args)
    print(f"[*] Connecting to {config.engine}://{config.host}/{config.database} …", file=sys.stderr)
    resp = process_query(config, args.question, auto_execute_reads=True)
    maybe_print_ai_fallback_note(config.ai, resp)
    _render_query(resp, args.json)
    sys.exit(0 if not resp.get("error") else 1)


def cmd_query(args) -> None:
    if args.local:
        _local_query(args)
        return

    session = _session(args)
    _require_cli_access(session)
    ai, provider = _resolve_ai_flags(args, {}, default_provider="hybrid")
    payload = {"question": args.question, "ai": ai, "ai_provider": provider, "auto_execute_reads": True}
    payload.update(_resolve_connection_payload(session, args))
    try:
        resp = session.query(payload)
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")
    maybe_print_ai_fallback_note(ai, resp)
    _render_query(resp, args.json)
    sys.exit(0 if not resp.get("error") else 1)


def cmd_chat(args) -> None:
    if args.local:
        from dbbuddy_core.pipeline import process_query

        print(LOCAL_MODE_WARNING, file=sys.stderr)
        config = build_config(args)
        print(f"[*] Connected to {config.engine}://{config.host}/{config.database}.")
        _chat_loop(lambda q: process_query(config, q, auto_execute_reads=True), config.ai, args.json)
        return

    session = _session(args)
    _require_cli_access(session)
    ai, provider = _resolve_ai_flags(args, {}, default_provider="hybrid")
    conn_payload = _resolve_connection_payload(session, args)
    print(f"[*] Connected to {session.api_url} (session synced to the platform).")

    def _run(question: str) -> dict:
        payload = {"question": question, "ai": ai, "ai_provider": provider, "auto_execute_reads": True}
        payload.update(conn_payload)
        return session.query(payload)

    _chat_loop(_run, ai, args.json)


def _chat_loop(run, ai_requested: bool, json_out: bool) -> None:
    print("Ask a question, or type 'exit' / Ctrl-D to quit.\n")
    while True:
        try:
            question = input("dbbuddy> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", "\\q"}:
            break
        try:
            resp = run(question)
            maybe_print_ai_fallback_note(ai_requested, resp)
            _render_query(resp, json_out)
        except Exception as exc:  # keep the REPL alive on a bad query
            print(f"[-] {exc}")
        print()


def _local_provider_chain():
    """The provider chain for `insights --local`: local Ollama, mirroring the
    engine's own local-AI default. No app DB is reachable in local mode, so the
    per-org chain the web app resolves cannot be used — this is the CLI-only,
    single-provider equivalent. Model/URL come from the same env the engine reads
    (`LOCAL_MODEL`, `OLLAMA_URL`), so a machine already set up for `--local`
    queries needs no extra configuration."""
    import os

    from dbbuddy_core.ai import OLLAMA_URL
    from dbbuddy_core.ai_providers import ProviderRuntimeConfig

    return [ProviderRuntimeConfig(
        adapter="ollama",
        model=os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b"),
        base_url=OLLAMA_URL,
        name="Local Ollama",
    )]


def _render_insights(bundle_markdown: str, extra: dict, json_out: bool) -> None:
    if json_out:
        print(json.dumps(extra, indent=2, default=str))
        return
    print("\n" + bundle_markdown.rstrip())
    suggestions = extra.get("suggested_questions") or []
    if suggestions:
        print("\nSuggested follow-ups:")
        for question in suggestions:
            print(f"  - {question}")


def cmd_insights(args) -> None:
    """Explain the result of a natural-language query (the web app's AI Insights,
    on the command line)."""
    if args.local:
        from dbbuddy_core.insights import context as insights_context
        from dbbuddy_core.insights.formatter import to_markdown
        from dbbuddy_core.insights.prompts import suggested_questions
        from dbbuddy_core.insights.service import InsightsDisabled, answer_followup, generate_insights
        from dbbuddy_core.pipeline import process_query

        print(LOCAL_MODE_WARNING, file=sys.stderr)
        config = build_config(args)
        resp = process_query(config, args.question, auto_execute_reads=True)
        if resp.get("error"):
            sys.exit(f"[-] {resp['error']}")
        rows = resp.get("results") or []
        if not resp.get("auto_executed", False):
            sys.exit("[-] The query was not executed (writes are held for review); "
                     "there is nothing to analyze.")

        ctx = insights_context.build_context(
            question=args.question, sql=resp.get("sql") or "",
            database=config.database, rows=rows)
        chain = _local_provider_chain()
        try:
            if args.ask:
                result = answer_followup(ctx, args.ask, chain)
                _render_insights(f"## Answer\n\n{result.get('answer', '')}", result, args.json)
            else:
                bundle = generate_insights(ctx, chain)
                extra = {"insights": bundle.to_dict(), "markdown": to_markdown(bundle),
                         "suggested_questions": suggested_questions(ctx),
                         "row_count": ctx.row_count}
                _render_insights(to_markdown(bundle), extra, args.json)
        except InsightsDisabled as exc:
            sys.exit(f"[-] {exc}")
        sys.exit(0)

    session = _session(args)
    _require_cli_access(session)
    ai, provider = _resolve_ai_flags(args, {}, default_provider="hybrid")

    # Run the query first — insights analyze a result, so we need one. The rows
    # come straight back from /query and are posted to /insights, exactly as the
    # web app's panel does after a query completes.
    query_payload = {"question": args.question, "ai": ai, "ai_provider": provider,
                     "auto_execute_reads": True}
    conn_payload = _resolve_connection_payload(session, args)
    query_payload.update(conn_payload)
    try:
        query_resp = session.query(query_payload)
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")
    if query_resp.get("error"):
        sys.exit(f"[-] {query_resp['error']}")
    rows = query_resp.get("results") or []

    insight_payload = {
        "question": args.question, "sql": query_resp.get("sql") or "",
        "database": query_resp.get("database") or "", "rows": rows,
    }
    # Carry the saved-connection reference so the server can label and audit it.
    if "connection_id" in conn_payload:
        insight_payload["connection_id"] = conn_payload["connection_id"]

    try:
        if args.ask:
            insight_payload["followup"] = args.ask
            result = session.insights_ask(insight_payload)
            _render_insights(f"## Answer\n\n{result.get('answer', '')}", result, args.json)
        else:
            result = session.insights(insight_payload)
            _render_insights(result.get("markdown") or "", result, args.json)
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")
    sys.exit(0)


def cmd_analyze(args) -> None:
    if args.local:
        from dbbuddy_core.pipeline import process_schema

        print(LOCAL_MODE_WARNING, file=sys.stderr)
        config = build_config(args)
        print(f"[*] Analyzing schema for {config.engine}://{config.host}/{config.database} …", file=sys.stderr)
        try:
            result = process_schema(config)
        except Exception as exc:
            sys.exit(f"[-] Failed to process schema: {exc}")
        if result is None:
            sys.exit("[-] Failed to process schema.")
        _emit_analyze(result, config.ai, args)
        return

    session = _session(args)
    _require_cli_access(session)
    ai, provider = _resolve_ai_flags(args, {}, default_provider="hybrid")
    payload = {"ai": ai, "ai_provider": provider}
    payload.update(_resolve_connection_payload(session, args))
    print(f"[*] Analyzing schema via {session.api_url} …", file=sys.stderr)
    try:
        result = session.analyze(payload)
    except (AuthRequired, SessionError) as exc:
        sys.exit(f"[-] {exc}")
    _emit_analyze(result, ai, args)


def _emit_analyze(result: dict, ai_requested: bool, args) -> None:
    maybe_print_ai_fallback_note(ai_requested, result)
    if args.output:
        path = write_output(result, args.output)
        print(f"[+] Semantic layer written to {path}", file=sys.stderr)
    if args.json or not args.output:
        print(json.dumps(result, indent=2, default=str))


# ── Argument parser ───────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    from dbbuddy import __version__

    # Backend/auth options shared by every command that talks to the platform.
    api = argparse.ArgumentParser(add_help=False)
    api.add_argument("--api-url", help="Backend URL (default: $DBBUDDY_API_URL, stored, or http://localhost:8000).")
    api.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # Connection/credential options for query/chat/analyze.
    conn = argparse.ArgumentParser(add_help=False, parents=[api])
    conn.add_argument("--connection", help="Saved connection name or id (see 'connections list').")
    conn.add_argument("--local", action="store_true",
                      help="Run the pipeline directly against an ERP DB with NO platform sync (offline).")
    conn.add_argument("--host", help="ERP host (inline creds / --local).")
    conn.add_argument("--port", type=int, help="ERP port.")
    conn.add_argument("--user", help="ERP user.")
    conn.add_argument("--password", help="ERP password (prompts if omitted).")
    conn.add_argument("--database", help="ERP database name.")
    conn.add_argument("--engine", help=f"ERP engine: {', '.join(SUPPORTED_ENGINES)} (default: mysql).")
    conn.add_argument("--config", help="Path to a JSON connection config (--local).")
    conn.add_argument("--ai", action=argparse.BooleanOptionalAction, default=None,
                      help="AI-enhanced mode (default: on). Use --no-ai for rule-based only.")
    conn.add_argument("--ai-provider", help=f"AI provider: {', '.join(VALID_PROVIDERS)}.")

    parser = argparse.ArgumentParser(prog="dbbuddy", description="DB Buddy — the Analyst's NL-to-SQL CLI.")
    parser.add_argument("--version", action="version", version=f"DB Buddy {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # auth & account
    p_login = sub.add_parser("login", parents=[api], help="Sign in (email/password or --api-key).")
    p_login.add_argument("--email", help="Account email (prompts if omitted).")
    p_login.add_argument("--api-key", nargs="?", const="", default=None,
                         help="Authenticate with a personal API key (prompts if value omitted).")
    p_login.set_defaults(func=cmd_login)

    sub.add_parser("logout", parents=[api], help="Clear cached credentials.").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", parents=[api], help="Show the signed-in account.").set_defaults(func=cmd_whoami)

    p_keys = sub.add_parser("keys", parents=[api], help="Manage personal API keys.")
    keys_sub = p_keys.add_subparsers(dest="keys_action", metavar="{create,list,revoke}")
    k_create = keys_sub.add_parser("create", parents=[api], help="Mint a new API key (shown once).")
    k_create.add_argument("--name", required=True, help="A label for the key (e.g. 'laptop', 'ci').")
    keys_sub.add_parser("list", parents=[api], help="List your API keys.")
    k_revoke = keys_sub.add_parser("revoke", parents=[api], help="Revoke an API key.")
    k_revoke.add_argument("key_id", help="The key id (from 'keys list').")
    p_keys.set_defaults(func=cmd_keys)

    # connections
    p_conns = sub.add_parser("connections", parents=[api], help="Manage saved ERP connections.")
    conns_sub = p_conns.add_subparsers(dest="connections_action", metavar="{list,add,remove}")
    conns_sub.add_parser("list", parents=[api], help="List saved connections.")
    c_add = conns_sub.add_parser("add", parents=[api], help="Save a new connection.")
    c_add.add_argument("--name", required=True, help="A label for the connection.")
    c_add.add_argument("--engine", help=f"Engine: {', '.join(SUPPORTED_ENGINES)} (default: mysql).")
    c_add.add_argument("--host", help="ERP host (default: localhost).")
    c_add.add_argument("--port", type=int, help="ERP port.")
    c_add.add_argument("--user", help="ERP user.")
    c_add.add_argument("--password", help="ERP password (prompts if omitted).")
    c_add.add_argument("--database", help="ERP database name.")
    c_remove = conns_sub.add_parser("remove", parents=[api], help="Delete a saved connection.")
    c_remove.add_argument("connection_id", help="The connection id (from 'connections list').")
    p_conns.set_defaults(func=cmd_connections)

    # charts (Infographics customization — type + colors, shared with the web app)
    p_charts = sub.add_parser("charts", parents=[api], help="List, customize, and publish saved charts.")
    charts_sub = p_charts.add_subparsers(
        dest="charts_action", metavar="{list,show,customize,publish,unpublish}"
    )
    charts_sub.add_parser("list", parents=[api], help="List your saved charts.")

    ch_show = charts_sub.add_parser(
        "show", parents=[api], help="Show a chart's current type and colors.",
    )
    ch_show.add_argument("chart_id", help="The chart id (from 'charts list').")

    ch_cust = charts_sub.add_parser(
        "customize", parents=[api],
        help="Change a saved chart's type and colors (applies in the web app + published reports).",
    )
    ch_cust.add_argument("chart_id", help="The chart id (from 'charts list').")
    ch_cust.add_argument("--type", choices=CHART_TYPES, help="Chart type.")
    ch_cust.add_argument("--palette", choices=CHART_PALETTES, help="Base color palette.")
    ch_cust.add_argument(
        "--series-color", action="append", metavar="COLUMN=#HEX",
        help="Override a series (numeric column) color. Repeatable.",
    )
    ch_cust.add_argument(
        "--category-color", action="append", metavar="LABEL=#HEX",
        help="Override a category (bar / pie slice) color. Repeatable.",
    )
    ch_cust.add_argument("--reset-colors", action="store_true", help="Clear all color overrides.")

    ch_pub = charts_sub.add_parser("publish", parents=[api], help="Publish a chart to your organization.")
    ch_pub.add_argument("chart_id", help="The chart id (from 'charts list').")
    ch_pub.add_argument("--visibility", choices=("organization", "private"), help="Default: organization.")
    ch_unpub = charts_sub.add_parser("unpublish", parents=[api], help="Revoke a published chart.")
    ch_unpub.add_argument("chart_id", help="The chart id (from 'charts list').")
    p_charts.set_defaults(func=cmd_charts)

    # data
    p_query = sub.add_parser("query", parents=[conn], help="Run a natural-language query.")
    p_query.add_argument("question", help="The question, e.g. \"show all users\".")
    p_query.set_defaults(func=cmd_query)

    sub.add_parser("chat", parents=[conn], help="Interactive query session (REPL).").set_defaults(func=cmd_chat)

    p_insights = sub.add_parser("insights", parents=[conn],
                                help="Run a query and explain the result (AI Insights).")
    p_insights.add_argument("question", help="The question to run and analyze.")
    p_insights.add_argument("--ask", metavar="FOLLOWUP",
                            help="Ask a follow-up about the result instead of a full analysis.")
    p_insights.set_defaults(func=cmd_insights)

    p_analyze = sub.add_parser("analyze", parents=[conn], help="Analyze & index the schema.")
    p_analyze.add_argument("--output", default="output.json", help="Where to write the semantic layer.")
    p_analyze.set_defaults(func=cmd_analyze)

    sub.add_parser("engines", help="List supported database engines.").set_defaults(func=cmd_engines)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
