"""ERP database connection configs — owned by the user, secrets encrypted at rest.

These records describe how to reach a *customer* database for queries. The
config lives in the application DB; the ERP database itself only ever serves as
a query target.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_permission, write_audit
from ..models import DatabaseConnection, SchemaSnapshot, User
from ..schemas import ConnectionIn, ConnectionOut, ConnectionUpdate
from ..security import decrypt_secret, encrypt_secret
from dbbuddy_core.dialects import DatabaseEngine

router = APIRouter(prefix="/connections", tags=["connections"])


def _connection_out(conn: DatabaseConnection) -> ConnectionOut:
    """Serialize a connection, probing whether its stored password can still be
    decrypted so the UI can flag a stale-key connection for re-entry."""
    credentials_ok = True
    try:
        decrypt_secret(conn.password_encrypted)  # plaintext discarded — probe only
    except Exception:
        credentials_ok = False
    return ConnectionOut(
        id=conn.id, name=conn.name, engine=conn.engine, host=conn.host, port=conn.port,
        username=conn.username, database=conn.database, created_at=conn.created_at,
        credentials_ok=credentials_ok,
    )


@router.get("", response_model=list[ConnectionOut])
def list_connections(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.user_id == user.id)
        .order_by(DatabaseConnection.created_at.desc())
        .all()
    )
    return [_connection_out(c) for c in rows]


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    req: ConnectionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("connection:manage")),
):
    # Canonicalize the engine ("SQL Server"/"mssql"/"postgres" → canonical value)
    # so storage and dialect lookup always agree, regardless of how the UI spells it.
    try:
        engine = DatabaseEngine.normalize(req.engine).value
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported database engine: {req.engine!r}")

    conn = DatabaseConnection(
        user_id=user.id,
        name=req.name,
        engine=engine,
        host=req.host,
        port=req.port,
        username=req.username,
        password_encrypted=encrypt_secret(req.password),
        database=req.database,
    )
    db.add(conn)
    db.flush()
    write_audit(db, user_id=user.id, action="create", entity_type="connection", entity_id=conn.id,
                organization_id=user.organization_id)
    db.commit()
    db.refresh(conn)
    return _connection_out(conn)


@router.patch("/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: str,
    req: ConnectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("connection:manage")),
):
    """Edit a connection. Any subset of fields may change; a non-empty ``password``
    re-encrypts under the current key (the way to heal a connection whose stored
    secret can no longer be decrypted). A blank/omitted password is left as-is."""
    conn = db.get(DatabaseConnection, connection_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")

    if req.engine is not None:
        try:
            conn.engine = DatabaseEngine.normalize(req.engine).value
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported database engine: {req.engine!r}")
    if req.name is not None:
        conn.name = req.name
    if req.host is not None:
        conn.host = req.host
    if req.port is not None:
        conn.port = req.port
    if req.username is not None:
        conn.username = req.username
    if req.database is not None:
        conn.database = req.database
    # Only replace the secret when a new one is actually supplied.
    if req.password:
        conn.password_encrypted = encrypt_secret(req.password)

    write_audit(db, user_id=user.id, action="update", entity_type="connection", entity_id=conn.id,
                organization_id=user.organization_id,
                detail={"password_changed": bool(req.password)})
    db.commit()
    db.refresh(conn)
    return _connection_out(conn)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("connection:manage")),
):
    conn = db.get(DatabaseConnection, connection_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    # Explicitly drop the cached schema snapshot (SQLite won't cascade the FK).
    db.query(SchemaSnapshot).filter(SchemaSnapshot.connection_id == connection_id).delete(synchronize_session=False)
    db.delete(conn)
    write_audit(db, user_id=user.id, action="delete", entity_type="connection", entity_id=connection_id,
                organization_id=user.organization_id)
    db.commit()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_connections(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("connection:manage")),
):
    """Remove every connection owned by the caller (the 'Clear saved databases'
    action). Saved charts/history that referenced them are kept but unlinked."""
    conn_ids = [row[0] for row in db.query(DatabaseConnection.id).filter(DatabaseConnection.user_id == user.id).all()]
    if conn_ids:
        db.query(SchemaSnapshot).filter(SchemaSnapshot.connection_id.in_(conn_ids)).delete(synchronize_session=False)
    db.query(DatabaseConnection).filter(DatabaseConnection.user_id == user.id).delete(synchronize_session=False)
    write_audit(db, user_id=user.id, action="delete_all", entity_type="connection",
                organization_id=user.organization_id)
    db.commit()
