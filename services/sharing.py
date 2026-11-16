"""Read-only share links for a comparison. Notes and personal criteria never leave the account."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import secrets

from sqlalchemy import select, func

from database import models as m, repo
from services import site_sync

MAX_ACTIVE = 10
MAX_DAYS = 30


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _serialize(row, now):
    return {"id": row.id, "title": row.title, "created_at": row.created_at.isoformat() + "Z", "expires_at": row.expires_at.isoformat() + "Z",
            "count": len(json.loads(row.listing_ids)), "active": row.revoked_at is None and row.expires_at > now}


def create(user_id, ids, title="", days=7, *, now=None):
    now = now or datetime.utcnow()
    if not isinstance(ids, list) or not 2 <= len(ids) <= 4 or any(type(i) is not int or not 0 < i <= 1000000000 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Sélectionnez de deux à quatre favoris différents.")
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"La durée doit être de 1 à {MAX_DAYS} jours.")
    if not isinstance(title, str) or len(title) > 80:
        raise ValueError("Le titre doit contenir au maximum 80 caractères.")
    with repo.session_scope() as session:
        canonical = []
        for identifier in ids:
            row = session.get(m.Listing, identifier)
            if row and row.duplicate_of_id:
                row = session.get(m.Listing, row.duplicate_of_id)
            favorite = row and session.scalar(select(m.Favorite.id).where(m.Favorite.user_id == user_id, m.Favorite.listing_id == row.id))
            if not row or row.removed_at is not None or not favorite:
                raise PermissionError("Vous pouvez partager uniquement vos favoris.")
            canonical.append(row.id)
        if len(set(canonical)) != len(canonical):
            raise ValueError("Sélectionnez des biens différents.")
        active = session.scalar(select(func.count()).select_from(m.SharedLink).where(m.SharedLink.user_id == user_id, m.SharedLink.revoked_at.is_(None), m.SharedLink.expires_at > now))
        if active >= MAX_ACTIVE:
            raise ValueError("Limite de dix liens actifs atteinte. Révoquez un ancien lien.")
        token = secrets.token_urlsafe(24)
        row = m.SharedLink(token_hash=_hash(token), user_id=user_id, title=title.strip(), listing_ids=json.dumps(canonical), created_at=now, expires_at=now + timedelta(days=days))
        session.add(row)
        session.flush()
        return {"token": token, "share": _serialize(row, now)}


def list_for(user_id, *, now=None):
    now = now or datetime.utcnow()
    with repo.session_scope() as session:
        rows = session.scalars(select(m.SharedLink).where(m.SharedLink.user_id == user_id).order_by(m.SharedLink.id.desc()).limit(50))
        return [_serialize(row, now) for row in rows]


def revoke(user_id, identifier, *, now=None):
    with repo.session_scope() as session:
        row = session.get(m.SharedLink, identifier)
        if not row or row.user_id != user_id:
            raise ValueError("Lien introuvable.")
        row.revoked_at = row.revoked_at or (now or datetime.utcnow())


def resolve(token, *, now=None):
    now = now or datetime.utcnow()
    if not isinstance(token, str) or not 20 <= len(token) <= 80:
        return None
    with repo.session_scope() as session:
        row = session.scalar(select(m.SharedLink).where(m.SharedLink.token_hash == _hash(token)))
        if not row or row.revoked_at is not None or row.expires_at <= now:
            return None
        ids, title, expires = json.loads(row.listing_ids), row.title, row.expires_at
    items = []
    for identifier in ids:
        listing = repo.get_listing_by_id(identifier)
        if not listing or listing.removed_at is not None or listing.inventory_status == "invalid":
            continue
        item = site_sync._listing(listing, full=True, publications=repo.listing_publications(listing.id))
        item["images"] = item["images"][:1]
        item["description"] = ""
        items.append(item)
    return {"title": title, "expires_at": expires.isoformat() + "Z", "items": items}
