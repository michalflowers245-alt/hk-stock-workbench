from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from collectors import PLATFORMS
from services import flush_mail, generate_tutorial, run_mail_cycle
from storage import Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name))
        self.future = datetime.now(timezone.utc) + timedelta(days=2)
        self.official = {
            "platform": "welab", "title": "迎新活动", "url": "https://www.welab.bank/zh/feature/welcome",
            "text": "合資格新客戶須開立戶口，存入 HKD 1,000，並維持結餘 21 天。",
            "facts": {"capital": "存入 HKD 1,000", "holding": "維持結餘 21 天"},
            "deadline": self.future.isoformat(),
        }

    def get_offer(self, offer_id):
        return self.store.offers(PLATFORMS, offer_id=offer_id, detail=True)

    def test_one_change_email_for_same_link_no_mail_for_unchanged_source(self):
        offer_id, state = self.store.save_offer(self.official)
        self.assertEqual(state, "new")
        self.assertEqual(self.store.pending_events(PLATFORMS), [])
        self.store.patch_offer(offer_id, {"review_state": "eligible", "followed": True})
        new_terms = self.official | {"deadline": (self.future + timedelta(days=1)).isoformat()}
        self.assertEqual(self.store.save_offer(new_terms, notify=True), (offer_id, "changed"))
        self.assertEqual(self.store.save_offer(new_terms, notify=True), (offer_id, "unchanged"))
        item = self.get_offer(offer_id)
        self.assertEqual(len(item["history"]), 2)
        self.assertEqual(item["review_state"], "pending")
        self.assertTrue(item["followed"])
        queued = self.store.pending_events(PLATFORMS)
        self.assertEqual(len(queued), 1)
        self.assertIn("活动条款有变动", queued[0]["subject"])
        with patch("services.deliver_email") as send:
            flush_mail(self.store, {"mock": True})
            flush_mail(self.store, {"mock": True})
        send.assert_called_once()

    def test_missing_pdf_does_not_replace_verified_terms(self):
        original = self.official | {"terms_urls": ["https://www.welab.bank/terms.pdf"],
                                    "terms_hashes": ["first"], "terms_complete": True}
        offer_id, _ = self.store.save_offer(original)
        partial = original | {"terms_hashes": [], "terms_complete": False, "text": "短页面"}
        self.assertEqual(self.store.save_offer(partial, notify=True), (offer_id, "unchanged"))
        self.assertEqual(len(self.get_offer(offer_id)["history"]), 1)
        self.assertEqual(self.store.pending_events(PLATFORMS), [])
        complete = original | {"terms_hashes": ["second"]}
        self.assertEqual(self.store.save_offer(complete, notify=True), (offer_id, "changed"))

    def test_deadline_alert_requires_exact_time_and_is_unique(self):
        offer_id, _ = self.store.save_offer(self.official | {"deadline": None, "ends_on": date.today().isoformat()})
        self.store.patch_offer(offer_id, {"followed": True})
        self.store.add_deadline_notices(PLATFORMS)
        self.assertEqual(self.store.pending_events(PLATFORMS), [])
        near = datetime.now(timezone.utc) + timedelta(hours=23)
        self.store.patch_offer(offer_id, {"deadline": near.isoformat()})
        self.store.add_deadline_notices(PLATFORMS)
        self.store.add_deadline_notices(PLATFORMS)
        self.assertEqual(len(self.store.pending_events(PLATFORMS)), 1)
        self.store.patch_offer(offer_id, {"followed": False})
        self.assertEqual(self.store.pending_events(PLATFORMS), [])

    def test_tutorial_and_ledger_keep_private_notes_and_pending_cash_separate(self):
        offer_id, _ = self.store.save_offer(self.official)
        self.store.patch_offer(offer_id, {"review": {"note": "私人复核备注", "capital": "本人复核金额"}})
        item = self.get_offer(offer_id)
        self.assertIn("私人复核备注", generate_tutorial(self.store, item, "personal")["text"])
        public = generate_tutorial(self.store, item, "public")["text"]
        self.assertIn("HKD 1,000", public)
        self.assertNotIn("本人复核金额", public)
        day = date.today().isoformat()
        draft = {"offer_id": offer_id, "occurred_on": day}
        cash_id = self.store.add_entry(draft | {"kind": "pending_cash", "amount": "50.00"})
        self.store.add_entry(draft | {"kind": "voucher", "amount": "20.00"})
        self.store.add_entry(draft | {"kind": "fees", "amount": "3.25"})
        self.assertTrue(self.store.settle_entry(cash_id, {"kind": "cash", "occurred_on": day}))
        totals = self.store.ledger()["totals"]
        self.assertEqual({key: totals[key] for key in ("cash", "fees", "net", "voucher", "pending_cash", "pending_voucher")},
                         {"cash": 50, "fees": 3.25, "net": 46.75, "voucher": 20,
                          "pending_cash": 0, "pending_voucher": 0})
        original = next(entry for entry in self.store.ledger()["entries"] if entry["id"] == cash_id)
        self.assertEqual((original["kind"], original["amount"], original["remaining_amount"]), ("pending_cash", 50, 0))
        with self.assertRaises(ValueError):
            self.store.settle_entry(cash_id, {"kind": "cash"})

    def test_history_retains_quote_time_capture_time_and_restart(self):
        first = datetime.now(timezone.utc).replace(second=10, microsecond=0)
        record = {"symbol": "HK.00700", "source": "富途 OpenAPI", "status": "live", "last_price": 80,
                  "prev_close": 78, "quote_at": first.isoformat(), "captured_at": first.isoformat()}
        self.store.save_quote(record)
        self.store.save_quote(record)
        newer = record | {"captured_at": (first + timedelta(minutes=1)).isoformat()}
        self.store.save_quote(newer)
        rebuilt = Store(self.store.root)
        history = rebuilt.history("00700")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["quote_at"], first.isoformat())
        self.assertEqual(history[-1]["captured_at"], newer["captured_at"])

    def test_failed_source_keeps_last_success(self):
        url = PLATFORMS["za"]["url"]
        self.store.record_checks("za", [{"url": url, "ok": True, "coverage": "public"}])
        self.store.record_checks("za", [{"url": url, "ok": False, "error": "TLS"}])
        result = self.store.source_status(PLATFORMS)[0]
        self.assertEqual(result["error"], "TLS")
        self.assertIsNotNone(result["last_success"])

    def test_futu_corruption_cannot_block_email(self):
        reader = MagicMock()
        reader.reauthorization_due.side_effect = RuntimeError("Unusable token")
        with patch("services.flush_mail") as call:
            run_mail_cycle(self.store, {"mock": True}, reader)
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
