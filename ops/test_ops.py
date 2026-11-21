"""隔离临时目录测试；不读写业务库、不发送邮件、不连接云账户。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import acceptance
import backup
import preflight


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        with sqlite3.connect(self.data / "workbench.sqlite3") as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE ledger(id INTEGER PRIMARY KEY, cents INTEGER);
                INSERT INTO ledger VALUES(1,12345);
                CREATE TABLE quote_history(symbol TEXT,minute TEXT,quote_at TEXT,captured_at TEXT,data TEXT);
                CREATE TABLE service_state(name TEXT,updated_at TEXT);
                CREATE TABLE source_checks(scope TEXT,url TEXT,ok INTEGER,last_success TEXT,error TEXT);
            """)
        (self.data / "futu-private").mkdir()
        (self.data / "futu-private/key").write_text("test-only-secret")

    def tearDown(self):
        self.temporary.cleanup()

    def test_online_backup_and_restore_excludes_credentials(self):
        result = backup.create_backup(self.data, self.root / "backups")
        target = self.root / "restored"
        restored = backup.restore_drill(Path(result["path"]), target)
        self.assertEqual(restored["tables"]["ledger"], 1)
        self.assertEqual([p.name for p in target.iterdir()], ["workbench.sqlite3"])
        self.assertEqual((target / "workbench.sqlite3").stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(target / "workbench.sqlite3") as db:
            self.assertEqual(db.execute("SELECT cents FROM ledger").fetchone()[0], 12345)

    def test_restore_rejects_tampered_backup_and_nonempty_target(self):
        result = backup.create_backup(self.data, self.root / "backups")
        path = Path(result["path"])
        with self.assertRaises(ValueError):
            backup.restore_drill(path, self.data)
        with path.open("ab") as dst:
            dst.write(b"tampered")
        with self.assertRaises(ValueError):
            backup.restore_drill(path, self.root / "restore")

    def test_backup_sees_committed_wal_but_not_uncommitted_rows(self):
        writer = sqlite3.connect(self.data / "workbench.sqlite3")
        try:
            writer.execute("INSERT INTO ledger VALUES(2,500)")
            writer.commit()
            writer.execute("INSERT INTO ledger VALUES(3,999)")
            result = backup.create_backup(self.data, self.root / "online-backup")
            self.assertEqual(result["tables"]["ledger"], 2)
            backup.restore_drill(Path(result["path"]), self.root / "online-restored")
        finally:
            writer.rollback()
            writer.close()

    def test_retention_only_deletes_own_completed_pairs(self):
        folder = self.root / "backups"
        backup.create_backup(self.data, folder, retain=1)
        unrelated = folder / "manual.sqlite3"
        unrelated.write_text("retain")
        backup.create_backup(self.data, folder, retain=1)
        self.assertEqual(len(list(folder.glob("hk-workbench-*.sqlite3"))), 1)
        self.assertTrue(unrelated.exists())

    def test_free_guard_rejects_unverified_paid_or_overquota(self):
        record = {"account_type": "free", "always_free_labels_verified": True, "home_region_verified": True,
                  "console_estimated_monthly_cost": 0, "other_paid_resources_absent": True,
                  "a1_total_ocpus": 1, "a1_total_memory_gb": 4, "e2_micro_instance_count": 0,
                  "total_boot_and_block_volume_gb": 50, "verified_at": datetime.now(timezone.utc).isoformat(),
                  "evidence_note": "隔离测试输入，不能用于真实免费证明"}
        self.assertEqual(preflight.free_check(record), [])
        self.assertTrue(preflight.free_check(record | {"account_type": "pay_as_you_go"}))
        self.assertTrue(preflight.free_check(record | {"a1_total_ocpus": 4}))
        self.assertTrue(preflight.free_check(record | {"a1_total_memory_gb": 24}))
        self.assertTrue(preflight.free_check({}))

    def test_short_empty_observation_never_passes(self):
        acceptance.start(self.data)
        with self.assertRaises(ValueError):
            acceptance.start(self.data)
        report = acceptance.report(self.data, None, [], "HK.00700", None)
        self.assertEqual(report["结论"], "验收未完成")
        self.assertFalse(report["自动检查"]["观测不少于24小时"])
        self.assertFalse(report["自动检查"]["三个指数及股票的指定交易时段记录完整"])
        self.assertTrue(all(value["状态"] == "待实测" for value in report["人工实测"].values()))

    def test_quote_gaps_and_delayed_records_are_visible(self):
        acceptance.start(self.data)
        run, info = acceptance.locate(self.data)
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=25)
        info["started_at"] = start.isoformat()
        (run / "run.json").write_text(json.dumps(info))
        with sqlite3.connect(self.data / "workbench.sqlite3") as db:
            for symbol in acceptance.INDICES + ("HK.00700",):
                when = start.isoformat(timespec="seconds")
                db.execute("INSERT INTO quote_history VALUES(?,?,?,?,?)", (symbol, when, when, when, '{"status":"delayed"}'))
        end = start + timedelta(minutes=2)
        result = acceptance.report(self.data, run, [start.isoformat()+"/"+end.isoformat()], "HK.00700", None)
        self.assertEqual(result["行情"][0]["live_minutes"], 0)
        self.assertEqual(len(result["行情"][0]["missing_minutes"]), 2)
        self.assertFalse(result["自动检查"]["探针无超过3分钟空档"])


if __name__ == "__main__":
    unittest.main()
