import sys, tempfile, tomllib, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import cruise_watch as cw

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text())
FEED = b"""<rss><channel>
<item><title>Alaska cruise sale: 7-night Inside Passage from $549 per person</title>
<link>https://x.test/a</link><description>Kids sail free.</description><pubDate>Tue, 27 Oct 2099 10:00:00 GMT</pubDate></item>
<item><title>Alaska cruise deal: 7-night from $1,899 per person</title>
<link>https://x.test/b</link><description>Balcony.</description></item>
<item><title>Transatlantic repositioning deal: 14-night from $699 pp</title>
<link>https://x.test/c</link><description>Barcelona to Miami</description></item>
<item><title>Antarctica expedition sale from $9,999</title><link>https://x.test/d</link><description>12-night</description></item>
<item><title>Cruise ship runs aground, no deal here</title><link>https://x.test/e</link><description></description></item>
<item><title>Old Alaska sale 7-night from $300 pp</title><link>https://x.test/f</link>
<description></description><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""

class T(unittest.TestCase):
    def run_once(self, db):
        sent = []
        found = cw.run(CFG, db, feeds=[("t", FEED)], send=lambda d, c: sent.append(d))
        return found, sent

    def test_filters_and_dedupe(self):
        with tempfile.TemporaryDirectory() as t:
            db = f"{t}/s.sqlite"
            found, sent = self.run_once(db)
            self.assertEqual(sorted(d["link"] for d in sent), ["https://x.test/a", "https://x.test/c"])
            a = next(d for d in sent if d["dest"] == "Alaska")
            self.assertAlmostEqual(a["pp_night"], 78.4, 1)
            p = CFG["party"]
            self.assertEqual(a["est_total"], round(549 * (p["adults"] + p["children"] * p["child_price_factor"])))
            self.assertIn("2 adults + 2 children", cw.message(a, CFG)[1])
            self.assertIn("Balcony", cw.message(a, CFG)[1])
            found2, sent2 = self.run_once(db)
            self.assertEqual(sent2, [])  # deduped

    def test_failed_notify_retries(self):
        with tempfile.TemporaryDirectory() as t:
            db = f"{t}/s.sqlite"
            def boom(d, c): raise RuntimeError("down")
            cw.run(CFG, db, feeds=[("t", FEED)], send=boom)
            _, sent = self.run_once(db)
            self.assertEqual(len(sent), 2)

FIXTURE = (Path(__file__).parent / "fixtures" / "cruisesheet_sample.html").read_text()


class SheetTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(f"{self.tmp.name}/s.sqlite")
        self.db.execute("CREATE TABLE seen (id TEXT PRIMARY KEY, title TEXT, first_seen TEXT)")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def cfg(self, cap=5, per_dest=10):
        c = dict(CFG)
        c["search"] = dict(CFG["search"], max_alerts_per_run=cap, max_alerts_per_destination=per_dest)
        return c

    def test_parse_real_cards(self):
        cards = cw.parse_cruisesheet(FIXTURE)
        self.assertEqual(len(cards), 4)
        for c in cards:
            self.assertTrue(c["link"].startswith("https://cruisesheet.com/"))
            self.assertRegex(c["title"], r"\d+ Night")
            self.assertGreater(c["price"], 0)
            self.assertGreater(c["cpd"], 0)
            self.assertIn(c["cabin"], {"Inside", "Outside", "Balcony", "Suite"})
            self.assertGreaterEqual(len(c["ports"]), 2)
            self.assertTrue(c["date"] and c["line"] and c["ship"])

    def test_limit_keywords_and_estimate(self):
        cards = cw.parse_cruisesheet(FIXTURE)
        top = max(c["cpd"] for c in cards)
        e = dict(name="X", max_cpd=top)
        self.assertEqual(len(cw.sheet_deals(e, cards, CFG)), 4)
        self.assertEqual(cw.sheet_deals(dict(name="X", max_cpd=1), cards, CFG), [])
        self.assertEqual(cw.sheet_deals(dict(e, keywords=["zzz-nowhere"]), cards, CFG), [])
        d = cw.sheet_deals(e, cards, CFG)[0]
        self.assertIn("per person", d["body_text"])
        self.assertIn("1 cabin (Balcony preferred)", d["body_text"])
        self.assertIn("2 adults + 2 children", d["body_text"])

    def test_per_destination_cap_and_shared_fetch(self):
        e = dict(name="X", max_cpd=100000)
        sent = []
        cw.run_sheets(self.cfg(8, per_dest=1), self.db, pages=[(e, FIXTURE)], send=lambda d, c: sent.append(d))
        self.assertEqual(len(sent), 1)  # all four sailings share one destination
        cfg = {"cruisesheet": [dict(name="A", path="", max_cpd=50, pages=2), dict(name="B", path="", max_cpd=90, pages=4),
                               dict(name="C", path="japan", max_cpd=130)]}
        calls = []
        orig = cw.fetch
        cw.fetch = lambda url: calls.append(url) or b"<html></html>"
        try:
            pages = cw.fetch_sheet_all(cfg, pause=0)
        finally:
            cw.fetch = orig
        self.assertEqual(len(calls), 2)  # one page for the shared "" path (widest limit), one for japan
        self.assertIn("max_cpd=90", calls[0])
        self.assertEqual(len(pages), 3)  # but every destination still gets the pages to filter

    def test_site_file_merges_failed_destinations_and_drops_past(self):
        import json
        from datetime import datetime, timezone
        path = f"{self.tmp.name}/site/deals.json"
        cards = cw.parse_cruisesheet(FIXTURE)
        e = dict(name="Alpaca", max_cpd=100000)
        infos = [d["info"] for d in cw.sheet_deals(e, cards, CFG)]
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        cw.write_site(path, infos, {"Alpaca"}, CFG, now=now)
        first = json.load(open(path))
        self.assertEqual(len(first["deals"]), 4)
        self.assertEqual([d["cpd"] for d in first["deals"]], sorted(d["cpd"] for d in first["deals"]))
        self.assertEqual(first["party"]["adults"], CFG["party"]["adults"])
        self.assertEqual(first["party"]["children"], 2)
        self.assertEqual(first["party"]["cabins"], 1)
        self.assertEqual(first["preferred_cabin"], "Balcony")
        # Alpaca failed to load this run, Beta loaded: Alpaca's old deals stay, Beta's are added
        other = [dict(infos[0], dest="Beta", link="https://x/b", price=1)]
        cw.write_site(path, other, {"Beta"}, CFG, now=now)
        second = json.load(open(path))
        self.assertEqual({d["dest"] for d in second["deals"]}, {"Alpaca", "Beta"})
        self.assertEqual(len(second["deals"]), 5)
        # a destination that loaded replaces its old deals, and sailed cruises are dropped
        later = datetime(2030, 1, 1, tzinfo=timezone.utc)
        cw.write_site(path, [], {"Alpaca"}, CFG, now=later)
        self.assertEqual([d["dest"] for d in json.load(open(path))["deals"]], [])

    def test_cap_dedupe_and_price_drop(self):
        e = dict(name="X", max_cpd=100000)
        sent = []
        out = cw.run_sheets(self.cfg(2), self.db, pages=[(e, FIXTURE)], send=lambda d, c: sent.append(d))
        self.assertEqual(len(sent), 2)
        self.assertLessEqual(sent[0]["cpd"], sent[1]["cpd"])  # cheapest per night first
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 4)  # rest recorded silently
        sent.clear()
        cw.run_sheets(self.cfg(2), self.db, pages=[(e, FIXTURE)], send=lambda d, c: sent.append(d))
        self.assertEqual(sent, [])  # nothing new
        card = cw.parse_cruisesheet(FIXTURE)[0]
        dropped = FIXTURE.replace(f"${card['price']:,}\n", f"${card['price'] - 100:,}\n", 1)
        cw.run_sheets(self.cfg(2), self.db, pages=[(e, dropped)], send=lambda d, c: sent.append(d))
        self.assertEqual(len(sent), 1)  # a price drop on a known sailing alerts again

    def test_balcony_cap_and_cheapest_fallback(self):
        card = dict(title="7 Night Alaska", link="https://cruisesheet.com/x", price=1050, cpd=150,
                    date="June 1, 2027", line="Princess", ship="Crown", cabin="Balcony", ribbon="",
                    ports=["Vancouver", "Juneau"])
        entry = dict(name="Alaska", max_cpd=100, preferred_max_cpd=160)
        self.assertEqual(len(cw.sheet_deals(entry, [card], CFG)), 1)
        self.assertIn("your limit $160", cw.sheet_deals(entry, [card], CFG)[0]["body_text"])
        inside_over = dict(card, cabin="Inside", cpd=150, price=1050)
        self.assertEqual(cw.sheet_deals(entry, [inside_over], CFG), [])
        inside_ok = dict(card, cabin="Inside", cpd=90, price=630, link="https://cruisesheet.com/y")
        self.assertEqual(len(cw.sheet_deals(entry, [inside_ok], CFG)), 1)
        self.assertIn("your limit $100", cw.sheet_deals(entry, [inside_ok], CFG)[0]["body_text"])

    def test_preferred_cabin_url_fallback_and_block(self):
        entry = dict(name="Alaska", path="alaska", max_cpd=100, pages=1)
        calls = []
        orig = cw.fetch
        def fake(url):
            calls.append(url)
            if "min_class=" in url:
                return b"<html><title>Cruises | CruiseSheet</title><p>No sailings</p></html>"
            return FIXTURE.encode()
        cw.fetch = fake
        try:
            pages = list(cw.fetch_sheet_pages(entry, pause=0, cabin="Balcony"))
        finally:
            cw.fetch = orig
        self.assertEqual(len(calls), 2)
        self.assertIn("min_class=balcony", calls[0])
        self.assertNotIn("min_class", calls[1])
        self.assertGreaterEqual(len(cw.parse_cruisesheet(pages[0])), 1)

        calls.clear()
        cw.fetch = lambda url: calls.append(url) or FIXTURE.encode()
        try:
            pages = list(cw.fetch_sheet_pages(entry, pause=0, cabin="Balcony"))
        finally:
            cw.fetch = orig
        self.assertEqual(len(calls), 1)  # balcony cards present: do not refetch
        self.assertIn("min_class=balcony", calls[0])
        self.assertEqual(len(pages), 1)

        calls.clear()
        cw.fetch = lambda url: calls.append(url) or b"<html><title>Just a moment...</title></html>"
        try:
            pages = list(cw.fetch_sheet_pages(entry, pause=0, cabin="Balcony"))
        finally:
            cw.fetch = orig
        self.assertEqual(len(calls), 1)  # bot check is not a second request
        self.assertEqual(len(cw.parse_cruisesheet(pages[0])), 0)

    def test_shared_fetch_uses_preferred_cap(self):
        cfg = {"search": {"preferred_cabin": "Balcony"},
               "cruisesheet": [dict(name="Tahiti", path="south-pacific", max_cpd=200, pages=1),
                               dict(name="Fiji", path="south-pacific", max_cpd=140, preferred_max_cpd=180, pages=4)]}
        calls = []
        orig = cw.fetch
        cw.fetch = lambda url: calls.append(url) or FIXTURE.encode()
        try:
            cw.fetch_sheet_all(cfg, pause=0)
        finally:
            cw.fetch = orig
        self.assertEqual(len(calls), 1)  # one shared South Pacific page, not one per destination
        self.assertIn("max_cpd=200", calls[0])  # widest of 200 and the balcony cap 180
        self.assertIn("min_class=balcony", calls[0])

    def test_403_backs_off_once(self):
        import urllib.error
        entry = dict(name="Alaska", path="alaska", max_cpd=100, pages=1)
        calls = []
        def fake(url):
            calls.append(url)
            raise urllib.error.HTTPError(url, 403, "forbidden", hdrs=None, fp=None)
        orig, orig_wait = cw.fetch, cw.BACKOFF
        cw.fetch, cw.BACKOFF = fake, 0
        try:
            pages = list(cw.fetch_sheet_pages(entry, pause=0))
        finally:
            cw.fetch, cw.BACKOFF = orig, orig_wait
        self.assertEqual(pages, [])
        self.assertEqual(len(calls), 2)  # one retry after 403, then stop


if __name__ == "__main__":
    unittest.main()
