"""iCalendar (RFC 5545) export of a member's planned visits."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from config import settings
from database import models as m, repo

PRODID = "-//ImmoAlert//Visites//FR"


def _escape(value):
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _fold(line):
    """Fold at 75 octets without splitting a UTF-8 character."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    limit = 75
    for char in line:
        encoded = char.encode()
        if len(current) + len(encoded) > limit:
            parts.append(current.decode())
            current, limit = b"", 74
        current += encoded
    parts.append(current.decode())
    return "\r\n ".join(parts)


def _stamp(value):
    return value.strftime("%Y%m%dT%H%M%SZ")


def _link(listing):
    site = (settings.web_site_url or "").rstrip("/")
    return f"{site}/?listing={listing.id}" if site else listing.url


def _event(note, listing, now):
    lines = ["BEGIN:VEVENT", f"UID:visit-{note.user_id}-{listing.id}@immoalert", f"DTSTAMP:{_stamp(now)}",
             f"DTSTART:{_stamp(note.visit_at)}", f"DTEND:{_stamp(note.visit_at + timedelta(hours=1))}",
             f"SUMMARY:{_escape('Visite : ' + (listing.title or 'Bien immobilier'))}"]
    if listing.location:
        lines.append(f"LOCATION:{_escape(listing.location)}")
    description = "\n".join(part for part in (_link(listing), note.note) if part)
    lines.append(f"DESCRIPTION:{_escape(description[:1500])}")
    if note.reminder_at and note.reminder_at < note.visit_at:
        minutes = int((note.visit_at - note.reminder_at).total_seconds() // 60)
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Visite à venir", f"TRIGGER:-PT{minutes}M", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def build(user_id, *, listing_id=None, now=None):
    now = now or datetime.utcnow()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Visites ImmoAlert"]
    count = 0
    with repo.session_scope() as session:
        query = select(m.MemberListingNote).where(m.MemberListingNote.user_id == user_id, m.MemberListingNote.stage == "visit",
            m.MemberListingNote.visit_at.is_not(None), m.MemberListingNote.visit_at >= now - timedelta(days=1)).order_by(m.MemberListingNote.visit_at)
        if listing_id is not None:
            query = query.where(m.MemberListingNote.listing_id == listing_id)
        for note in session.scalars(query):
            listing = session.get(m.Listing, note.listing_id)
            favorite = session.scalar(select(m.Favorite.id).where(m.Favorite.user_id == user_id, m.Favorite.listing_id == note.listing_id))
            if not listing or listing.removed_at is not None or not favorite:
                continue
            lines += _event(note, listing, now)
            count += 1
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n", count
