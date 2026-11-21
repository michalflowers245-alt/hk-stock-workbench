from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from account_import import AccountData
from storage import Store


class AccountImportTests(unittest.TestCase):
    def setUp(self):
        working = TemporaryDirectory()
        self.addCleanup(working.cleanup)
        self.store = Store(Path(working.name))
        self.accounts = AccountData(self.store)

    def csv(self, kind, *lines):
        return self.accounts.template(kind) + '\r\n'.join(lines)

    def test_balance_preview_and_reimport_are_read_only_and_keep_precision(self):
        csv = self.csv("balances", "2026-01-02,港元账户,hkd,12.34000001,官方月结单",
                       "2026-01-02,美元账户,USD,9.99,官方月结单")
        preview = self.accounts.preview("za", "balances", csv)
        self.assertEqual(preview["new"], 2)
        self.assertEqual(self.accounts.list_data("za")["balances"], [])
        saved = self.accounts.import_csv("za", "balances", csv)
        self.assertEqual(saved["inserted"], 2)
        self.assertEqual(self.accounts.import_csv("za", "balances", csv)["existing"], 2)
        history = self.accounts.list_data("za")
        self.assertEqual(history["last_import_at"], saved["imported_at"])
        self.assertEqual({row["currency"]: row["amount"] for row in history["balances"]},
                         {"HKD": "12.34000001", "USD": "9.99"})
        self.assertEqual(self.accounts.list_data("welab")["balances"], [])
        self.assertEqual(self.store.ledger()["entries"], [])

    def test_identical_amounts_with_distinct_transaction_references_are_not_merged(self):
        csv = self.csv("transactions", "2026-01-02,TX01,退款,HKD,100,官方月结单",
                       "2026-01-02,TX02,退款,HKD,100,官方月结单",
                       "2026-01-02,TX02,退款,HKD,+100.00,交易记录页")
        self.assertEqual(self.accounts.import_csv("welab", "transactions", csv)["inserted"], 2)
        self.assertEqual({row["reference"] for row in self.accounts.list_data("welab")["transactions"]},
                         {"TX01", "TX02"})
        conflict = self.csv("transactions", "2026-01-02,TX03,退款,HKD,40,官方月结单",
                            "2026-01-02,TX01,退款,HKD,20,官方月结单")
        for action in (self.accounts.preview, self.accounts.import_csv):
            with self.assertRaisesRegex(ValueError, "流水编号已对应不同内容"):
                action("welab", "transactions", conflict)
        self.assertEqual(len(self.accounts.list_data("welab")["transactions"]), 2)

    def test_positions_are_dated_observations_and_accept_zero_quantity(self):
        csv = self.csv("positions", "2026-01-01,00700,腾讯,HKD,100,39800,本人交易结单",
                       "2026-01-02,00700,腾讯,HKD,0,,本人交易结单")
        self.accounts.import_csv("eddid", "positions", csv)
        rows = self.accounts.list_data("eddid")["positions"]
        self.assertEqual([(row["observed_on"], row["symbol"], row["quantity"]) for row in rows],
                         [("2026-01-02", "00700", "0"), ("2026-01-01", "00700", "100")])
        self.assertIsNone(rows[0]["market_value"])

    def test_record_can_be_corrected_after_scope_checked_removal(self):
        original = self.csv("transactions", "2026-01-02,TX01,退款,HKD,100,官方月结单")
        corrected = self.csv("transactions", "2026-01-02,TX01,退款,HKD,95,官方月结单")
        self.accounts.import_csv("welab", "transactions", original)
        record = self.accounts.list_data("welab")["transactions"][0]
        self.assertFalse(self.accounts.remove_record("za", "transactions", record["id"]))
        self.assertFalse(self.accounts.remove_record("welab", "balances", record["id"]))
        with self.assertRaisesRegex(ValueError, "流水编号已对应不同内容"):
            self.accounts.import_csv("welab", "transactions", corrected)
        self.assertTrue(self.accounts.remove_record("welab", "transactions", record["id"]))
        self.assertFalse(self.accounts.remove_record("welab", "transactions", record["id"]))
        self.assertEqual(self.accounts.import_csv("welab", "transactions", corrected)["inserted"], 1)
        row = self.accounts.list_data("welab")["transactions"][0]
        self.assertNotEqual(row["id"], record["id"])
        self.assertEqual((row["reference"], row["amount"]), ("TX01", "95"))

    def test_invalid_second_row_rejects_batch_without_any_write(self):
        csv = self.csv("balances", "2026-01-01,港元账户,HKD,20,官方月结单",
                       "2026-01-02,美元账户,USD,NaN,官方月结单")
        with self.assertRaisesRegex(ValueError, "第 3 行"):
            self.accounts.import_csv("eddid", "balances", csv)
        self.assertEqual(self.accounts.list_data("eddid")["balances"], [])
        with self.assertRaisesRegex(ValueError, "CSV 表头"):
            self.accounts.preview("eddid", "balances", "用户名,密码\nexample,password")
        with self.assertRaisesRegex(ValueError, "请选择众安银行"):
            self.accounts.list_data("fafa")


if __name__ == "__main__":
    unittest.main()
