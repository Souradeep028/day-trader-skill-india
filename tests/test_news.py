"""trader-news tests. Fixtures in tests/fixtures/trader-news/, fetched on 9 November 2026:
gnews-INFY.NS.xml and gnews-TCS.NS.xml are Google News India's real answers for "Infosys" and
"Tata Consultancy Services" over 7 days, yahoo-INFY.NS.xml is Yahoo's real INFY.NS feed, and
chart-INFY.NS.json is Infosys's real 5 November chart (see tests/fixtures/trader-entry/)."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-11-08T10:30:00+00:00"          # 4:00 PM IST, Sunday, after the Muhurat session
WATCH = {"INFY.NS": "Infosys Limited", "TCS.NS": "Tata Consultancy Services Limited"}
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def everything():
    items = (news.parse_gnews(raw("gnews-INFY.NS.xml"), "INFY.NS") + news.parse_rss(raw("yahoo-INFY.NS.xml"), "INFY.NS")
             + news.parse_gnews(raw("gnews-TCS.NS.xml"), "TCS.NS"))
    return news.dedupe(news.tag(items, WATCH))


class Feeds(unittest.TestCase):
    def test_google_news(self):
        items = news.parse_gnews(raw("gnews-INFY.NS.xml"), "INFY.NS")
        self.assertEqual(len(items), 49)
        first = items[0]
        # the publisher is cut off the title and kept as the source
        self.assertEqual(first["title"], "Infosys Enables Direct Metro Connectivity to its Pune Campus to Advance "
                                         "Sustainable Commuting")
        self.assertEqual(first["source"], "Infosys")
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 3, 13, 49, 19, tzinfo=UTC))
        self.assertTrue(first["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertTrue(first["id"].startswith("gnews:"))
        self.assertTrue(all(i["summary"] == "" for i in items))
        et = [i for i in items if i["source"] == "The Economic Times"]
        self.assertTrue(et and not any(i["title"].endswith("The Economic Times") for i in et))

    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-INFY.NS.xml"), "INFY.NS")
        self.assertEqual(len(items), 13)
        self.assertTrue(all(i["source"] == "Yahoo Finance" for i in items))
        self.assertEqual(items[0]["time"], datetime.datetime(2026, 10, 29, 14, 1, 10, tzinfo=UTC))

    def test_the_search_is_by_company_name(self):
        self.assertEqual(news.short_name("Infosys Limited", "INFY.NS"), "Infosys")
        self.assertEqual(news.short_name("Tata Consultancy Services Limited", "TCS.NS"), "Tata Consultancy Services")
        self.assertEqual(news.short_name("Mahindra & Mahindra Limited", "M&M.NS"), "Mahindra & Mahindra")
        self.assertEqual(news.short_name(None, "M&M.NS"), "M&M")
        self.assertEqual(news.gnews_query("INFY.NS", "Infosys Limited", 48), '"Infosys" when:2d')
        self.assertEqual(news.gnews_query("INFY.NS", None, 6), '"INFY" when:1d')

    def test_names_from_the_universe(self):
        with tempfile.TemporaryDirectory() as d:
            u = os.path.join(d, "universe.txt")
            with open(u, "w") as fh:
                fh.write("INFY.NS|Infosys Limited\nTCS.NS|Tata Consultancy Services Limited\n")
            self.assertEqual(news.names_from_universe(u), WATCH)
        self.assertEqual(news.names_from_universe("/nonexistent.txt"), {})

    def test_times(self):
        self.assertEqual(news.parse_time("Fri, 06 Nov 2026 03:48:39 GMT").hour, 3)
        self.assertEqual(news.parse_time("2026-11-05T09:40:00+05:30").hour, 4)
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.to_ist(datetime.datetime(2026, 11, 6, 3, 48, tzinfo=UTC)).strftime("%H:%M"), "09:18")
        self.assertEqual(news.to_ist(datetime.datetime(2026, 7, 1, 3, 48, tzinfo=UTC)).strftime("%H:%M"), "09:18")


class Tagging(unittest.TestCase):
    def test_a_story_on_both_stocks_touches_both(self):
        items = everything()
        it = [i for i in items if i["title"].startswith("Infosys, Wipro, TCS and other IT stocks rise")][0]
        self.assertEqual(it["touches"], ["INFY.NS", "TCS.NS"])          # "TCS" in capitals, as a word
        self.assertEqual(it["source"], "The Economic Times")
        self.assertEqual(len([i for i in items if i["touches"] == ["INFY.NS", "TCS.NS"]]), 7)

    def test_indian_tags(self):
        by = {i["title"]: i for i in everything()}
        self.assertIn("earnings", by["What Infosys (INFY) Said on Its Q2 Earnings Call"]["tags"])
        self.assertTrue(by["What Infosys (INFY) Said on Its Q2 Earnings Call"]["important"])
        election = [t for t in by if t.startswith("US Midterm Election results")][0]
        self.assertNotIn("earnings", by[election]["tags"])               # election results are not earnings
        made_up = [{"id": "x", "source": "Moneycontrol", "kind": "headline", "ticker": "INFY.NS", "summary": "",
                    "link": "", "time": None,
                    "title": "Infosys hits upper circuit after SEBI nod; RBI keeps repo rate, FIIs buy; block deal "
                             "in TCS; Q2FY27 results date set"}]
        tags = news.tag(made_up, WATCH)[0]["tags"]
        for t in ("halt", "regulatory", "macro", "capital", "earnings"):
            self.assertIn(t, tags)

    def test_a_capital_word_is_not_a_symbol_inside_another(self):
        hay = [{"id": "y", "source": "x", "kind": "headline", "ticker": "ITC.NS", "summary": "", "link": "",
                "time": None, "title": "ITCHOTELS lists; HITC Ltd and itc are other names"}]
        self.assertEqual(news.tag(hay, {"TCS.NS": None, "ITC.NS": None})[0]["touches"], ["ITC.NS"])
        hay[0]["title"] = "Results day for ITC: what to watch"
        self.assertEqual(news.tag(hay, {"ITC.NS": None})[0]["touches"], ["ITC.NS"])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_gnews(raw("gnews-INFY.NS.xml"), "INFY.NS")
        b = news.parse_gnews(raw("gnews-INFY.NS.xml"), "TCS.NS")
        items = news.dedupe(news.tag(a + b, {"INFY.NS": None, "TCS.NS": None}))
        self.assertEqual(len(items), 49)
        self.assertEqual(items[0]["touches"], ["INFY.NS", "TCS.NS"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW, "--portfolio", none,
                     "--watchlist", none, "--plan", none, "--universe", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "INFY.NS", "TCS.NS", "--hours", "48", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 19)
        self.assertEqual(data["notes"], [])
        code, out, _ = run(["--tickers", "INFY.NS", "TCS.NS", "--hours", "48"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_the_text_is_in_india_time(self):
        code, out, err = run(["--tickers", "INFY.NS", "--hours", "168", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("2026-11-06 09:18 IST  INFY.NS", out)               # 03:48 UTC, the ET story
        self.assertIn("Checked 2026-11-08 16:00 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)
        self.assertNotRegex(out, r"\d\d:\d\d ET\b")                         # no New York times left

    def test_no_google(self):
        code, out, err = run(["--tickers", "INFY.NS", "--hours", "400", "--no-google", "--json", "--dry-run"]
                             + self.base)
        self.assertEqual({i["source"] for i in json.loads(out)["items"]}, {"Yahoo Finance"})

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "TCS.NS", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "INFY.NS"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["INFY.NS", "TCS.NS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "48"] + args)
        self.assertEqual(code, 0, err)
        self.assertEqual({t for i in json.loads(out)["items"] for t in i["touches"]}, {"TCS.NS"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_moves(self):
        m = news.parse_chart(raw("chart-INFY.NS.json"), "INFY.NS")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1167.4, 1128.2, 3.47))
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "15:29")
        line = news.render_moves([m])[0]
        self.assertIn("INFY.NS      1,167.40 +3.47% vs yesterday's close 1,128.20 at 15:29 IST  MOVING", line)
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ.NS")
        code, out, _ = run(["--tickers", "TCS.NS", "--hours", "48", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)                     # no chart file offline: no line, no note
        self.assertNotIn("note:", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
