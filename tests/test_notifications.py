from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collectors import PLATFORMS
from notifications import Notifications
from storage import HK, Store


class NotificationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.notices = Notifications(self.store)
        self.instant = datetime.now(timezone.utc).replace(microsecond=0)

    def quote(self, value, **changes):
        return {"symbol": "HK.00700", "status": "live", "last_price": value,
                "quote_at": self.instant.isoformat(), "captured_at": self.instant.isoformat()} | changes

    def count(self, prefix):
        with self.store.connect() as db:
            return db.execute("SELECT count(*) FROM outbox WHERE event_key LIKE ?", (prefix + "%",)).fetchone()[0]

    def test_stale_or_delayed_quotes_never_fire_and_crossing_survives_restart(self):
        self.notices.save_rule({"symbol": "00700", "field": "price", "operator": "gte", "threshold": 100})
        self.notices.evaluate_quote(self.quote(110, status="delayed"), self.instant)
        self.notices.evaluate_quote(self.quote(110, quote_at=(self.instant - timedelta(minutes=5)).isoformat()), self.instant)
        self.assertEqual(self.count("quote:"), 0)
        self.notices.evaluate_quote(self.quote(110), self.instant)
        self.notices = Notifications(Store(self.store.root))
        self.notices.evaluate_quote(self.quote(112), self.instant)
        self.assertEqual(self.count("quote:"), 1)
        for index, price in enumerate((90, 111, 99, 102), start=1):
            tick = self.instant + timedelta(seconds=index)
            self.notices.evaluate_quote(self.quote(price, quote_at=tick.isoformat(), captured_at=tick.isoformat()), tick)
        self.assertEqual(self.count("quote:"), 1)
        tick = self.instant + timedelta(hours=25)
        self.notices.evaluate_quote(self.quote(90, quote_at=tick.isoformat(), captured_at=tick.isoformat()), tick)
        tick += timedelta(seconds=1)
        self.notices.evaluate_quote(self.quote(102, quote_at=tick.isoformat(), captured_at=tick.isoformat()), tick)
        self.assertEqual(self.count("quote:"), 2)

    def test_digest_groups_new_offers_and_changes_remain_urgent(self):
        local = self.instant.astimezone(HK)
        hour = local.hour
        self.notices.save_settings({"digest_hour": hour})
        offer = {"platform": "welab", "title": "测试条款", "url": "https://www.welab.bank/zh/test",
                 "text": "测试条款：存入 1000 港元", "deadline": (self.instant + timedelta(days=4)).isoformat()}
        offer_id, _ = self.store.save_offer(offer, notify=True)
        self.notices.digest(self.notices.settings(), self.instant)
        self.notices.digest(self.notices.settings(), self.instant)
        self.assertEqual(self.count("digest:"), 1)
        pending = self.store.pending_events(PLATFORMS)
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["event_key"].startswith("digest:"))
        self.store.save_offer(offer | {"deadline": (self.instant + timedelta(days=3)).isoformat()}, notify=True)
        self.notices.digest(self.notices.settings(), self.instant)
        pending = self.store.pending_events(PLATFORMS)
        self.assertEqual(len(pending), 2)
        self.assertTrue(any(r["event_key"] == f"offer:{offer_id}:2" for r in pending))

    def test_task_reminder_cancels_when_task_completed(self):
        offer_id, _ = self.store.save_offer({"platform": "welab", "title": "日期任务", "text": "条件待核", "url": "https://www.welab.bank/zh/test"})
        task = self.store.save_task(offer_id, {"stage": "registration", "title": "核对登记", "due_on": self.instant.astimezone(HK).date().isoformat(), "completed": False})
        self.notices.prepare(self.instant)
        self.notices.prepare(self.instant)
        self.assertEqual(self.count("task:"), 1)
        self.assertTrue(any(r["event_key"].startswith("task:") for r in self.store.pending_events(PLATFORMS)))
        self.store.save_task(offer_id, task | {"completed": True, "evidence": "本人确认已登记"})
        self.notices.prepare(self.instant)
        self.assertFalse(any(r["event_key"].startswith("task:") for r in self.store.pending_events(PLATFORMS)))


if __name__ == "__main__":
    unittest.main()
