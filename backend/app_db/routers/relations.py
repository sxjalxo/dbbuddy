"""Relation graph — analyst-only view of how databases and tables interconnect.

Two levels (see ``relations_service``):

* ``GET /relations/overview`` — one node per connection, inferred cross-database
  links. Built purely from stored snapshots, so it scales to hundreds of
  databases with no live-DB access.
* ``GET /relations/detail/{connection_id}`` — the table/FK graph for one database,
  from its snapshot.

Snapshots are captured on demand:

* ``POST /relations/snapshot/{connection_id}`` — introspect that one live database
  and (over)write its snapshot.
* ``POST /relations/snapshot`` — refresh every connection the caller owns
  (best-effort; per-connection failures are reported, not fatal).

Gated on ``schema:analyze``, which only the analyst role holds — so the whole
feature is analyst-only, enforced server-side (not just hidden in the UI).
"""

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_permission, write_audit
from ..models import DatabaseConnection, SchemaSnapshot, User
from ..relations_service import build_detail_graph, build_overview_graph, snapshot_from_rich
from ..security import decrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/relations", tags=["relations"])

# The analyst role is the only one granted schema:analyze, so this gates the
# entire relation-graph feature to analysts (server-side, not just UI-hidden).
_analyst = require_permission("schema:analyze")


def _owned_connections(db: Session, user: User) -> list[DatabaseConnection]:
    return (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.user_id == user.id)
        .order_by(DatabaseConnection.created_at.asc())
        .all()
    )


def _snapshots_by_connection(db: Session, connection_ids: list[str]) -> dict[str, SchemaSnapshot]:
    if not connection_ids:
        return {}
    rows = (
        db.query(SchemaSnapshot)
        .filter(SchemaSnapshot.connection_id.in_(connection_ids))
        .all()
    )
    return {r.connection_id: r for r in rows}


def _fingerprint(data: dict) -> str:
    """Stable hash of a snapshot's structural shape, to detect real changes."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _capture_snapshot(db: Session, conn: DatabaseConnection) -> SchemaSnapshot:
    """Introspect one live database and (over)write its stored snapshot.

    Raises ``HTTPException`` with an actionable message on a decryption or
    connection failure — the caller maps these into a per-connection result for
    the bulk path, or returns them directly for the single path.
    """
    from cryptography.fernet import InvalidToken

    from dbbuddy_core.db import connect_db

    try:
        password = decrypt_secret(conn.password_encrypted)
    except InvalidToken as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Saved credentials can no longer be decrypted (the app encryption key "
            "changed). Re-enter this connection, then snapshot again.",
        ) from exc

    dialect_conn = connect_db(
        host=conn.host, user=conn.username, password=password,
        database=conn.database, engine=conn.engine, port=conn.port,
    )
    if dialect_conn is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not connect to {conn.engine} database '{conn.database}' on {conn.host}.",
        )
    try:
        rich = dialect_conn.fetch_schema_rich()
    except Exception as exc:  # noqa: BLE001 — upstream DB problem, not a server bug
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not read the schema of '{conn.database}': {exc}",
        ) from exc
    finally:
        dialect_conn.close()

    data = snapshot_from_rich(rich)
    snap = db.query(SchemaSnapshot).filter_by(connection_id=conn.id).one_or_none()
    if snap is None:
        snap = SchemaSnapshot(connection_id=conn.id)
        db.add(snap)
    snap.data = data
    snap.table_count = len(data.get("tables", []))
    snap.fingerprint = _fingerprint(data)
    return snap


@router.post("/snapshot/{connection_id}")
def snapshot_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_analyst),
):
    """Introspect one live database and refresh its stored schema snapshot."""
    conn = db.get(DatabaseConnection, connection_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")

    snap = _capture_snapshot(db, conn)
    write_audit(db, user_id=user.id, action="snapshot", entity_type="connection",
                entity_id=conn.id, organization_id=user.organization_id,
                detail={"table_count": snap.table_count})
    db.commit()
    db.refresh(snap)
    return {
        "connection_id": conn.id,
        "table_count": snap.table_count,
        "captured_at": snap.captured_at,
    }


@router.post("/snapshot")
def snapshot_all(db: Session = Depends(get_db), user: User = Depends(_analyst)):
    """Refresh snapshots for every connection the caller owns.

    Best-effort: a connection that can't be reached is reported in ``failed``
    rather than failing the whole batch, so one dead database does not block
    refreshing the rest of a large fleet.

    Each capture runs in its own **savepoint** (``begin_nested``). A failure rolls
    back only that connection's work and leaves every sibling capture intact, and
    the batch still lands in a single outer commit. A plain ``rollback()`` here
    would discard the *earlier successful* captures too while they were still
    reported as captured — the API would report a snapshot the database never
    stored (QA finding #11).
    """
    conns = _owned_connections(db, user)
    captured, failed = [], []
    for conn in conns:
        try:
            with db.begin_nested():
                snap = _capture_snapshot(db, conn)
                table_count = snap.table_count
            captured.append({"connection_id": conn.id, "table_count": table_count})
        except HTTPException as exc:
            # The savepoint has already rolled back this connection's partial work.
            failed.append({"connection_id": conn.id, "name": conn.name, "error": exc.detail})
    if captured:
        write_audit(db, user_id=user.id, action="snapshot_all", entity_type="connection",
                    organization_id=user.organization_id,
                    detail={"captured": len(captured), "failed": len(failed)})
        db.commit()
    return {"captured": captured, "failed": failed}


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(_analyst)):
    """The database-level graph across every connection the caller owns, built
    from stored snapshots (no live-DB access)."""
    conns = _owned_connections(db, user)
    snaps = _snapshots_by_connection(db, [c.id for c in conns])
    databases = []
    for c in conns:
        snap = snaps.get(c.id)
        databases.append({
            "id": c.id,
            "label": c.name,
            "engine": c.engine,
            "database": c.database,
            "snapshot": snap.data if snap else None,
            "captured_at": snap.captured_at.isoformat() if snap else None,
        })
    return build_overview_graph(databases)


@router.get("/detail/{connection_id}")
def detail(connection_id: str, db: Session = Depends(get_db), user: User = Depends(_analyst)):
    """The table/FK graph for one database, from its snapshot."""
    conn = db.get(DatabaseConnection, connection_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    snap = db.query(SchemaSnapshot).filter_by(connection_id=conn.id).one_or_none()
    if snap is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This database has not been snapshotted yet. Refresh it first.",
        )
    return build_detail_graph(conn.id, conn.name, snap.data)
