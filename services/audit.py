"""Security journal: authentication, account and sharing events, bounded in size."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

from sqlalchemy import delete, select

from database import models as m, repo

log = logging.getLogger(__name__)
RETENTION_DAYS = 90
MAX_ROWS = 20000


def record(event, *, user_id=None, actor="", detail=""):
    """Never let journaling break the operation it describes."""
    try:
        with repo.session_scope() as session:
            session.add(m.AuditEvent(user_id=user_id, actor=(actor or "Visiteur")[:80], event=event[:40], detail=(detail or "")[:200]))
            session.execute(delete(m.AuditEvent).where(m.AuditEvent.created_at < datetime.utcnow() - timedelta(days=RETENTION_DAYS)))
            oldest = select(m.AuditEvent.id).order_by(m.AuditEvent.id.desc()).offset(MAX_ROWS).limit(1000)
            session.execute(delete(m.AuditEvent).where(m.AuditEvent.id.in_(oldest)))
    except Exception:
        log.warning("Could not write audit event %s", event)


def recent(limit=200, event=None):
    with repo.session_scope() as session:
        query = select(m.AuditEvent).order_by(m.AuditEvent.id.desc()).limit(max(1, min(limit, 500)))
        if event:
            query = query.where(m.AuditEvent.event == event)
        return [{"id": row.id, "user_id": row.user_id, "actor": row.actor, "event": row.event, "detail": row.detail,
                 "created_at": row.created_at.isoformat() + "Z"} for row in session.scalars(query)]
