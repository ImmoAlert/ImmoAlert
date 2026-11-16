from __future__ import annotations

import gzip
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import test_web_api as base
from config import settings
from database import models as m, repo
from services import backup, extra_channels


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls, self.host, self.port = [], host, port
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["Subject"]))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SelfHostedWebTests.asyncSetUp

    def configure(self, **values):
        for name, value in values.items():
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_email_uses_starttls_and_login(self):
        self.configure(smtp_host="smtp.test", smtp_port=587, smtp_user="u", smtp_password="p", smtp_sender="a@test.fr")
        FakeSmtp.instances.clear()
        with patch("services.extra_channels.smtplib.SMTP", FakeSmtp):
            await extra_channels.send_email("moi@test.fr", "Sujet", "Corps")
        self.assertEqual(FakeSmtp.instances[0].calls, ["starttls", ("login", "u"), ("send", "moi@test.fr", "Sujet")])

    async def test_email_port_465_uses_implicit_tls(self):
        self.configure(smtp_host="smtp.test", smtp_port=465, smtp_user="", smtp_sender="a@test.fr")
        FakeSmtp.instances.clear()
        with patch("services.extra_channels.smtplib.SMTP_SSL", FakeSmtp):
            await extra_channels.send_email("moi@test.fr", "Sujet", "Corps")
        self.assertEqual(FakeSmtp.instances[0].calls, [("send", "moi@test.fr", "Sujet")])

    async def test_failed_email_asks_for_retry(self):
        self.configure(smtp_host="smtp.test", smtp_port=587, smtp_sender="a@test.fr")
        with patch("services.extra_channels.send_email", side_effect=OSError("down")):
            ok = await extra_channels.deliver(self.member.id, {"email_alerts": True, "email": "m@test.fr"}, "Titre", "Corps")
        self.assertFalse(ok)

    async def test_push_is_sent_with_vapid_and_gone_subscription_is_dropped(self):
        self.configure(vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@test.fr")
        extra_channels.save_subscription(self.member.id, {"endpoint": "https://push.test/1", "keys": {"p256dh": "k", "auth": "a"}})
        self.assertEqual(len(extra_channels.subscriptions(self.member.id)), 1)
        sent = MagicMock()
        with patch("pywebpush.webpush", sent):
            ok = await extra_channels.deliver(self.member.id, {"push_alerts": True}, "Titre", "Corps", "/?listing=1")
        self.assertTrue(ok)
        kwargs = sent.call_args.kwargs
        self.assertEqual(kwargs["subscription_info"]["endpoint"], "https://push.test/1")
        self.assertEqual(kwargs["vapid_private_key"], "priv")
        from pywebpush import WebPushException
        gone = WebPushException("gone", response=SimpleNamespace(status_code=410))
        with patch("pywebpush.webpush", side_effect=gone):
            await extra_channels.deliver(self.member.id, {"push_alerts": True}, "Titre", "Corps")
        self.assertEqual(extra_channels.subscriptions(self.member.id), [])

    async def test_push_rejects_insecure_endpoint_and_limits_devices(self):
        with self.assertRaises(ValueError):
            extra_channels.save_subscription(self.member.id, {"endpoint": "http://x.test", "keys": {"p256dh": "k", "auth": "a"}})
        for index in range(7):
            try:
                extra_channels.save_subscription(self.member.id, {"endpoint": f"https://push.test/{index}", "keys": {"p256dh": "k", "auth": "a"}})
            except ValueError:
                pass
        self.assertLessEqual(len(extra_channels.subscriptions(self.member.id)), 5)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SelfHostedWebTests.asyncSetUp

    def prepare(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        database = root / "data.db"
        connection = sqlite3.connect(database)
        connection.execute("create table t(x)")
        connection.execute("insert into t values (42)")
        connection.commit()
        connection.close()
        for name, value in {"database_url": f"sqlite:///{database}", "backup_dir": str(root / "out"), "backup_retention": 2}.items():
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return root

    async def test_sqlite_backup_is_restorable_throttled_and_pruned(self):
        root = self.prepare()
        now = datetime(2026, 1, 10, 3, 0, 0)
        first = backup.run_backup(now=now)
        self.assertTrue(first and first.is_file())
        self.assertEqual(first.stat().st_mode & 0o077, 0)
        restored = root / "restored.db"
        restored.write_bytes(gzip.decompress(first.read_bytes()))
        self.assertEqual(sqlite3.connect(restored).execute("select x from t").fetchone(), (42,))
        self.assertIsNone(backup.run_backup(now=now + timedelta(hours=2)))
        for day in (1, 2, 3):
            self.assertIsNotNone(backup.run_backup(now=now + timedelta(days=day)))
        self.assertEqual(len(list((root / "out").glob("immoalert-*.gz"))), 2)

    async def test_postgres_backup_uses_pg_dump_without_password_in_arguments(self):
        root = self.prepare()
        with patch.object(settings, "database_url", "postgresql://bot:s3cret@db.test:5433/immo"), \
                patch("services.backup.shutil.which", return_value="/usr/bin/pg_dump"), \
                patch("services.backup.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"-- dump", stderr=b"")) as run:
            path = backup.run_backup(force=True)
        command = run.call_args.args[0]
        self.assertNotIn("s3cret", " ".join(command))
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "s3cret")
        self.assertEqual(gzip.decompress(path.read_bytes()), b"-- dump")
        self.assertIn("5433", command)

    async def test_failed_dump_leaves_no_file_and_no_success_marker(self):
        root = self.prepare()
        with patch.object(settings, "database_url", "postgresql://bot:x@db.test/immo"), \
                patch("services.backup.shutil.which", return_value="/usr/bin/pg_dump"), \
                patch("services.backup.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")):
            self.assertIsNone(backup.run_backup(force=True))
        self.assertEqual(list((root / "out").glob("*")), [])
        self.assertIsNone(repo.get_runtime_setting(backup.MARKER))


if __name__ == "__main__":
    unittest.main()
