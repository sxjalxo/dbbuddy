import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app_db.config import settings
from dbbuddy_core import secrets_store
from dbbuddy_core.db import DatabaseUnavailableError
from dbbuddy_core.dialects import DatabaseEngine, SUPPORTED_ENGINES
from dbbuddy_core.erp_concurrency import ERPBusy
from dbbuddy_core.models import DBConfig
from dbbuddy_core.pipeline import process_query, process_schema

logger = logging.getLogger(__name__)

# The supported AI providers: local (Qwen Coder), nemotron (NVIDIA cloud API),
# openai (OpenAI cloud API), and hybrid (Qwen for labeling + Nemotron fallback).
AIProvider = Literal["local", "nemotron", "openai", "hybrid"]

# Providers whose API key can be managed via the API key endpoints.
ManagedKeyProvider = Literal["nemotron", "openai"]

# Activate any persisted API keys for this process on startup.
secrets_store.apply_to_env()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: initialize the application DB and start the in-process background-
    # job scheduler (disabled in tests via DBBUDDY_DISABLE_SCHEDULER).
    from app_db.database import init_db

    init_db()

    # Subscribe to cross-worker session revocations. Without this a worker still
    # publishes its own logouts but never hears anyone else's, so it would keep
    # serving a revoked session until its cache TTL expired — the gap this exists
    # to close. A no-op when there is no Redis, which is the documented degraded
    # behaviour rather than a failure.
    from app_db.deps import start_revocation_listener

    start_revocation_listener()

    scheduler = None
    if os.getenv("DBBUDDY_DISABLE_SCHEDULER") != "1":
        from app_db.jobs import scheduler as _scheduler
        scheduler = _scheduler
        scheduler.start()
    try:
        yield
    finally:
        # Shutdown: stop the scheduler if it was started.
        if scheduler is not None:
            scheduler.shutdown()


app = FastAPI(title="dbbuddy API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request, call_next):
    """Assign a correlation id per request (honoring an inbound X-Request-ID),
    expose it for audit rows, and echo it back on the response."""
    from app_db.request_context import new_request_id, set_request_id

    rid = request.headers.get("X-Request-ID") or new_request_id()
    set_request_id(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# Reject oversized request bodies before they are buffered into memory. The API
# only ever accepts small JSON payloads (auth, query text, connection configs),
# so a generous cap both stops accidental huge uploads and closes a memory-DoS
# vector (a client can otherwise stream hundreds of MB that FastAPI buffers to
# validate). Enforced by the declared Content-Length so nothing is read first.
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


@app.middleware("http")
async def limit_request_body_size(request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large."},
                )
        except ValueError:
            pass  # unparseable header — let normal parsing reject it
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """Attach baseline security response headers.

    Every response from this API is JSON carrying customer ERP data, so:

    * ``X-Content-Type-Options: nosniff`` — stop a browser from re-interpreting a
      JSON body as HTML/JS (the classic way a reflected value becomes stored XSS).
    * ``X-Frame-Options`` / ``frame-ancestors 'none'`` — the API is never framed;
      denying it removes clickjacking against any HTML error page it may serve.
    * ``Referrer-Policy: no-referrer`` — response URLs can contain record ids;
      they must not leak to third parties via the Referer header.
    * ``Cache-Control: no-store`` — query results and tokens must never be written
      to a shared/browser cache. This is the one with real teeth here.
    * HSTS, only over TLS and only in production, so local http:// dev is unaffected.
    """
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    headers.setdefault("Cache-Control", "no-store")
    if settings.is_production and request.url.scheme == "https":
        headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


@app.exception_handler(RecursionError)
async def _too_deeply_nested_handler(request, _exc):
    """Pathologically nested JSON (e.g. thousands of nested arrays) overflows the
    parser's recursion limit. Return a clean 400 instead of a 500 so it reads as
    the client error it is."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=400,
        content={"detail": "Request payload is too deeply nested."},
    )

# ── Application database (platform state: users, roles, charts, history, …) ────
# Separate from customer ERP databases. Created/seeded on startup (see lifespan).
from app_db.deps import get_token_payload, require_token_permission  # noqa: E402
from app_db.rate_limit import (  # noqa: E402
    ANALYZE_BUDGET, QUERY_BUDGET, rate_limit,
)
from app_db.routers import admin as admin_router  # noqa: E402
from app_db.routers import ai_providers as ai_providers_router  # noqa: E402
from app_db.routers import audit as audit_router  # noqa: E402
from app_db.routers import auth as auth_router  # noqa: E402
from app_db.routers import charts as charts_router  # noqa: E402
from app_db.routers import connections as connections_router  # noqa: E402
from app_db.routers import dashboards as dashboards_router  # noqa: E402
from app_db.routers import history as history_router  # noqa: E402
from app_db.routers import insights as insights_router  # noqa: E402
from app_db.routers import keys as keys_router  # noqa: E402
from app_db.routers import jobs as jobs_router  # noqa: E402
from app_db.routers import notifications as notifications_router  # noqa: E402
from app_db.routers import orgs as orgs_router  # noqa: E402
from app_db.routers import relations as relations_router  # noqa: E402
from app_db.routers import reports as reports_router  # noqa: E402


app.include_router(auth_router.router)
app.include_router(keys_router.router)
app.include_router(connections_router.router)
app.include_router(history_router.router)
app.include_router(charts_router.router)
app.include_router(orgs_router.router)
app.include_router(admin_router.router)
app.include_router(reports_router.router)
app.include_router(audit_router.router)
app.include_router(jobs_router.router)
app.include_router(notifications_router.router)
app.include_router(ai_providers_router.router)
app.include_router(relations_router.router)
app.include_router(insights_router.router)
app.include_router(dashboards_router.router)


# Query endpoints accept EITHER a saved connection_id (credentials resolved from
# the application DB, auth required) OR inline credentials (legacy / test-before-
# save). The inline fields are optional so connection_id-only requests validate.
class AnalyzeRequest(BaseModel):
    connection_id: str | None = None
    host: str = ""
    user: str = ""
    password: str = ""
    database: str = ""
    port: int | None = None
    engine: DatabaseEngine = DatabaseEngine.MYSQL
    ai: bool = False
    ai_provider: AIProvider = "hybrid"


class QueryRequest(BaseModel):
    connection_id: str | None = None
    host: str = ""
    user: str = ""
    password: str = ""
    database: str = ""
    port: int | None = None
    engine: DatabaseEngine = DatabaseEngine.MYSQL
    question: str
    ai: bool = True
    ai_provider: AIProvider = "hybrid"
    # When False, read-only (SELECT) queries are held for confirmation instead
    # of auto-executing.
    auto_execute_reads: bool = True


class ExecuteRequest(BaseModel):
    connection_id: str | None = None
    host: str = ""
    user: str = ""
    password: str = ""
    database: str = ""
    port: int | None = None
    engine: DatabaseEngine = DatabaseEngine.MYSQL
    # Either redeem an execution_token minted by /query (runs the reviewed,
    # server-stored SQL — the client cannot alter it) OR submit raw ``sql``. Raw
    # writes additionally require the ``query:write:manual`` permission; raw reads
    # are always allowed. When a token is present, ``sql`` is ignored.
    sql: str = ""
    execution_token: str | None = None


def _err_detail(exc: Exception) -> str:
    """A non-empty error message for an *upstream* (502) failure.

    Safe to return because ``DatabaseUnavailableError`` messages are ours and are
    what the operator needs (unreachable host, bad credentials, unknown database).
    Some exceptions (e.g. Fernet's ``InvalidToken``) stringify to '', which would
    otherwise yield a useless ``{"detail": ""}`` — fall back to the class name.
    """
    return str(exc) or f"Internal error: {type(exc).__name__}"


def _internal_error(exc: Exception, *, context: str) -> HTTPException:
    """Log an unexpected exception server-side and return an opaque 500.

    Raw exception text used to be echoed to the client. On this code path the
    exception frequently comes from a database driver, whose messages routinely
    embed the DSN, host, port, and username of the *customer's* ERP — plus, on
    driver/ORM errors, fragments of the SQL. That is exactly the reconnaissance an
    authenticated-but-untrusted caller wants, and none of it helps a legitimate
    user. The correlation id ties the opaque response back to the full traceback
    in the server log.
    """
    from app_db.request_context import get_request_id

    rid = get_request_id()
    logger.exception("Unhandled error in %s (request_id=%s)", context, rid)
    return HTTPException(
        status_code=500,
        detail=f"Internal server error. Reference: {rid}" if rid else "Internal server error.",
    )


def _erp_busy(exc: Exception) -> HTTPException:
    """A saturated (but healthy) target database → 503 + Retry-After.

    Distinct from 502: nothing is broken, we are simply refusing to add load to a
    customer database that is already at its concurrency ceiling.
    """
    return HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"})


def _resolve_ai_chain(payload: dict):
    """Resolve the caller org's active AI provider chain (active + fallbacks).

    Returns None when the org has no configured active provider, in which case the
    engine falls back to the legacy ``ai_provider`` request string — so behavior is
    unchanged before any provider records exist. Never fatal to the request.
    """
    try:
        from app_db.ai_runtime import resolve_active_provider_chain
        return resolve_active_provider_chain(payload.get("org_id"))
    except Exception:  # noqa: BLE001 — provider resolution must not break a query
        # Best-effort, but not silent: a resolution failure means the request
        # quietly falls back to the legacy provider string, which is worth a trail.
        logger.warning("AI provider chain resolution failed; falling back to legacy provider",
                       exc_info=True)
        return None


def _clip(text, limit: int = 2000):
    """Trim free text before it goes into an audit row (SQL/questions can be huge).
    Also drops NUL bytes so the audit write itself never trips a Postgres text error."""
    if not isinstance(text, str):
        return None
    text = text.replace("\x00", "")
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


def _audit_db_action(payload: dict, req, *, action: str, detail: dict) -> None:
    """Emit a server-side audit row for an action against a customer ERP database.

    The SQL actually run against customer databases — including writes via
    ``/execute`` — otherwise leaves no trail, which is a forensics/compliance gap
    for an ERP product. This records (actor, org, connection, engine/database,
    plus the caller-supplied detail such as sql/safety/row_count).

    Auditing must never break the query path, so any failure here is swallowed.
    """
    from app_db.database import SessionLocal
    from app_db.deps import write_audit

    merged = {
        "engine": str(getattr(req, "engine", "")) or None,
        "database": getattr(req, "database", "") or None,
        **detail,
    }
    merged = {k: v for k, v in merged.items() if v is not None}
    try:
        with SessionLocal() as db:
            write_audit(
                db, user_id=payload.get("sub"), action=action,
                entity_type="db_query", entity_id=getattr(req, "connection_id", None),
                organization_id=payload.get("org_id"), detail=merged,
            )
            db.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort, never fatal
        # Never break the query path, but a broken audit trail is a compliance
        # signal operators must be able to see — so log it rather than dropping it.
        logger.exception("Failed to write audit row for db action %r", action)


def _issue_execution_token(payload: dict, req, result: dict, *, engine, host, port, database, user) -> None:
    """Attach a single-use ``execution_token`` to a held /query result.

    When /query produces SQL but holds it for confirmation (``auto_executed`` is
    False — a write, or a read with auto-execute off), mint a token bound to that
    exact SQL and the resolved execution target. /execute redeems it and runs the
    stored SQL, so the confirmed statement cannot be altered client-side. Token
    minting is best-effort: on failure the result is returned without a token (the
    client falls back to the permission-gated raw path, or re-queries).
    """
    from app_db import execution_tokens
    from app_db.database import SessionLocal

    sql = result.get("sql")
    if not sql:
        return
    try:
        ctx_hash = execution_tokens.context_hash(engine, host, port, database, user)
        with SessionLocal() as db:
            token = execution_tokens.issue(
                db,
                user_id=payload.get("sub"),
                sql=sql,
                safety_category=result.get("safety_category") or "write",
                context_hash=ctx_hash,
                connection_id=getattr(req, "connection_id", None),
                organization_id=payload.get("org_id"),
            )
            db.commit()
        result["execution_token"] = token
    except Exception:  # noqa: BLE001 — never fail the query response over token minting
        # The client degrades gracefully (re-query / permission-gated raw path),
        # but a persistent minting failure should be visible in the logs.
        logger.warning("Failed to mint execution token for held query", exc_info=True)


def _resolve_connection(req, payload: dict):
    """Resolve (host, user, password, database, engine, port) for a query request.

    Auth and permission are enforced by the calling endpoint's dependency
    (``require_token_permission``), so both the inline-credential and saved
    ``connection_id`` paths are reachable only by an authenticated, permitted
    user — there is no anonymous bypass.

    A saved ``connection_id`` must belong to the caller; its stored ERP password
    is decrypted only after that ownership check. Otherwise inline credentials
    are used as-is (test-before-save).
    """
    conn_id = getattr(req, "connection_id", None)
    if not conn_id:
        return req.host, req.user, req.password, req.database, str(req.engine), getattr(req, "port", None)

    from cryptography.fernet import InvalidToken

    from app_db.database import SessionLocal
    from app_db.models import DatabaseConnection
    from app_db.security import decrypt_secret

    with SessionLocal() as db:
        conn = db.get(DatabaseConnection, conn_id)
        if conn is None or conn.user_id != payload.get("sub"):
            raise HTTPException(status_code=404, detail="Connection not found.")
        # Credentials are decrypted only after auth + permission + ownership pass.
        try:
            password = decrypt_secret(conn.password_encrypted)
        except InvalidToken as exc:
            # The stored secret was encrypted with a different key than the one this
            # process holds — the at-rest key changed since the connection was saved
            # (e.g. an ephemeral dev key rotated on restart, or APP_SECRET_KEY was
            # changed). InvalidToken has an empty message, so give an actionable one
            # instead of surfacing a blank 500.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Saved credentials for this connection can no longer be decrypted "
                    "because the application encryption key changed since it was saved. "
                    "Re-enter the connection to store its password again, and set a "
                    "stable APP_SECRET_KEY so this does not recur across restarts."
                ),
            ) from exc
        return (
            conn.host, conn.username, password,
            conn.database, conn.engine, conn.port,
        )


class ApiKeyRequest(BaseModel):
    provider: ManagedKeyProvider = "nemotron"
    api_key: str


@app.get("/")
def health_check():
    return {"status": "ok"}


# A full schema walk — the most expensive thing an authenticated caller can
# ask for, and previously unmetered. Fails open: no shared window means the
# analyze still runs, because a cache outage must not become a service outage.
@app.post("/analyze", dependencies=[Depends(rate_limit("analyze", ANALYZE_BUDGET))])
def analyze(req: AnalyzeRequest, payload: dict = Depends(require_token_permission("schema:analyze"))):
    try:
        host, user, password, database, engine, port = _resolve_connection(req, payload)
        config = DBConfig(
            host=host, user=user, password=password, database=database,
            port=port, engine=engine, ai=req.ai, ai_provider=req.ai_provider,
            ai_provider_chain=_resolve_ai_chain(payload),
        )
        return process_schema(config)
    except HTTPException:
        raise
    except ERPBusy as exc:
        raise _erp_busy(exc) from exc
    except DatabaseUnavailableError as exc:
        # Target ERP DB unreachable/unusable — an upstream problem, not a server bug.
        raise HTTPException(status_code=502, detail=_err_detail(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc, context="/analyze") from exc


@app.post("/query", dependencies=[Depends(rate_limit("query", QUERY_BUDGET))])
def query(req: QueryRequest, payload: dict = Depends(require_token_permission("query:run"))):
    try:
        host, user, password, database, engine, port = _resolve_connection(req, payload)
        config = DBConfig(
            host=host, user=user, password=password, database=database,
            port=port, engine=engine, ai=req.ai, ai_provider=req.ai_provider,
            ai_provider_chain=_resolve_ai_chain(payload),
        )
        # ``user_id`` is what keys the engine's rate limiter. Without it every
        # API caller collapsed onto the single bucket "default", so the 10 req/s
        # ceiling was global rather than per-user: one client's burst throttled
        # everyone, and no individual caller could actually be limited.
        result = process_query(
            config, req.question,
            auto_execute_reads=req.auto_execute_reads,
            user_id=payload.get("sub"),
        )
        # A held plan (write, or read with auto-execute off) gets a single-use
        # execution token so /execute can run the reviewed, server-stored SQL.
        if isinstance(result, dict) and result.get("auto_executed") is False:
            _issue_execution_token(
                payload, req, result,
                engine=engine, host=host, port=port, database=database, user=user,
            )
        _audit_db_action(payload, req, action="query", detail={
            "question": _clip(req.question, 500),
            "sql": _clip(result.get("sql") if isinstance(result, dict) else None),
            "safety_category": result.get("safety_category") if isinstance(result, dict) else None,
            "requires_confirmation": result.get("requires_confirmation") if isinstance(result, dict) else None,
            "row_count": result.get("row_count") if isinstance(result, dict) else None,
        })
        return result
    except HTTPException:
        raise
    except ERPBusy as exc:
        raise _erp_busy(exc) from exc
    except DatabaseUnavailableError as exc:
        # Target ERP DB unreachable/unusable — an upstream problem, not a server bug.
        raise HTTPException(status_code=502, detail=_err_detail(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc, context="/query") from exc


def _resolve_execute_sql(req, payload, *, engine, host, port, database, user):
    """Decide *what* SQL /execute runs and whether the caller may run it.

    Returns ``(sql, safety_category, source)``. Two paths:

    * ``execution_token`` present → redeem it (single-use) and run the exact SQL
      the server stored when /query produced it. The client-submitted ``sql`` is
      ignored, so a confirmed write cannot be altered after the fact.
    * raw ``sql`` → classify it. Reads run for any ``query:run`` caller; writes
      require the explicit ``query:write:manual`` permission (the reviewed-plan
      flow is otherwise mandatory for writes). The permission boundary — not the
      client — decides whether a hand-written write is allowed.
    """
    from dbbuddy_core.safety import classify_query_safety

    if req.execution_token:
        from app_db import execution_tokens
        from app_db.database import SessionLocal

        ctx_hash = execution_tokens.context_hash(engine, host, port, database, user)
        with SessionLocal() as db:
            try:
                row = execution_tokens.redeem(
                    db, token=req.execution_token, user_id=payload.get("sub"),
                    context_hash=ctx_hash,
                )
                sql, safety_category = row.sql, row.safety_category
                db.commit()
            except execution_tokens.TokenContextMismatch as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except execution_tokens.TokenExpiredOrUsed as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except execution_tokens.TokenInvalid as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return sql, safety_category, "token"

    sql = req.sql
    if not sql or not sql.strip():
        raise HTTPException(status_code=400, detail="No SQL or execution token provided.")
    safety_category, _ = classify_query_safety(sql)
    if safety_category != "read" and "query:write:manual" not in (payload.get("permissions") or []):
        raise HTTPException(
            status_code=403,
            detail=(
                "Direct execution of write SQL requires the 'query:write:manual' permission. "
                "Generate the change through /query and run it with the returned execution token."
            ),
        )
    return sql, safety_category, "manual"


@app.post("/execute")
def execute(req: ExecuteRequest, payload: dict = Depends(require_token_permission("query:run"))):
    try:
        from dbbuddy_core.db import connect_db
        from dbbuddy_core.query import execute_query as run_query

        host, user, password, database, engine, port = _resolve_connection(req, payload)

        # Determine the authoritative SQL (server-stored via token, or a
        # permission-checked raw statement) before touching the database.
        sql, safety_category, source = _resolve_execute_sql(
            req, payload, engine=engine, host=host, port=port, database=database, user=user,
        )

        # Backpressure, same ceiling the pipeline and dashboard paths honor. This
        # endpoint opened an unpooled connection per call with no cap, so a burst
        # of confirmed writes could open arbitrarily many sessions against a
        # customer ERP — the exact failure erp_concurrency exists to prevent.
        from dbbuddy_core.erp_concurrency import query_slot

        with query_slot(engine, host, port, database):
            conn = connect_db(host, user, password, database, engine=engine, port=port)
            if conn is None:
                raise DatabaseUnavailableError("Unable to connect to the database.")

            # Enable autocommit to prevent lock timeouts on write queries.
            # Each statement is its own transaction — no lingering locks.
            try:
                conn.autocommit = True
            except Exception:
                pass

            try:
                results = run_query(conn, sql)
            finally:
                # Always close the connection after execution to release all locks.
                try:
                    conn.close()
                except Exception:
                    pass

        # Detect write result — execute_query returns [{rows_affected: N}] for writes
        if results and "rows_affected" in results[0]:
            rows_affected = results[0]["rows_affected"]
            _audit_db_action(payload, req, action="execute", detail={
                "sql": _clip(sql), "safety_category": safety_category, "source": source,
                "write": True, "rows_affected": rows_affected,
            })
            return {
                "results": [],
                "rows_affected": rows_affected,
                "message": f"Query executed successfully. {rows_affected} row(s) affected.",
                "write": True,
            }

        _audit_db_action(payload, req, action="execute", detail={
            "sql": _clip(sql), "safety_category": safety_category, "source": source,
            "write": False, "row_count": len(results),
        })
        return {"results": results, "write": False}
    except HTTPException:
        raise
    except ERPBusy as exc:
        raise _erp_busy(exc) from exc
    except DatabaseUnavailableError as exc:
        raise HTTPException(status_code=502, detail=_err_detail(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc, context="/execute") from exc


@app.post("/rebuild-context")
def rebuild_context(req: AnalyzeRequest, payload: dict = Depends(require_token_permission("schema:analyze"))):
    """Force a rebuild of a database's prepared context (manual invalidation).

    Re-fetches the schema, re-runs AI refinement, re-indexes the vector store,
    and persists the result — same heavy path as Analyze Schema. Call this after
    a schema change or an ai_provider change to apply it without mixing models.
    """
    from dbbuddy_core import context_store

    try:
        host, user, password, database, engine, port = _resolve_connection(req, payload)
        config = DBConfig(
            host=host, user=user, password=password, database=database,
            port=port, engine=engine, ai=req.ai, ai_provider=req.ai_provider,
            ai_provider_chain=_resolve_ai_chain(payload),
        )
        context_store.rebuild(config)
        return context_store.get_status(config)
    except HTTPException:
        raise
    except ERPBusy as exc:
        raise _erp_busy(exc) from exc
    except DatabaseUnavailableError as exc:
        # Target ERP DB unreachable/unusable — an upstream problem, not a server bug.
        raise HTTPException(status_code=502, detail=_err_detail(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc, context="/rebuild-context") from exc


@app.post("/analyze-status")
def analyze_status(req: AnalyzeRequest, payload: dict = Depends(require_token_permission("schema:analyze"))):
    """Report readiness for a database: schema analyzed, semantic + vector ready.

    Lightweight — does a schema fetch but no AI refinement or re-indexing.
    """
    from dbbuddy_core import context_store

    try:
        host, user, password, database, engine, port = _resolve_connection(req, payload)
        config = DBConfig(
            host=host, user=user, password=password, database=database,
            port=port, engine=engine, ai=req.ai, ai_provider=req.ai_provider,
            ai_provider_chain=_resolve_ai_chain(payload),
        )
        return context_store.get_status(config)
    except HTTPException:
        raise
    except ERPBusy as exc:
        raise _erp_busy(exc) from exc
    except DatabaseUnavailableError as exc:
        # Target ERP DB unreachable/unusable — an upstream problem, not a server bug.
        raise HTTPException(status_code=502, detail=_err_detail(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc, context="/analyze-status") from exc


@app.get("/engines")
def engines():
    """List supported database engines."""
    return {"engines": SUPPORTED_ENGINES}


@app.get("/context-metrics")
def context_metrics(_: dict = Depends(get_token_payload)):
    """Context-cache and latency metrics: build time, hit rate, cold/warm ms."""
    from dbbuddy_core import context_store

    return context_store.get_metrics()


@app.get("/ai-metrics")
def ai_metrics(_: dict = Depends(require_token_permission("settings:ai"))):
    """Per-provider AI-call metrics: latency (avg/last), retries, failovers,
    rate-limit hits, skips, and circuit-breaker state. Provider-independent
    observability for the labeling path."""
    from dbbuddy_core import ai_metrics as _m
    from dbbuddy_core.ai_providers import breaker_snapshot

    breakers = breaker_snapshot()
    rows = _m.snapshot()
    for row in rows:
        b = breakers.get(row["name"])
        row["circuit_state"] = b["state"] if b else "closed"
        row["circuit_cooldown_s"] = b["cooldown_remaining_s"] if b else 0.0
    return {"providers": rows}


@app.get("/ai-health")
def ai_health(
    provider: AIProvider = "hybrid",
    _: dict = Depends(require_token_permission("settings:ai")),
):
    """Diagnose the AI labeling path through this backend process (no DB needed).

    Runs ai_refine on a tiny sample schema and reports whether AI actually
    produced terms, which provider, and the last error — so a silent fallback to
    rule-based labeling can be debugged without touching the database.
    """
    from dbbuddy_core.ai import (
        ai_refine, get_last_provider_error, is_ollama_running, OLLAMA_URL,
    )

    sample = {"users": ["id", "name", "amount", "created_at"]}
    semantic = {t: {c: {"term": c, "source": "rule"} for c in cols} for t, cols in sample.items()}
    try:
        out = ai_refine(semantic, provider, schema=sample)
    except Exception as exc:
        return {"provider": provider, "error": str(exc)}

    cols = [ci for tbl in out.values() for ci in tbl.values()]
    return {
        "provider": provider,
        "ollama_url": OLLAMA_URL,
        "ollama_reachable": is_ollama_running(),
        "ai_used": any(c.get("source") == "ai" for c in cols),
        "providers_used": sorted({c.get("provider") for c in cols if c.get("provider")}),
        "sample_terms": {f"{t}.{col}": ci.get("term") for t, tbl in out.items() for col, ci in tbl.items()},
        "last_error": get_last_provider_error(),
    }


@app.get("/api-key")
def api_key_status(
    provider: ManagedKeyProvider = "nemotron",
    _: dict = Depends(require_token_permission("settings:ai")),
):
    """Report whether an API key is configured. Never returns the key value."""
    return {
        "provider": provider,
        "has_key": secrets_store.has_api_key(provider),
        "backend": secrets_store.backend_name(),  # "keyring" or "file"
    }


@app.post("/api-key")
def api_key_set(req: ApiKeyRequest, _: dict = Depends(require_token_permission("settings:ai"))):
    """Store an API key securely. The value is never echoed back."""
    try:
        secrets_store.set_api_key(req.provider, req.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err_detail(exc)) from exc
    return {"provider": req.provider, "has_key": True}


@app.delete("/api-key")
def api_key_delete(
    provider: ManagedKeyProvider = "nemotron",
    _: dict = Depends(require_token_permission("settings:ai")),
):
    """Delete a stored API key."""
    try:
        secrets_store.delete_api_key(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err_detail(exc)) from exc
    return {"provider": provider, "has_key": False}
