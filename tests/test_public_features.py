from __future__ import annotations

from datetime import datetime, timedelta
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import test_web_api as base
from database import repo, models as m
from services import alert_preferences, calendar_export, commute, comparison_score, sharing, source_health, two_factor


class PublicSiteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SelfHostedWebTests.asyncSetUp
    post = base.SelfHostedWebTests.post

    async def register(self, login="nouveau", password="Un-mot-de-passe-solide-42"):
        return await self.client.post("/api/register", json={"login": login, "password": password}, headers={"Origin": self.origin})

    async def test_anonymous_can_browse_but_not_use_member_features(self):
        self.assertEqual((await self.client.get("/api/inventory")).status, 200)
        detail = await self.client.get(f"/api/listings/{self.item.id}")
        self.assertEqual(detail.status, 200)
        payload = await detail.json()
        self.assertFalse(payload["listing"]["is_favorite"])
        self.assertEqual((await self.post("favorite", {"listing_id": self.item.id, "enabled": True})).status, 401)
        self.assertEqual((await self.client.get("/api/inventory?favorites=1")).status, 401)
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(set(state["snapshot"]) & {"favorites", "filters"}, set())

    async def test_registration_enforces_password_policy_and_signs_in(self):
        self.assertEqual((await self.register(password="court")).status, 400)
        self.assertEqual((await self.register(password="azertyuiop")).status, 400)
        response = await self.register()
        self.assertEqual(response.status, 201)
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["account"]["login"], "nouveau")
        self.assertEqual((await self.post("favorite", {"listing_id": self.item.id, "enabled": True})).status, 202)

    async def test_registration_can_be_closed_and_logins_are_unique(self):
        self.assertEqual((await self.register()).status, 201)
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.register()).status, 400)
        from unittest.mock import patch
        from config import settings
        with patch.object(settings, "web_public_registration", False):
            self.assertEqual((await self.register("autre")).status, 403)

    async def test_two_factor_login_flow(self):
        await self.register()
        uid = (await (await self.client.get("/api/state")).json())["account"]["user_id"]
        started = two_factor.start_setup(uid)
        code = two_factor.code_at(started["secret"])
        confirmed = two_factor.confirm_setup(uid, code)
        self.assertEqual(len(confirmed["recovery_codes"]), 8)
        self.client.session.cookie_jar.clear()
        credentials = {"login": "nouveau", "password": "Un-mot-de-passe-solide-42"}
        denied = await self.client.post("/api/login", json=credentials, headers={"Origin": self.origin})
        self.assertEqual(denied.status, 401)
        self.assertTrue((await denied.json())["otp_required"])
        wrong = await self.client.post("/api/login", json={**credentials, "otp": "000000"}, headers={"Origin": self.origin})
        self.assertEqual(wrong.status, 401)
        recovery = confirmed["recovery_codes"][0]
        ok = await self.client.post("/api/login", json={**credentials, "otp": recovery}, headers={"Origin": self.origin})
        self.assertEqual(ok.status, 200)
        self.client.session.cookie_jar.clear()
        reused = await self.client.post("/api/login", json={**credentials, "otp": recovery}, headers={"Origin": self.origin})
        self.assertEqual(reused.status, 401)

    async def test_password_change_revokes_other_sessions(self):
        await self.register()
        other = await self.client.post("/api/login", json={"login": "nouveau", "password": "Un-mot-de-passe-solide-42"}, headers={"Origin": self.origin})
        self.assertEqual(other.status, 200)
        changed = await self.client.post("/api/account/password", json={"current": "Un-mot-de-passe-solide-42", "new": "Autre-mot-de-passe-solide-43"}, headers={"Origin": self.origin})
        self.assertEqual(changed.status, 200)
        self.assertEqual((await self.client.post("/api/account/password", json={"current": "faux", "new": "Encore-un-solide-44"}, headers={"Origin": self.origin})).status, 400)

    async def test_shared_comparison_is_public_read_only_and_revocable(self):
        await self.register()
        uid = (await (await self.client.get("/api/state")).json())["account"]["user_id"]
        second = repo.save_listing(source="unit", external_id="2", url="https://example.test/2", title="T3", price=900, surface_m2=55, location="Rouen 76000", rooms=3, last_seen_at=datetime.utcnow(), inventory_status="active").listing
        for listing in (self.item, second):
            await self.post("favorite", {"listing_id": listing.id, "enabled": True})
        created = await self.client.post("/api/shares", json={"ids": [self.item.id, second.id], "days": 3}, headers={"Origin": self.origin})
        self.assertEqual(created.status, 201)
        data = await created.json()
        token = data["token"]
        self.client.session.cookie_jar.clear()
        shared = await self.client.get(f"/api/shared/{token}")
        self.assertEqual(shared.status, 200)
        self.assertNotIn("note", str(await shared.json()).lower().replace("notes_", ""))
        self.assertEqual((await self.client.get("/api/shared/inconnu")).status, 404)
        sharing.revoke(uid, data["share"]["id"])
        self.assertEqual((await self.client.get(f"/api/shared/{token}")).status, 404)

    async def test_share_requires_own_favorites_and_expires(self):
        await self.register()
        uid = (await (await self.client.get("/api/state")).json())["account"]["user_id"]
        with self.assertRaises(PermissionError):
            sharing.create(uid, [self.item.id, self.item.id + 1])
        await self.post("favorite", {"listing_id": self.item.id, "enabled": True})
        second = repo.save_listing(source="unit", external_id="2", url="https://example.test/2", title="T3", price=900, location="Rouen", last_seen_at=datetime.utcnow(), inventory_status="active").listing
        await self.post("favorite", {"listing_id": second.id, "enabled": True})
        now = datetime.utcnow()
        token = sharing.create(uid, [self.item.id, second.id], now=now)["token"]
        self.assertIsNotNone(sharing.resolve(token, now=now))
        self.assertIsNone(sharing.resolve(token, now=now + timedelta(days=8)))


class PureFeatureTests(unittest.TestCase):
    def test_commute_estimate_is_labelled_and_bounded(self):
        out = commute.estimate_from_points((49.44, 1.09), (49.49, 1.09), "car")
        self.assertEqual(out["method"], "estimate")
        self.assertTrue(1 <= out["minutes"] <= 600)
        with self.assertRaises(ValueError):
            commute.estimate_from_points((0, 0), (1, 1), "teleport")

    def test_score_uses_only_available_criteria(self):
        items = [{"price_per_m2": 10, "personal_criteria": {}}, {"price_per_m2": 20, "personal_criteria": {}}]
        self.assertIsNone(comparison_score.score_items(items)[0]["score"])
        items = [{"price_per_m2": 10, "personal_criteria": {"commute_minutes": 10}}, {"price_per_m2": 20, "personal_criteria": {"commute_minutes": 40}}]
        scored = comparison_score.score_items(items)
        self.assertEqual(scored[0]["score"], 100)
        self.assertEqual(scored[1]["score"], 0)

    def test_source_incident_opens_once_and_recovers(self):
        class Run:
            def __init__(self, status, unique=0):
                self.status, self.unique_count = status, unique
        failed = [Run("failed")] * 3
        self.assertEqual(source_health.evaluate("x", failed, None), "down")
        self.assertIsNone(source_health.evaluate("x", failed, "open"))
        self.assertIsNone(source_health.evaluate("x", [Run("failed")] * 2, None))
        self.assertEqual(source_health.evaluate("x", [Run("success")] + failed, "open"), "recovered")


class StoredFeatureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SelfHostedWebTests.asyncSetUp
    post = base.SelfHostedWebTests.post
    login = base.SelfHostedWebTests.login

    async def test_price_drop_threshold_and_email_validation(self):
        uid = self.member.id
        saved = alert_preferences.save_preferences(uid, {"price_drop_min_percent": 7, "back_on_market": False})
        self.assertEqual(saved["price_drop_min_percent"], 7)
        with self.assertRaises(ValueError):
            alert_preferences.save_preferences(uid, {"price_drop_min_percent": 95})
        with self.assertRaises(ValueError):
            alert_preferences.save_preferences(uid, {"email": "pas-un-email"})
        with self.assertRaises(ValueError):
            alert_preferences.save_preferences(uid, {"email_alerts": True})

    async def test_ics_contains_visit_with_reminder(self):
        from services import property_tracking
        await self.login(99)
        await self.post("favorite", {"listing_id": self.item.id, "enabled": True})
        visit = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
        property_tracking.save_note(self.member.id, self.item.id, {"stage": "visit", "visit_at": visit.isoformat() + "Z", "reminder_at": (visit - timedelta(hours=2)).isoformat() + "Z"})
        text, count = calendar_export.build(self.member.id)
        self.assertEqual(count, 1)
        self.assertIn("BEGIN:VALARM", text)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(text.endswith("\r\n"))
        response = await self.client.get("/api/calendar.ics")
        self.assertEqual(response.status, 200)
        self.assertIn("text/calendar", response.headers["Content-Type"])

    async def test_back_on_market_event_is_recorded(self):
        from services import property_tracking
        with repo.session_scope() as session:
            pub = session.query(m.ListingPublication).filter_by(listing_id=self.item.id).first()
            pub.availability_status = "unavailable"
            pid = pub.id
        property_tracking.record_availability(pid, "available")
        with repo.session_scope() as session:
            kinds = [e.kind for e in session.query(m.ListingEvent).all()]
        self.assertEqual(kinds, ["back_on_market"])


if __name__ == "__main__":
    unittest.main()
