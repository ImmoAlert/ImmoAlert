"""Tell administrators once when a portal keeps failing, and once when it recovers."""
from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape
import logging

from sqlalchemy import select

from config import settings
from database import models as m, repo
from services import audit

log = logging.getLogger(__name__)
KEY = "source_alert:"


def evaluate(source, runs, state):
    """Return 'down', 'recovered' or None from the newest-first runs of a source.

    Skipped runs carry no evidence. A portal is down after N consecutive
    failures; it recovers on a run that produced listings without failing.
    """
    runs = [run for run in runs if run.status != "skipped"]
    need = settings.source_alert_failures
    if state != "open":
        return "down" if len(runs) >= need and all(run.status == "failed" for run in runs[:need]) else None
    latest = runs[0] if runs else None
    if latest and (latest.status == "success" or latest.status == "partial" and latest.unique_count > 0):
        return "recovered"
    return None


def admin_chat_ids():
    with repo.session_scope() as session:
        ids = set(settings.admin_telegram_ids)
        admins = session.scalars(select(m.User).join(m.WebAccount, m.WebAccount.user_id == m.User.id).where(m.WebAccount.is_admin == True, m.WebAccount.enabled == True))  # noqa: E712
        ids.update(user.telegram_id for user in admins if user.telegram_id)
        return sorted(ids)


def pending_transitions():
    from services.scheduler import source_names
    changes = []
    with repo.session_scope() as session:
        for name in source_names():
            runs = list(session.scalars(select(m.SourceRun).where(m.SourceRun.source == name).order_by(m.SourceRun.id.desc()).limit(settings.source_alert_failures + 2)))
            state = repo.get_runtime_setting(KEY + name)
            outcome = evaluate(name, runs, state)
            if outcome:
                error = next((run.last_error for run in runs if run.last_error), "") or ""
                changes.append((name, outcome, error[:160]))
    return changes


async def check_sources(bot, *, now=None):
    changes = await asyncio.to_thread(pending_transitions)
    if not changes:
        return []
    chats = await asyncio.to_thread(admin_chat_ids)
    for name, outcome, error in changes:
        if outcome == "down":
            text = f"<b>Source en panne : {escape(name)}</b>\n{settings.source_alert_failures} collectes consécutives ont échoué."
            if error:
                text += f"\nDernière erreur : {escape(error)}"
        else:
            text = f"<b>Source rétablie : {escape(name)}</b>\nLa collecte fonctionne de nouveau."
        sent = not chats
        for chat in chats:
            try:
                await bot.send_message(chat, text, disable_web_page_preview=True)
                sent = True
            except Exception as exc:
                log.warning("Source alert not delivered (%s)", type(exc).__name__)
        # Leave the incident untouched when nobody could be told, so it is retried.
        if sent:
            await asyncio.to_thread(repo.set_runtime_setting, KEY + name, "open" if outcome == "down" else "closed")
            await asyncio.to_thread(audit.record, "source_" + outcome, actor="système", detail=name)
    return changes
