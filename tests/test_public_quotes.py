from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from public_quotes import collect_quotes, parse_quotes, quote_page_url
from storage import Store


def sample(code="00700", stamp="2026/11/17 16:08:19", price="376.000"):
    row = ["0"] * 40
    for index, value in {0: "100", 1: "腾讯控股", 2: code, 3: price, 4: "390.400",
                         5: "386.600", 30: stamp, 33: "386.600", 34: "376.000",
                         36: "31401095.0", 37: "11892584512.650"}.items():
        row[index] = value
    return 'v_hk' + code + '="' + "~".join(row) + '";'


class PublicQuoteTests(unittest.TestCase):
    instant = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
    watch = [{"symbol": "HK.00700", "name": "腾讯控股"}]

    def test_public_snapshot_has_actual_source_time_and_never_claims_live(self):
        result = parse_quotes(sample(), self.watch, self.instant)
        quote = result["quotes"][0]
        self.assertEqual(quote["quote_at"], "2026-11-17T08:08:19+00:00")
        self.assertEqual(quote["captured_at"], "2026-11-17T13:00:00+00:00")
        self.assertEqual(quote["status"], "public_snapshot")
        self.assertEqual(quote["last_price"], 376)
        self.assertEqual(quote["volume"], 31401095)
        self.assertEqual(quote["source_url"], "https://gu.qq.com/hk00700/gp")
        self.assertEqual(quote_page_url("HK.800700"), "https://gu.qq.com/hkHSTECH/zs")

    def test_missing_future_time_and_inconsistent_price_are_rejected(self):
        for content in (sample(stamp=""), sample(stamp="2026/11/18 10:00:00"), sample(price="NaN"), sample(price="999")):
            result = parse_quotes(content, self.watch, self.instant)
            self.assertEqual(result["quotes"], [])
            self.assertFalse(result["checks"][0]["ok"])
        missing = parse_quotes(sample(code="09988"), self.watch, self.instant)
        self.assertEqual(missing["quotes"], [])

    def test_index_turnover_uses_source_scale_and_no_invented_share_volume(self):
        result = parse_quotes(sample(code="HSI"), [{"symbol": "HK.800000"}], self.instant)
        self.assertIsNone(result["quotes"][0]["volume"])
        self.assertEqual(result["quotes"][0]["turnover"], 11892584512.650 * 10000)

    def test_source_denial_or_network_failure_returns_specific_checks(self):
        with patch("public_quotes.fetch", return_value=b"User-agent: *\nDisallow: /\n") as fetch:
            result = collect_quotes(self.watch)
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(result["checks"][0]["ok"])
        with patch("public_quotes.fetch", side_effect=OSError("network")):
            result = collect_quotes(self.watch)
            self.assertEqual(result["quotes"], [])
            self.assertIn("OSError", result["detail"])

    def test_repeated_source_timestamp_keeps_one_history_point_and_latest_capture(self):
        with TemporaryDirectory() as folder:
            store = Store(Path(folder))
            quote = parse_quotes(sample(), self.watch, self.instant)["quotes"][0]
            store.save_quote(quote)
            again = quote | {"captured_at": (self.instant + timedelta(minutes=15)).isoformat(timespec="seconds")}
            store.save_quote(again)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM quote_history").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT captured_at FROM quote_latest").fetchone()[0], again["captured_at"])
                value = db.execute("SELECT data FROM quote_history").fetchone()[0]
                self.assertIn("source_url", value)
            later = quote | {"quote_at": (self.instant + timedelta(minutes=16)).isoformat(timespec="seconds"),
                             "captured_at": (self.instant + timedelta(minutes=16)).isoformat(timespec="seconds")}
            store.save_quote(later)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM quote_history").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
