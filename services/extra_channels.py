"""Optional delivery channels beyond Telegram: e-mail (SMTP) and Web Push."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from email.message import EmailMessage
import json
import logging
import smtplib
import ssl
from urllib.parse import urlsplit

from sqlalchemy import select

from config import settings
from database import models as m, repo
from services import alert_preferences as preferences

log = logging.getLogger(__name__)
WINDOW = timedelta(days=2)


def email_available():
    return bool(settings.smtp_host and settings.smtp_sender)


def push_available():
    if not (settings.vapid_public_key and settings.vapid_private_key):
        return False
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return True


def channel_status():
    return {"email": email_available(), "push": push_available(), "vapid_public_key": settings.vapid_public_key if push_available() else None}


def _site_link(listing):
    site = (settings.web_site_url or "").rstrip("/")
    link = f"{site}/?listing={listing.id}" if site else listing.url
    return link if urlsplit(link or "").scheme in {"http", "https"} else ""


def notice_text(notice, listing):
    """Plain French text shared by e-mail and push notifications."""
    data = json.loads(notice.payload or "{}")
    title = (listing.title or "Bien immobilier")[:100]
    if notice.kind == "price_drop":
        return "Baisse de loyer", f"{title} : {data.get('previous_price')} → {data.get('price')} €/mois"
    if notice.kind == "back_on_market":
        return "De retour sur le marché", f"{title} est de nouveau disponible."
    if notice.kind == "visit_reminder":
        return "Rappel de visite", f"{title} : visite prévue le {str(data.get('visit_at', ''))[:16].replace('T', ' à ')} (UTC)."
    return "ImmoAlert", title


def _send_email_sync(to, subject, body):
    message = EmailMessage()
    message["From"] = settings.smtp_sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with server:
        if settings.smtp_port != 465:
            server.starttls(context=context)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
        server.send_message(message)


async def send_email(to, subject, body):
    await asyncio.to_thread(_send_email_sync, to, subject, body)


def _send_push_sync(subscription, payload):
    from pywebpush import webpush, WebPushException
    try:
        webpush(subscription_info={"endpoint": subscription["endpoint"], "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
                data=json.dumps(payload, ensure_ascii=False), vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject}, ttl=86400, timeout=15)
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in {404, 410}:
            return False  # The browser revoked this subscription.
        raise


def subscriptions(user_id):
    with repo.session_scope() as session:
        return [{"id": row.id, "endpoint": row.endpoint, "p256dh": row.p256dh, "auth": row.auth}
                for row in session.scalars(select(m.PushSubscription).where(m.PushSubscription.user_id == user_id))]


def save_subscription(user_id, data):
    endpoint, keys = data.get("endpoint"), data.get("keys") or {}
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 600:
        raise ValueError("Abonnement de notification invalide.")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str) or not 0 < len(p256dh) <= 200 or not 0 < len(auth) <= 100:
        raise ValueError("Abonnement de notification invalide.")
    with repo.session_scope() as session:
        count = len(list(session.scalars(select(m.PushSubscription.id).where(m.PushSubscription.user_id == user_id))))
        row = session.scalar(select(m.PushSubscription).where(m.PushSubscription.endpoint == endpoint))
        if row and row.user_id != user_id:
            row.user_id = user_id
        elif not row:
            if count >= 5:
                raise ValueError("Limite de cinq appareils atteinte.")
            session.add(m.PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth))
        else:
            row.p256dh, row.auth = p256dh, auth


def remove_subscription(user_id, endpoint):
    with repo.session_scope() as session:
        row = session.scalar(select(m.PushSubscription).where(m.PushSubscription.endpoint == endpoint, m.PushSubscription.user_id == user_id))
        if row:
            session.delete(row)


def _drop_subscription(identifier):
    with repo.session_scope() as session:
        row = session.get(m.PushSubscription, identifier)
        if row:
            session.delete(row)


async def deliver(user_id, prefs, title, body, link=""):
    """Best effort on every enabled channel. Returns False when a retry is useful."""
    ok = True
    if prefs.get("email_alerts") and prefs.get("email") and email_available():
        try:
            await send_email(prefs["email"], "ImmoAlert · " + title, body + (("\n\n" + link) if link else "") + "\n\nVous recevez ce message car les alertes e-mail sont activées dans Mon compte.")
        except Exception as exc:
            log.warning("E-mail notification failed (%s)", type(exc).__name__)
            ok = False
    if prefs.get("push_alerts") and push_available():
        for subscription in await asyncio.to_thread(subscriptions, user_id):
            try:
                alive = await asyncio.to_thread(_send_push_sync, subscription, {"title": title, "body": body, "url": link or "/"})
                if not alive:
                    await asyncio.to_thread(_drop_subscription, subscription["id"])
            except Exception as exc:
                log.warning("Push notification failed (%s)", type(exc).__name__)
                ok = False
    return ok


def _wants_extra(prefs, user_id):
    if prefs.get("email_alerts") and prefs.get("email") and email_available():
        return True
    return bool(prefs.get("push_alerts") and push_available() and subscriptions(user_id))


async def dispatch_extra_channels(now=None):
    """Send recent inbox notices to e-mail/push once; quiet hours defer them."""
    from services.member_notifications import _valid_notice
    now = now or datetime.utcnow()
    if not (email_available() or push_available()):
        return
    with repo.session_scope() as session:
        disabled = set(session.scalars(select(m.WebAccount.user_id).where(m.WebAccount.enabled == False)))  # noqa: E712
        notices = list(session.scalars(select(m.MemberNotification).where(
            m.MemberNotification.extra_sent_at.is_(None), m.MemberNotification.created_at >= now - WINDOW,
            m.MemberNotification.not_before <= now).order_by(m.MemberNotification.created_at).limit(200)))
    cache = {}
    for notice in notices:
        if notice.user_id in disabled:
            continue
        if notice.user_id not in cache:
            prefs = await asyncio.to_thread(preferences.get_preferences, notice.user_id)
            cache[notice.user_id] = prefs, await asyncio.to_thread(_wants_extra, prefs, notice.user_id)
        prefs, wanted = cache[notice.user_id]
        if not wanted:
            continue
        if preferences.is_quiet(prefs, now):
            continue
        listing = await asyncio.to_thread(_valid_notice, notice, prefs, now)
        done = True
        if listing:
            title, body = notice_text(notice, listing)
            done = await deliver(notice.user_id, prefs, title, body, _site_link(listing))
        if done:
            with repo.session_scope() as session:
                row = session.get(m.MemberNotification, notice.id)
                if row:
                    row.extra_sent_at = now
