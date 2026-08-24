"""Per-account query history (replaces the browser localStorage history)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_permission
from ..models import QueryHistory, User
from ..schemas import HistoryIn, HistoryOut, HistoryPatch

router = APIRouter(prefix="/history", tags=["history"])

# Unpinned history is capped per user/connection; pinned (saved) rows are kept.
UNPINNED_CAP = 100


@router.get("", response_model=list[HistoryOut])
def list_history(
    connection_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("history:read")),
):
    q = db.query(QueryHistory).filter(QueryHistory.user_id == user.id)
    if connection_id:
        q = q.filter(QueryHistory.database_connection_id == connection_id)
    return q.order_by(QueryHistory.created_at.desc()).all()


@router.post("", response_model=HistoryOut, status_code=status.HTTP_201_CREATED)
def add_history(req: HistoryIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = QueryHistory(
        user_id=user.id,
        database_connection_id=req.database_connection_id,
        nl_query=req.nl_query,
        sql=req.sql,
        status=req.status,
        confidence=req.confidence,
        pinned=req.pinned,
    )
    db.add(item)

    # Evict oldest unpinned beyond the cap (scoped to this connection).
    scope = db.query(QueryHistory).filter(
        QueryHistory.user_id == user.id,
        QueryHistory.database_connection_id == req.database_connection_id,
        QueryHistory.pinned == False,  # noqa: E712
    ).order_by(QueryHistory.created_at.desc()).all()
    for stale in scope[UNPINNED_CAP:]:
        db.delete(stale)

    db.flush()
    db.commit()
    db.refresh(item)
    return item


@router.patch("/{item_id}", response_model=HistoryOut)
def update_history(item_id: str, req: HistoryPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = db.get(QueryHistory, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "History item not found")
    item.pinned = req.pinned
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_history(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = db.get(QueryHistory, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "History item not found")
    db.delete(item)
    db.commit()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_history(
    connection_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clear unpinned history (pinned/saved queries are kept)."""
    q = db.query(QueryHistory).filter(
        QueryHistory.user_id == user.id,
        QueryHistory.pinned == False,  # noqa: E712
    )
    if connection_id:
        q = q.filter(QueryHistory.database_connection_id == connection_id)
    q.delete(synchronize_session=False)
    db.commit()
