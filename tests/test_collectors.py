import unittest
from datetime import date
from unittest.mock import patch

import collectors


class PublicCollectorTests(unittest.TestCase):
    def test_public_source_rejects_foreign_host(self):
        source = collectors.PLATFORMS["welab"]["url"]
        hosts = collectors.PLATFORMS["welab"]["hosts"]
        self.assertEqual(collectors.official_url("//welab.bank.evil.test/zh", source, hosts), "")
        self.assertEqual(collectors.official_url("https://elsewhere@welab.bank/zh", source, hosts), "")
        self.assertEqual(collectors.official_url("https://www.welab.bank:9000/zh", source, hosts), "")
        self.assertEqual(
            collectors.official_url("feature/test/?utm_id=ad&edition=4#top", source, hosts),
            "https://www.welab.bank/zh/feature/test/?edition=4",
        )

    def test_robots_disallow_is_reported_as_failed_coverage(self):
        blocked = collectors.SourceError("来源规则禁止自动读取此页面")
        with patch.object(collectors, "get_html", side_effect=blocked), patch.object(collectors, "pdf_text", side_effect=blocked):
            result = collectors.collect_offers("za")
        self.assertEqual(result["offers"], [])
        self.assertEqual(len(result["checks"]), 5)
        self.assertTrue(all(not item["ok"] for item in result["checks"]))

    def test_za_public_documents_do_not_hide_main_site_failure(self):
        official_note = collectors.DocumentParser(
            "<p>除活動外，基金手續費優惠已於 2026 年 7 月 31 日終止。</p>"
            "<p>「股票最勁抽」推廣期為 2026 年 7 月 1 日至 2026 年 12 月 31 日。"
            "合資格客戶可獲指定股票兌換券。</p>"
        ).root

        def get_html(url, _hosts):
            if url == collectors.ZA_STOCK_PROMOTION_PAGE:
                return url, official_note
            raise collectors.SourceError("HTTPS 连接失败")

        def read_terms(url, _hosts):
            title = collectors.ZA_STUDENT_TERMS[url]
            return [f"ZA Bank Limited {title}活動條款及細則",
                    "1. 活動於 2026 年 10 月", "23 日開始至 2026 年 11 月 30 日結束（活動期）。",
                    "2. 參加者必須持有香港身份證。"], "hash"

        with patch.object(collectors, "get_html", side_effect=get_html), patch.object(collectors, "pdf_text", side_effect=read_terms):
            result = collectors.collect_offers("za")
        offers = {item["title"]: item for item in result["offers"]}
        self.assertEqual(len(offers), 3)
        self.assertEqual(offers["大學生開戶優惠"]["ends_on"], "2026-11-30")
        self.assertEqual(offers["大學生開戶優惠"]["source_kind"], "official_terms")
        self.assertEqual(offers["股票最勁抽"]["ends_on"], "2026-12-31")
        self.assertFalse(offers["股票最勁抽"]["terms_complete"])
        self.assertEqual([item["ok"] for item in result["checks"]], [False, False, True, True, True])

    def test_same_source_uses_stable_key_and_changes_revision_content(self):
        source = "https://www.welab.bank/zh/feature/rewards/"
        version_a = collectors.make_offer("welab", "迎新", source, source, [
            "推廣期：2026年11月1日 至 2026年11月25日",
            "合資格新客戶須存入港幣 50,000 元並持續 20 日。",
        ])
        version_b = collectors.make_offer("welab", "迎新", source, source, [
            "推廣期：2026年11月1日 至 2026年11月30日",
            "合資格新客戶須存入港幣 50,000 元並持續 20 日。",
        ])
        self.assertEqual(version_a["key"], version_b["key"])
        self.assertNotEqual(version_a["revision_text"], version_b["revision_text"])
        self.assertEqual(version_b["ends_on"], "2026-11-30")
        self.assertIsNone(version_b["deadline"])
        self.assertIn("50,000", version_b["facts"]["capital"])

    def test_promotion_range_picks_actual_end_time(self):
        values = collectors.periods(["開戶獎賞活動日期：2026年11月1日至2026年11月30日（星期一）下午11時59分。"])
        self.assertEqual(values["starts_at"], "2026-11-01")
        self.assertEqual(values["ends_on"], "2026-11-30")
        self.assertEqual(values["deadline"], "2026-11-30T23:59:00+08:00")

    def test_missing_linked_terms_can_be_retried_without_claiming_full_coverage(self):
        config = collectors.PLATFORMS["welab"]
        self.enterContext(patch.dict(config, {"campaigns": {}}))
        base = config["url"]
        detail = base + "feature/example-rewards/"
        terms = "https://www.welab.bank/uploads/current-terms.pdf"
        public_index = collectors.DocumentParser(f'<a href="{detail}">新客戶迎新禮</a>').root
        public_detail = collectors.DocumentParser(
            f'<main><h1>新客戶迎新禮</h1><p>推廣期：2026年11月1日至2026年11月30日。'
            f'公開活動頁介紹新客戶迎新優惠。</p><a href="{terms}">完整條款</a></main>'
        ).root
        with patch.object(collectors, "get_html", side_effect=((base, public_index), (detail, public_detail))):
            with patch.object(collectors, "pdf_text", side_effect=collectors.SourceError("HTTP 503")):
                missing = collectors.collect_offers("welab")
        self.assertFalse(missing["offers"][0]["terms_complete"])
        self.assertIn((terms, False), {(check["url"], check["ok"]) for check in missing["checks"]})

        with patch.object(collectors, "get_html", side_effect=((base, public_index), (detail, public_detail))):
            with patch.object(collectors, "pdf_text", return_value=(["新客戶須存款。"], "digest")):
                recovered = collectors.collect_offers("welab")
        self.assertTrue(recovered["offers"][0]["terms_complete"])
        self.assertIn((terms, True), {(check["url"], check["ok"]) for check in recovered["checks"]})

    def test_welab_direct_targeted_offer_survives_index_failure_and_uses_referrer_terms(self):
        config = collectors.PLATFORMS["welab"]
        url = next(link for link in config["campaigns"] if "targeted-mgm-program-" in link)
        general_terms = "https://www.welab.bank/uploads/mgm-ntb-campaign-tnc-oct26.pdf"
        targeted_terms = "https://www.welab.bank/uploads/targeted-mgm-program-tnc-oct26.pdf"
        targeted_page = collectors.DocumentParser(
            "<main><h1>特選客戶友賞齊齊分</h1>"
            "<p>推廣期：2026年10月15日至2026年11月30日。所有參加者須對照正式條款確認所適用的推廣安排及獎賞。</p>"
            f'<a href="{general_terms}">新客戶條款</a><a href="{targeted_terms}">特選推薦人條款</a></main>'
        ).root
        self.enterContext(patch.dict(config, {"campaigns": {url: "特選客戶友賞齊齊分"}}))
        self.enterContext(patch.object(collectors, "get_html", side_effect=(collectors.SourceError("HTTP 503"), (url, targeted_page))))
        fetch_terms = self.enterContext(patch.object(collectors, "pdf_text", side_effect=(
            (["特選推薦人須收到銀行邀請。"], "targeted"), (["新客戶須開戶。"], "general"),
        )))
        result = collectors.collect_offers("welab")
        self.assertEqual(result["offers"][0]["url"], url)
        self.assertIn("特選推薦人須收到銀行邀請", result["offers"][0]["text"])
        self.assertNotIn("新客戶須開戶", result["offers"][0]["text"])
        self.assertEqual(fetch_terms.call_args_list[0].args[0], targeted_terms)
        self.assertIn((config["url"], False), {(item["url"], item["ok"]) for item in result["checks"]})

    def test_two_fafa_offers_share_the_same_public_url(self):
        root = collectors.DocumentParser(
            "<article><h2>超低費率，免費行情</h2><p>經邀請開戶可享優惠費率、每月驗資，送免費行情</p></article>"
            "<article><h2>新股申購優惠</h2><p>未中籤可獲申購手續費全額返還</p></article>"
        ).root
        result = collectors.fafa_offers(root, collectors.PLATFORMS["fafa"]["url"])
        self.assertEqual({item["key"] for item in result}, {"fafa:invitation", "fafa:ipo-rebate"})
        self.assertEqual(len({item["url"] for item in result}), 1)
        self.assertTrue(result[1]["facts"].get("fees"))

    def test_hkex_rows_require_official_reports(self):
        table = """
        <table><thead><tr><th>Stock Code</th><th>Stock Name</th><th>New listing announcements</th><th>Prospectuses</th><th>Allotment results</th></tr></thead>
        <tbody><tr><td>9941</td><td>Company B</td>
        <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/1116/announcement.pdf">Download</a></td>
        <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/1116/prospectus.pdf">Download</a></td>
        <td><a href="https://outside.test/result.pdf">Download</a></td></tr></tbody></table>
        """
        root = collectors.DocumentParser(table).root
        rows, recognized = collectors.ipo_rows(root, "主板", collectors.IPO_SOURCES["主板"], today=date(2026, 11, 17))
        self.assertTrue(recognized)
        self.assertEqual(rows[0]["code"], "09941")
        self.assertTrue(rows[0]["fresh_prospectus"])
        self.assertIsNone(rows[0]["allotment_url"])

    def test_hkex_timetable_uses_the_application_close(self):
        values = collectors.ipo_dates([
            "Application lists open ..................................... 11:45 a.m. on Thursday, November 19, 2026",
            "Application lists close .................................... 12:00 noon on Thursday, November 19, 2026",
            "Expected Price Determination Date .................. no later than 12:00 noon on Friday,",
            "November 20, 2026",
            "Dealings in the Shares on the Stock",
            "Exchange expected to commence at 9:00 a.m. on ........... Tuesday, November 24, 2026",
        ])
        self.assertEqual(values["deadline"], "2026-11-19T12:00:00+08:00")
        self.assertEqual(values["listing_date"], "2026-11-24")


if __name__ == "__main__":
    unittest.main()
