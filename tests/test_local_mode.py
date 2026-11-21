from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collectors import PLATFORMS
from notifications import Notifications
from services import runtime_info, run_local_notices
from storage import Store


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.notices = Notifications(self.store)

    def test_local_default_does_not_enable_email_even_with_smtp_config(self):
        with patch.dict("os.environ", {"HK_SMTP_HOST": "smtp.example.com", "HK_SMTP_USER": "test@example.com",
                                      "HK_SMTP_PASSWORD": "fake-test-only", "HK_MAIL_TO": "test@example.com"}, clear=True):
            info = runtime_info()
        self.assertEqual(info["mode"], "local")
        self.assertFalse(info["mail_enabled"])
        self.assertEqual(info["quote_mode"], "public_snapshot")

    def test_read_receipts_persist_and_are_independent_of_email_delivery(self):
        with self.store.connect() as db:
            self.store.add_event(db, "offer:12:1", "【港股工作台】新活动", "仅用于本地提醒测试")
        run_local_notices(self.store)
        first = self.notices.inbox()
        self.assertEqual(first["unread"], 1)
        self.assertEqual(first["events"][0]["subject"], "新活动")
        self.notices.read_notice(first["events"][0]["id"])
        rebuilt = Notifications(Store(self.store.root))
        self.assertEqual(rebuilt.inbox()["unread"], 0)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT sent_at FROM outbox").fetchone()[0])

    def test_local_notice_does_not_wait_for_digest_and_invalid_deadline_is_removed(self):
        from datetime import datetime, timedelta, timezone
        ending = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        offer_id, _ = self.store.save_offer({"platform": "welab", "title": "本地临期测试", "text": "测试条件",
                                           "url": "https://www.welab.bank/zh/test", "deadline": ending})
        self.store.patch_offer(offer_id, {"followed": True})
        run_local_notices(self.store)
        self.assertEqual(self.notices.inbox()["unread"], 1)
        self.store.patch_offer(offer_id, {"followed": False})
        run_local_notices(self.store)
        self.assertEqual(self.notices.inbox()["unread"], 0)


if __name__ == "__main__":
    unittest.main()
