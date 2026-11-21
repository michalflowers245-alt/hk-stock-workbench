from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from concurrent.futures import ThreadPoolExecutor

from collectors import PLATFORMS
from storage import Store, SCHEMA, HK


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(self.folder.name)
        self.day = datetime.now(HK).date().isoformat()
        self.offer = {"platform": "welab", "title": "新客存款奖励", "url": "https://www.welab.bank/zh/promotion", "text": "官方条款第一版", "deadline": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()}
        self.offer_id, _ = self.store.save_offer(self.offer)

    def test_changed_terms_keep_review_and_override_but_suspend_deadlines(self):
        self.store.patch_offer(self.offer_id, {"review_state": "eligible", "reviewed_version": 1, "followed": True, "review": {"note": "本人证据，不能丢失"}, "deadline": self.offer["deadline"]})
        self.store.add_deadline_notices(PLATFORMS)
        self.assertEqual(len(self.store.pending_events(PLATFORMS)), 1)
        self.store.save_offer(self.offer | {"text": "官方条款第二版"})
        detail = self.store.offers(PLATFORMS, offer_id=self.offer_id, detail=True)
        self.assertEqual(detail["review"]["note"], "本人证据，不能丢失")
        self.assertTrue(detail["review_stale"])
        self.assertIsNotNone(detail["deadline_override"])
        self.assertEqual(self.store.pending_events(PLATFORMS), [])
        self.assertTrue(any(h["review"].get("note") for h in detail["review_history"]))
        self.store.patch_offer(self.offer_id, {"followed": True})
        self.assertTrue(self.store.offers(PLATFORMS, offer_id=self.offer_id)["review_stale"])
        for stale in ({"review_state": "eligible"}, {"review_state": "eligible", "reviewed_version": 1}):
            with self.assertRaises(ValueError):
                self.store.patch_offer(self.offer_id, stale)
        self.store.patch_offer(self.offer_id, {"review_state": "eligible", "reviewed_version": 2})
        self.store.add_deadline_notices(PLATFORMS)
        self.assertEqual(len(self.store.pending_events(PLATFORMS)), 1)

    def test_expected_rewards_are_immutable_partial_and_idempotent(self):
        initial = {"offer_id": self.offer_id, "kind": "pending_cash", "amount": 100, "currency": "USD", "rate_hkd": 7.8, "occurred_on": self.day, "expected_on": "2020-01-01", "idempotency_key": "expected-0001"}
        expected_id = self.store.add_entry(initial)
        self.assertEqual(self.store.add_entry(initial), expected_id)
        with self.assertRaises(ValueError):
            self.store.add_entry(initial | {"amount": 101})
        partial = {"kind": "cash", "amount": 40, "rate_hkd": 7.9, "occurred_on": self.day, "idempotency_key": "settlement-0001"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(lambda _: self.store.settle_entry(expected_id, partial), range(2))), [True, True])
        ledger = self.store.ledger()
        self.assertEqual(len(ledger["entries"]), 2)
        expected = next(e for e in ledger["entries"] if e["id"] == expected_id)
        self.assertEqual((expected["amount"], expected["settled_amount"], expected["remaining_amount"], expected["due_status"]), (100, 40, 60, "overdue"))
        self.assertEqual((ledger["totals"]["cash"], ledger["totals"]["pending_cash"]), (316, 468))
        self.assertEqual(ledger["totals"]["overdue_pending_cash"], 468)
        with self.assertRaises(ValueError):
            self.store.settle_entry(expected_id, partial | {"amount": 61, "idempotency_key": "too-much-0001"})
        final = {"kind": "cash", "idempotency_key": "final-00000001", "occurred_on": self.day}
        self.store.settle_entry(expected_id, final)
        self.store.settle_entry(expected_id, final)
        self.assertEqual(self.store.ledger()["totals"]["pending_cash"], 0)
        with self.assertRaises(ValueError):
            self.store.delete_entry(expected_id)
        settlement_id = next(e["id"] for e in ledger["entries"] if e["expected_entry_id"])
        self.store.delete_entry(settlement_id)
        self.assertEqual(self.store.ledger()["totals"]["pending_cash"], 312)

    def test_currency_values_vouchers_and_losses_are_separate(self):
        base = {"offer_id": self.offer_id, "occurred_on": self.day}
        with self.assertRaises(ValueError):
            self.store.add_entry(base | {"kind": "cash", "amount": 20, "currency": "USD"})
        self.store.add_entry(base | {"kind": "cash", "amount": 100})
        self.store.add_entry(base | {"kind": "fees", "amount": 5})
        self.store.add_entry(base | {"kind": "trading_pnl", "amount": -20})
        self.store.add_entry(base | {"kind": "fx_pnl", "amount": -3})
        self.store.add_entry(base | {"kind": "voucher", "amount": 50, "redeemed_value": 20, "voucher_scope": "仅抵佣金"})
        totals = self.store.ledger()["totals"]
        self.assertEqual((totals["net"], totals["combined_net"], totals["voucher"], totals["redeemed_voucher"]), (95, 72, 50, 20))

    def test_one_offer_cash_economics_and_guarded_simple_annualization(self):
        ledger = {"offer_id": self.offer_id, "occurred_on": self.day}
        for kind, amount in (("cash", 100), ("fees", 20), ("voucher", 500), ("trading_pnl", 999), ("fx_pnl", 888)):
            self.store.add_entry(ledger | {"kind": kind, "amount": amount})
        first = self.store.save_reservation({"offer_id": self.offer_id, "amount": 1000, "starts_on": "2020-01-01", "ends_on": "2020-01-10", "status": "released"})
        second = self.store.save_reservation({"offer_id": self.offer_id, "amount": 500, "starts_on": "2020-01-06", "ends_on": "2020-01-15", "status": "released"})
        self.store.save_retrospective(self.offer_id, {"actual_minutes": 45})
        econ = self.store.offer_economics(self.offer_id)
        self.assertEqual((econ["net"], econ["voucher"], econ["capital_peak_hkd"], econ["capital_days_hkd"]), (80, 500, 1500, 15000))
        self.assertEqual(econ["simple_annualized_percent"], 194.67)
        self.assertEqual(econ["actual_minutes"], 45)
        self.assertIn("不代表可重复收益", econ["annualized_note"])
        self.assertEqual(self.store.offers(PLATFORMS, offer_id=self.offer_id, detail=True)["economics"], econ)
        pending = self.store.add_entry(ledger | {"kind": "pending_voucher", "amount": 1})
        self.assertIsNone(self.store.offer_economics(self.offer_id)["simple_annualized_percent"])
        self.store.delete_entry(pending)
        self.store.save_reservation(second | {"status": "active"})
        self.assertIsNone(self.store.offer_economics(self.offer_id)["simple_annualized_percent"])
        self.store.save_reservation(second | {"starts_on": "2099-01-01", "ends_on": "2099-01-10", "status": "released"})
        future = self.store.offer_economics(self.offer_id)
        self.assertIsNone(future["simple_annualized_percent"])
        self.assertIn("未来结束日", future["annualized_note"])
        with self.store.connect(write=True) as db:
            zero = {"amount": 0, "amount_hkd": 0, "currency": "HKD", "rate_hkd": 1, "starts_on": "2020-01-01", "ends_on": "2020-01-10", "status": "released"}
            db.execute("UPDATE capital_reservations SET payload=?", (json.dumps(zero),))
        self.assertIsNone(self.store.offer_economics(self.offer_id)["simple_annualized_percent"])
        with self.store.connect(write=True) as db:
            broken = zero | {"amount": 100, "amount_hkd": 100}
            broken.pop("rate_hkd")
            db.execute("UPDATE capital_reservations SET payload=?", (json.dumps(broken),))
        self.assertIsNone(self.store.offer_economics(self.offer_id)["simple_annualized_percent"])
        self.assertIsNone(self.store.offer_economics(self.offer_id)["capital_days_hkd"])

    def test_received_voucher_redemption_updates_cumulative_value_with_audit(self):
        entry = {"offer_id": self.offer_id, "occurred_on": self.day, "amount": 100, "currency": "USD", "rate_hkd": 7.8}
        voucher_id = self.store.add_entry(entry | {"kind": "voucher", "voucher_scope": "抵佣金"})
        cash_id = self.store.add_entry(entry | {"kind": "cash"})
        for item_id, value in ((voucher_id, -1), (voucher_id, 100.01), (cash_id, 10)):
            with self.assertRaises(ValueError):
                self.store.redeem_voucher(item_id, {"redeemed_value": value})
        first = self.store.redeem_voucher(voucher_id, {"redeemed_value": 20, "note": "首次抵扣20美元"})
        self.assertEqual((first["amount"], first["redeemed_value"]), (100, 20))
        self.store.redeem_voucher(voucher_id, {"redeemed_value": 20, "note": "网络重提"})
        second = self.store.redeem_voucher(voucher_id, {"redeemed_value": 45, "voucher_scope": "累计抵扣佣金", "note": "第二次抵扣25美元"})
        self.assertEqual([(h["old_value"], h["new_value"]) for h in second["voucher_redemptions"]], [(0, 20), (20, 45)])
        self.assertEqual(second["voucher_scope"], "累计抵扣佣金")
        totals = self.store.ledger()["totals"]
        self.assertEqual((totals["voucher"], totals["redeemed_voucher"], totals["cash"]), (780, 351, 780))
        self.store.delete_entry(voucher_id)
        with self.assertRaises(ValueError):
            self.store.redeem_voucher(voucher_id, {"redeemed_value": 50})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voucher_redemptions WHERE ledger_id=?", (voucher_id,)).fetchone()[0], 2)

    def test_migration_takes_restorable_backup(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "workbench.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO ledger(kind,cents,note,occurred_on) VALUES ('cash',1234,'真实旧记录','2020-01-01')")
            upgraded = Store(folder)
            self.assertEqual(upgraded.ledger()["totals"]["cash"], 12.34)
            backups = list((Path(folder) / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as old:
                self.assertNotIn("currency", [r[1] for r in old.execute("PRAGMA table_info(ledger)")])
                self.assertEqual(old.execute("SELECT cents,note FROM ledger").fetchone(), (1234, "真实旧记录"))
            Store(folder)
            self.assertEqual(len(list((Path(folder) / "backups").glob("*.sqlite3"))), 1)

    def test_eligibility_unknown_satisfied_unmet_and_stale(self):
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "unknown")
        requirements = {"reviewed": True, "source_url": self.offer["url"], "eligibility": {"new_customer": True, "invitation_required": True}, "capital_hkd": 1000}
        with self.assertRaises(ValueError):
            self.store.save_requirements(self.offer_id, requirements | {"source_url": "https://evil.test"})
        self.store.save_requirements(self.offer_id, requirements)
        self.store.save_profile("welab", {"status": "none", "prior_owner": False, "invited": True, "available_capital": 5000})
        self.store.save_budget({"amount": 1000})
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "satisfied")
        self.store.save_profile("welab", {"invited": False})
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "unmet")
        self.store.save_offer(self.offer | {"text": "更新版"})
        self.assertTrue(self.store.requirements(self.offer_id)["stale"])
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "unknown")

    def test_unknown_budget_and_stale_requirements_cannot_match(self):
        base = {"reviewed": True, "reviewed_version": 1, "source_url": self.offer["url"], "capital_hkd": 0}
        self.store.save_requirements(self.offer_id, base)
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "unknown")
        self.store.save_budget({"amount": 0})
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "satisfied")
        self.store.save_requirements(self.offer_id, base | {"capital_hkd": None})
        self.assertEqual(self.store.assess_offer(self.offer_id)["state"], "unknown")
        self.store.save_offer(self.offer | {"text": "新条款"})
        with self.assertRaises(ValueError):
            self.store.save_requirements(self.offer_id, base)
        self.store.save_requirements(self.offer_id, base | {"reviewed_version": 2})
        self.assertFalse(self.store.requirements(self.offer_id)["stale"])

    def test_performance_refuses_hourly_rate_when_time_is_missing(self):
        other_id, _ = self.store.save_offer(self.offer | {"url": self.offer["url"] + "-other"})
        for offer_id in (self.offer_id, other_id):
            self.store.add_entry({"offer_id": offer_id, "kind": "cash", "amount": 100, "occurred_on": self.day})
        self.store.save_retrospective(self.offer_id, {"actual_minutes": 60})
        first = next(p for p in self.store.performance() if p["platform"] == "welab")
        self.assertEqual((first["income_offer_count"], first["timed_income_offer_count"]), (2, 1))
        self.assertFalse(first["time_coverage_complete"])
        self.assertIsNone(first["cash_per_hour"])
        self.store.save_retrospective(other_id, {"actual_minutes": 60})
        second = next(p for p in self.store.performance() if p["platform"] == "welab")
        self.assertTrue(second["time_coverage_complete"])
        self.assertEqual(second["cash_per_hour"], 100)

    def test_task_prerequisites_evidence_and_global_listing(self):
        registration = self.store.save_task(self.offer_id, {"stage": "registration", "due_on": self.day})
        deposit = {"stage": "deposit", "due_on": self.day, "prerequisite_ids": [registration["id"]]}
        with self.assertRaises(ValueError):
            self.store.save_task(self.offer_id, deposit | {"completed": True, "evidence": "入金记录"})
        with self.assertRaises(ValueError):
            self.store.save_task(self.offer_id, registration | {"completed": True})
        self.store.save_task(self.offer_id, registration | {"completed": True, "evidence": "实际报名确认"})
        self.store.save_task(self.offer_id, deposit | {"completed": True, "evidence": "已收到入金记录"})
        self.assertEqual(len(self.store.tasks()), 2)
        self.assertFalse(self.store.tasks()[0]["overdue"])
        self.assertEqual(self.store.tasks()[0]["offer_title"], self.offer["title"])
        with self.assertRaises(ValueError):
            self.store.save_task(self.offer_id, registration | {"completed": False})

    def test_budget_not_sum_of_accounts_and_exact_overlap(self):
        self.store.save_profile("welab", {"available_capital": 9999})
        self.store.save_profile("za", {"available_capital": 9999})
        self.store.save_budget({"amount": 1000})
        self.store.save_reservation({"offer_id": self.offer_id, "amount": 600, "starts_on": "2027-01-01", "ends_on": "2027-01-10"})
        second = self.store.save_reservation({"amount": 500, "starts_on": "2027-01-05", "ends_on": "2027-01-20"})
        overview = self.store.reservations()
        self.assertEqual(overview["budget"]["available_hkd"], 1000)
        self.assertEqual(len(overview["conflicts"]), 1)
        overlap = overview["conflicts"][0]
        self.assertEqual((overlap["starts_on"], overlap["ends_on"], overlap["shortfall_hkd"]), ("2027-01-05", "2027-01-10", 100))
        self.store.save_reservation(second | {"status": "released"})
        self.assertEqual(self.store.reservations()["conflicts"], [])

    def test_retrospective_and_ipo_cutoff_kept_separate_from_hkex(self):
        self.store.save_retrospective(self.offer_id, {"actual_minutes": 30, "problems": "私人信息", "shareable_notes": "实际步骤", "shareable_approved": True})
        self.assertTrue(self.store.retrospective(self.offer_id)["shareable_approved"])
        self.store.add_entry({"offer_id": self.offer_id, "kind": "cash", "amount": 20, "occurred_on": self.day})
        performance = next(p for p in self.store.performance() if p["platform"] == "welab")
        self.assertEqual(performance["cash_per_hour"], 40)
        self.store.save_ipos([{"key": "test-ipo", "name": "测试新股", "deadline": self.offer["deadline"]}])
        self.store.save_ipo_cutoff("test-ipo", {"platform": "phillip", "deadline": self.offer["deadline"], "source_url": "https://www.poems.com.hk/zh-hk/", "verified": True})
        ipo = self.store.ipos()[0]
        self.assertEqual(ipo["identity"], "test-ipo")
        self.assertEqual(ipo["deadline"], ipo["broker_cutoffs"][0]["deadline"])
        self.assertTrue(ipo["broker_cutoffs"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
