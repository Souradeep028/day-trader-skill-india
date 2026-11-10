"""trader-scanner tests. Fixtures in tests/fixtures/trader-scanner/ (see its README.txt): real Yahoo
charts for the 50 Nifty 50 stocks, a real spark answer, and rows cut from NSE's real equity and price
band lists. Where a test needs a chart read mid-session, it cuts the real bars and says so."""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-11-05.json.gz")          # 50 real 5-min charts + one unknown symbol
DAILY = os.path.join(SDIR, "daily-nifty50-2026-11-06.json")      # 50 real daily charts to 6 Nov 2026
LIVE = os.path.join(SDIR, "spark-3mo-live-2026-11-09.json")      # a real 20-symbol spark answer
NSE = os.path.join(SDIR, "nse")                                  # rows of NSE's EQUITY_L.csv and sec_list.csv
DT = datetime.datetime
D = datetime.date
DAY = D(2026, 11, 5)
CUTOFF = datetime.time(14, 45)
AT_940 = ["BAJAJFINSV.NS", "BSE.NS", "INFY.NS", "SUNPHARMA.NS", "TATACONSUM.NS", "TECHM.NS"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """India wall-clock text -> unix seconds."""
    local = DT.strptime(ist_text, "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M")
    return int((local - datetime.timedelta(hours=5, minutes=30) - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


def orb_by_hand(cut_slot):
    """The opening-range breakout written out a second way, from the raw chart JSON: the high of the
    9:15 to 9:25 bars to the paisa, then the first bar up to cut_slot that closes above it."""
    with gzip.open(WALL, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    hits = []
    for sym, doc in bundle.items():
        res = (doc["chart"]["result"] or [None])[0]
        if not res:
            continue
        q = res["indicators"]["quote"][0]
        bars = {}
        for i, t in enumerate(res["timestamp"]):
            if q["close"][i] is None:
                continue
            local = DT(1970, 1, 1) + datetime.timedelta(seconds=t, hours=5, minutes=30)
            bars[(local.hour * 60 + local.minute - 555) // 5] = (round(q["high"][i], 2), round(q["close"][i], 2))
        high = max(bars[k][0] for k in range(3))
        if any(bars[k][1] > high for k in range(3, cut_slot + 1) if k in bars):
            hits.append(sym)
    return sorted(hits)


class Universe(unittest.TestCase):
    def setUp(self):
        texts = {}
        for f in scan.NSE_FILES:
            with open(os.path.join(NSE, f), encoding="utf-8") as fh:
                texts[f] = fh.read()
        self.kept, self.counts, self.bands = scan.build_universe(texts)

    def test_the_eq_series_is_kept(self):
        self.assertEqual([s for s, _ in self.kept],
                         ["20MICRONS.NS", "360ONE.NS", "A2ZINFRA.NS", "ABDL.NS", "BAJAJ-AUTO.NS", "BLUECHIP.NS",
                          "GLOBAL.NS", "INFY.NS", "M&M.NS", "RELIANCE.NS", "TCS.NS"])
        self.assertEqual(self.counts, {"rows": 14, "keep": 11, "trade-for-trade (BE, delivery only)": 2,
                                       "trade-for-trade (BZ, delivery only)": 1})
        self.assertEqual(dict(self.kept)["M&M.NS"], "Mahindra & Mahindra Limited")

    def test_price_bands(self):
        b = self.bands
        self.assertEqual((b["20MICRONS.NS"], b["ABDL.NS"], b["A2ZINFRA.NS"], b["BLUECHIP.NS"]), (20.0, 10.0, 5.0, 2.0))
        self.assertIsNone(b["INFY.NS"])                             # "No Band": an F&O stock
        self.assertNotIn("GLOBAL.NS", b)                            # in the equity list, not in the band list
        self.assertNotIn("ABAN.NS", b)                              # BE series: not ours

    def test_not_an_nse_file(self):
        with self.assertRaises(scan.ScanError):
            scan.build_universe({"EQUITY_L.csv": "<html>maintenance</html>", "sec_list.csv": ""})
        with self.assertRaises(scan.ScanError):
            scan.build_universe({"EQUITY_L.csv": "SYMBOL,SERIES\nINFY,EQ\n"})


class Time(unittest.TestCase):
    def test_ist_and_clock(self):
        self.assertEqual(scan.ts_to_ist(1793850300), DT(2026, 11, 5, 9, 15))       # INFY's first bar
        self.assertEqual(scan.ts_to_ist(ts("2026-07-01 09:15")), DT(2026, 7, 1, 9, 15))
        self.assertEqual(scan.parse_now("2026-11-05 09:40"), DT(2026, 11, 5, 4, 10))
        self.assertEqual(scan.parse_now("2026-11-05T04:10:00Z"), DT(2026, 11, 5, 4, 10))
        self.assertEqual(scan.clock_label(DT(2026, 11, 5, 9, 40)), "9:40 AM")
        self.assertEqual(scan.clock_label(datetime.time(14, 45)), "2:45 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        with open(LIVE, encoding="utf-8") as fh:
            data = scan.parse_spark(fh.read())
        self.assertEqual(len(data), 19)            # 20 asked; Yahoo silently drops the made-up ZZZZQX.NS
        self.assertNotIn("ZZZZQX.NS", data)
        self.assertIn("M&M.NS", data)
        t = data["INFY.NS"]
        self.assertEqual(len(t["t"]), 65)
        self.assertIsNone(t["h"])                  # spark is closes only
        self.assertEqual(t["c"][-1], 1171.6)
        self.assertEqual(t["meta"]["regularMarketVolume"], 1003958)
        # Sunday 8 November's one-hour Diwali Muhurat session is a real bar, and counts
        self.assertEqual(scan.daily_closes(t, scan.parse_now("2026-11-09 09:00"))[-2:],
                         [(D(2026, 11, 6), 1181.4), (D(2026, 11, 8), 1171.6)])
        # read during Friday's session: Friday's bar is still in progress
        self.assertEqual(scan.daily_closes(t, scan.parse_now("2026-11-06 12:00"))[-1][0], D(2026, 11, 5))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls_and_names(self):
        self.assertEqual(scan.spark_path(["M&M.NS", "infy.ns"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,INFY.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("BAJAJ-AUTO.NS", "1d", "5m"),
                         "/v8/finance/chart/BAJAJ-AUTO.NS?range=1d&interval=5m")
        self.assertEqual([scan.from_yahoo(s) for s in ("BAJAJ-AUTO.NS", "m&m.ns", "BRK-B")],
                         ["BAJAJ-AUTO.NS", "M&M.NS", "BRK.B"])


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(LIVE, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=INFY.NS&range=3mo&interval=1d"), self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/INFY.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "SBIN.NS", "ITC.NS", "LT.NS",
                "BHARTIARTL.NS", "M&M.NS", "BAJAJ-AUTO.NS", "TATASTEEL.NS", "ONGC.NS", "ETERNAL.NS", "JIOFIN.NS",
                "TITAN.NS", "ADANIENT.NS", "COALINDIA.NS", "NTPC.NS", "ZZZZQX.NS"] + ["X%02d.NS" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZZQX.NS"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00.NS"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-09 09:00"), 5, 1000000)
        self.assertEqual((counts["requested"], counts["fetched"]), (40, 19))

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)
        self.now = scan.parse_now("2026-11-09 09:00")

    def test_setups_before_the_open(self):
        self.assertEqual(len(self.syms), 50)
        self.assertIn("BAJAJ-AUTO.NS", self.syms)                   # the dash survives the round trip
        hits, counts, last = scan.scan_daily(self.data, self.syms, self.now, 0, 0)
        self.assertEqual(last, D(2026, 11, 6))
        self.assertEqual(counts, {"requested": 50, "fetched": 50, "illiquid": 0, "short": 0, "scanned": 50,
                                  "hits": 37})
        tags = {h["symbol"]: h["setups"] for h in hits}
        self.assertEqual(tags["DRREDDY.NS"], ["bull_flag"])
        self.assertEqual(sum(1 for v in tags.values() if v == ["breakout_20d"]), 36)   # a broad rally into 6 Nov
        hdfc = [h for h in hits if h["symbol"] == "HDFCBANK.NS"][0]
        self.assertEqual((hdfc["price"], hdfc["rsi"], hdfc["close_date"]), (788.25, 74.1, "2026-11-06"))

    def test_breakouts_match_a_second_count(self):
        hits, _, _ = scan.scan_daily(self.data, self.syms, self.now, 0, 0)
        tool = sorted(h["symbol"] for h in hits if "breakout_20d" in h["setups"])
        mine = []
        for sym, s in self.data.items():
            closes = [c for c in s["c"] if c is not None]
            if closes[-1] > max(closes[-21:-1]):                    # above every close of the 20 before it
                mine.append(sym)
        self.assertEqual(tool, sorted(mine))

    def test_the_in_progress_bar_is_dropped(self):
        bars = scan.daily_closes(self.data["INFY.NS"], scan.parse_now("2026-11-06 15:29"))
        self.assertEqual(bars[-1], (D(2026, 11, 5), 1167.4000244140625))
        bars = scan.daily_closes(self.data["INFY.NS"], scan.parse_now("2026-11-06 15:30"))
        self.assertEqual(bars[-1], (D(2026, 11, 6), 1181.4000244140625))

    def test_liquidity_filter(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, self.now, 5, 1000000)
        self.assertEqual(counts["illiquid"], 9)     # MARUTI, BAJAJ-AUTO, ULTRACEMCO ... under 1M shares on 6 Nov
        self.assertEqual(counts["scanned"], 41)
        self.assertNotIn("MARUTI.NS", [h["symbol"] for h in hits])

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(5, 15)], "c": [10.0] * 10,
             "meta": {"regularMarketPrice": 10, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X.NS": s, "E.NS": {"t": [], "c": [], "meta": {}}},
                                             ["X.NS", "E.NS", "GONE.NS"], scan.parse_now("2026-10-16 09:00"), 5,
                                             1000000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, CUTOFF)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_reel_scan_at_940(self):
        hits, counts, failed = self.scan_at("2026-11-05 09:40")
        self.assertEqual(len(self.syms), 51)
        self.assertEqual(counts["scanned"], 50)
        self.assertEqual(self.errors, {"ZZZZ.NS": "no data for ZZZZ.NS: No data found, symbol may be delisted"})
        self.assertEqual(sorted(h["symbol"] for h in hits), AT_940)
        self.assertEqual(AT_940, orb_by_hand(4))                     # a second count, from the raw JSON
        infy = [h for h in hits if h["symbol"] == "INFY.NS"][0]
        self.assertEqual(infy, {"symbol": "INFY.NS", "setups": ["orb"], "price": 1160.5, "at": "9:40 AM",
                                "bar_start": "09:35", "bar_index": 4, "or_high": 1154.7, "or_low": 1144.9,
                                "pct_over": 0.5})
        self.assertEqual([h["symbol"] for h in hits[:3]], ["BSE.NS", "TATACONSUM.NS", "TECHM.NS"])  # time, then name

    def test_later_in_the_morning(self):
        hits, counts, _ = self.scan_at("2026-11-05 10:30")
        self.assertEqual(len(hits), 23)
        self.assertEqual(sorted(h["symbol"] for h in hits), orb_by_hand(14))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-11-05 10:30", min_price=300)
        self.assertEqual((len(hits), counts["illiquid"]), (21, 6))
        self.assertTrue(all(h["price"] >= 300 for h in hits))

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-11-05 09:39", syms=["INFY.NS"])
        self.assertEqual(hits, [])                         # the 9:35 bar is not done until 9:40
        hits, _, _ = self.scan_at("2026-11-05 09:40", syms=["INFY.NS"])
        self.assertEqual(hits[0]["at"], "9:40 AM")

    def test_the_live_tick_is_dropped(self):
        s = copy.deepcopy(self.data["INFY.NS"])            # the real bars as Yahoo served them at 10:04:45
        keep = [i for i, t in enumerate(s["t"]) if t <= ts("2026-11-05 10:00")]
        for k in ("t", "c", "h", "l", "v"):
            s[k] = [s[k][i] for i in keep]
        for k, v in (("t", ts("2026-11-05 10:04:45")), ("c", 1168.0), ("h", 1168.0), ("l", 1168.0), ("v", 10)):
            s[k].append(v)
        bars = scan.session_bars(s, DAY, DT(2026, 11, 5, 10, 4))
        self.assertEqual(len(bars), 9)                     # 9:15 to 9:55; 10:00 in progress, the tick dropped
        self.assertTrue(scan.usable(bars, DAY, DT(2026, 11, 5, 10, 4), 3))
        day, cut = scan.orb_window(scan.parse_now("2026-11-05 10:04"), None, 15, CUTOFF)
        hits, _, _ = scan.scan_orb({"INFY.NS": s}, ["INFY.NS"], day, cut, 15, 0, 0)
        self.assertEqual(hits[0]["price"], 1160.5)         # still the first close above the range

    def test_stage_one_is_a_necessary_condition(self):
        cut = DT(2026, 11, 5, 10, 30)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, DAY, cut)
            if scan.usable(bars, DAY, cut, 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, DAY, DT(2026, 11, 5, 10, 30), 15, 0, 0, confirm)
        self.assertEqual(len(hits), 23)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 50)                    # stage 1 skipped the rest

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["INFY.NS", "TCS.NS"], DAY, DT(2026, 11, 5, 10, 30), 15,
                                             0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["INFY.NS"])

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        cut = DT(2026, 11, 5, 10, 30)
        hits, counts, _ = scan.scan_orb(spark_like, sorted(self.data), DAY, cut, 15, 0, 0, confirm, max_confirm=5)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (5, counts["candidates"] - 5))
        self.assertLessEqual(len(hits), 5)
        hits_all, _, _ = scan.scan_orb(spark_like, sorted(self.data), DAY, cut, 15, 0, 0, confirm, max_confirm=0)
        self.assertEqual(len(hits_all), 23)
        for h in hits:
            self.assertIn(h, hits_all)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-11-05", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        bars = scan.session_bars(s, DAY, DT(2026, 11, 5, 10, 0))
        self.assertFalse(scan.usable(bars, DAY, DT(2026, 11, 5, 10, 0), 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-11-05", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X.NS": s}, ["X.NS"], DAY, DT(2026, 11, 5, 12, 0), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")          # range 9:15 to 9:45 (high 101.4), the 9:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        self.assertEqual(scan.ts_to_ist(s["t"][0]), DT(2026, 11, 5, 9, 15))

    def test_window_messages(self):
        def msg(now, at=None, until=CUTOFF):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-11-07 10:15"))
        self.assertIn("Diwali", msg("2026-11-10 10:15"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-05 09:25"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-05 09:32"))
        self.assertIn("expired", msg("2026-11-05 14:50"))
        self.assertIn("expired", msg("2026-11-05 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM", msg("2026-11-05 15:45"))
        self.assertIn("later than now", msg("2026-11-05 09:40", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-11-05 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-05 16:30"), datetime.time(9, 40), 15, CUTOFF),
                         (DAY, DT(2026, 11, 5, 9, 40)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-05 09:35"), None, 15, CUTOFF)[1],
                         DT(2026, 11, 5, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-05 10:15"), None, 7, CUTOFF)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        bands = os.path.join(self.t, "bands.json")
        code, text, _ = run(["universe", "--offline", NSE, "--out", uni, "--bands", bands,
                             "--now", "2026-11-09 08:30"])
        self.assertEqual(code, 0)
        self.assertIn("Kept 11 stocks in the EQ series", text)
        self.assertIn("Dropped: 2 trade-for-trade (BE, delivery only), 1 trade-for-trade (BZ, delivery only).",
                      text)
        self.assertIn("Price bands: 1 at 2%, 1 at 5%, 1 at 10%, 1 at 20%, 6 with no band (F&O stocks), "
                      "1 not in the band list.", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 11)
        self.assertIn("M&M.NS|Mahindra & Mahindra Limited", lines)
        doc = self.read(bands)
        self.assertEqual(doc["as_of"], "2026-11-09T03:00:00Z")
        self.assertEqual((doc["bands"]["BLUECHIP.NS"], doc["bands"]["INFY.NS"]), (2.0, None))
        self.assertNotIn("GLOBAL.NS", doc["bands"])
        risk = load("trader-risk", "check")
        self.assertEqual(risk.load_bands(doc)["20MICRONS.NS"], 20.0)        # the format trader-risk reads

        # daily over that universe with the fixture charts: 5 of the 11 are Nifty 50 stocks, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-11-09 09:00",
                             "--out", self.wl, "--min-volume", "0"])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 5 of 11 charts (6 failed or had no data)", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"]), ("daily", 11, 5, 6))
        self.assertEqual(sorted(h["symbol"] for h in doc["hits"]), ["BAJAJ-AUTO.NS", "M&M.NS", "RELIANCE.NS"])
        self.assertEqual(doc["as_of"], "2026-11-06 close")

        # the reel's 9:40 scan
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-05 09:40", "--min-price", "0",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("6 of 50 charts closed above their opening-range high by 9:40 AM", text)
        self.assertIn("INFY.NS", text)
        self.assertIn("range 1,144.90 to 1,154.70", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source")},
                         {"created_by": "trader-scanner", "created_at": "2026-11-05T04:10:00Z", "mode": "orb",
                          "as_of": "2026-11-05 09:40 IST", "universe": 51, "scanned": 50, "source": "yahoo"})
        self.assertEqual(sorted(h["symbol"] for h in doc["hits"]), AT_940)

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "09:40", "--watchlist", self.wl, "--min-price", "0",
                             "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (6, 6, 6))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the 5 November bundle."""
        bundle = json.loads(scan._read_maybe_gz(WALL))
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    y = y.replace("%26", "&")
                    r = ((bundle.get(y) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": y, "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[path.split("/chart/")[1].split("?")[0].replace("%26", "&")])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-05 10:35")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-11-05 10:30", "--min-price", "0",
                                 "--max-confirm", "3", "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0, text)
        self.assertIn("50 usable; 32 candidates, 3 confirmed", text)
        self.assertIn("PARTIAL: 29 more candidates were not checked", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 3)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 3)   # 51 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-07 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-11-07 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-05 09:32", "--out", self.wl])
        self.assertIn("after 9:35 AM", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "09:30"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-05 09:40", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-05 09:39", "--symbols", "INFY.NS",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 9:39 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)

    def test_universe_says_how_to_get_the_files_by_hand(self):
        code, _, err = run(["universe", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertEqual(code, 1)
        self.assertIn("no EQUITY_L.csv", err)
        self.assertIn("save the two files from a browser", scan.BY_HAND.lower())

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("INFY.NS", "TCS.NS", "M&M.NS", "ZZZZ.NS"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-05 10:30", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual(sorted(h["symbol"] for h in out["hits"]), ["INFY.NS", "M&M.NS", "TCS.NS"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nINFY.NS|Infosys Limited\ntcs.ns\n\nINFY.NS\n")
        self.assertEqual(scan.symbols_from_file(txt), ["INFY.NS", "TCS.NS", "INFY.NS"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "infy.ns"}, {"symbol": "TCS.NS"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["INFY.NS", "TCS.NS"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-09 09:00",
                             "--json", "--no-write", "--min-volume", "0"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-09 09:00", "--json",
                             "--no-write", "--min-volume", "0"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_the_nifty_lists(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        nifty = scan.symbols_from_file(os.path.join(root, "trader", "nifty50.txt"))
        self.assertEqual(len(nifty), 50)
        self.assertEqual(sorted(nifty), sorted(json.loads(scan._read_maybe_gz(DAILY))))
        bank = scan.symbols_from_file(os.path.join(root, "trader", "banknifty.txt"))
        # NSE's ind_niftybanklist.csv, 9 November 2026
        self.assertEqual(bank, ["AUBANK.NS", "AXISBANK.NS", "BANKBARODA.NS", "CANBK.NS", "FEDERALBNK.NS",
                                "HDFCBANK.NS", "ICICIBANK.NS", "IDFCFIRSTB.NS", "INDUSINDBK.NS", "KOTAKBANK.NS",
                                "PNB.NS", "SBIN.NS", "UNIONBANK.NS", "YESBANK.NS"])

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-09 09:00", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 37)
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


class Surveillance(unittest.TestCase):
    ASM = ('\ufeff"SYMBOL","NAME OF SECURITY","ISIN","Stage"\n"ABCLTD","Abc Ltd","INE000A01010","I"\n'
           '"XYZ-RE","Xyz Ltd","INE000A01011","II"\n"","","",""\n')
    GSM = "Report as on 06-Nov-2026\nSr No,Symbol,Company Name,GSM Stage\n1,PQR,Pqr Ltd,Stage 0\n"

    def test_parse(self):
        self.assertEqual(scan.parse_surveillance(self.ASM, "ASM"),
                         {"ABCLTD.NS": "ASM Stage I", "XYZ-RE.NS": "ASM Stage II"})
        self.assertEqual(scan.parse_surveillance(self.GSM, "GSM"), {"PQR.NS": "GSM Stage 0"})
        with self.assertRaises(scan.ScanError):
            scan.parse_surveillance("a,b\n1,2\n", "ASM")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            a, g, out = os.path.join(d, "asm-lt.csv"), os.path.join(d, "gsm.csv"), os.path.join(d, "s.json")
            with open(a, "w") as fh:
                fh.write(self.ASM)
            with open(g, "w") as fh:
                fh.write(self.GSM)
            code, text, err = run(["surveillance", a, g, "--out", out, "--now", "2026-11-09 08:30"])
            self.assertEqual(code, 0, err)
            self.assertIn("asm-lt.csv: 2 stocks on the ASM list.", text)
            self.assertIn("gsm.csv: 1 stock on the GSM list.", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["updated"], "2026-11-09")
            self.assertEqual(sorted(doc["symbols"]), ["ABCLTD.NS", "PQR.NS", "XYZ-RE.NS"])
            check = load("trader-risk", "check")
            o = {"symbol": "PQR.NS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "10", "type": "market",
                 "time_in_force": "day"}
            r = check.check([o], {"budget": 100000}, now=check.parse_now("2026-11-09 09:40"), surveillance=doc)
            self.assertIn("surveillance: PQR.NS is on NSE's GSM Stage 0 list", " | ".join(r["results"][0]["reasons"]))


class Morning(unittest.TestCase):
    """trader-scanner/morning.py: the routine runs the real tools on the real fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def morning(self, *extra):
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        cmd = [sys.executable, os.path.join(root, "trader-scanner", "morning.py"),
               "--watchlist", os.path.join(root, "trader", "nifty50.txt"),
               "--scan-offline", DAILY, "--news-offline", os.path.join(FIX, "trader-news"),
               "--universe-offline", os.path.join(FIX, "trader-scanner", "nse")] + list(extra)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return p.returncode, p.stdout, p.stderr

    def test_a_special_session(self):
        code, text, _ = self.morning("--now", "2026-11-08 12:00")
        self.assertIn("special session: Diwali Muhurat trading, a short Sunday session; the pack does not trade it",
                      text)

    def test_a_closed_day(self):
        code, text, _ = self.morning("--now", "2026-11-10 08:30")
        self.assertEqual(code, 0)
        self.assertEqual(text, "No NSE session today, Tuesday 10 November 2026 (NSE holiday: Diwali "
                               "Balipratipada). The next one is Wednesday 11 November 2026.\n")
        self.assertFalse(os.path.exists("trader/morning.md"))

    def test_the_brief(self):
        with open("trader/events.json", "w") as fh:
            json.dump({"events": [{"symbol": "MARKET", "date": "2026-11-09", "what": "US jobs data", "kind": "market"},
                                  {"symbol": "ICICIBANK.NS", "date": "2026-11-11", "what": "a board meeting"},
                                  {"symbol": "ICICIBANK.NS", "date": "2026-11-20", "what": "too late"}]}, fh)
        with open("trader/surveillance.json", "w") as fh:
            json.dump({"updated": "2026-10-20", "symbols": {}}, fh)
        code, text, err = self.morning("--now", "2026-11-09 08:30")
        self.assertEqual(code, 0, err)
        for line in ("# Morning brief: Monday 9 November 2026",
                     "Today is Nifty's weekly F&O expiry",           # Tuesday the 10th is a holiday
                     "- Universe and price bands: refreshed from NSE (11 stocks).",
                     "- ASM/GSM lists: 20 days old; NSE changes them weekly",
                     "31 of 41 charts matched a setup (2026-11-06 close).",
                     "| ICICIBANK.NS | breakout_20d | 1571.20 | 79.5 |",
                     "- ICICIBANK.NS: a board meeting on 2026-11-11.",
                     "- MARKET: US jobs data on 2026-11-09. The risk check blocks every new entry that day.",
                     "run ticket.py reconcile after every fill",
                     "- Max loss ₹500 a trade with charges, ₹1,500 a day, 3 trades, charges at most 15 % of the risk.",
                     "Nothing has been placed."):
            self.assertIn(line, text)
        self.assertNotIn("too late", text)
        with open("trader/morning.md") as fh:
            self.assertEqual(fh.read(), text)
        with open("trader/watchlist.json") as fh:
            self.assertEqual(len(json.load(fh)["hits"]), 31)
        self.assertTrue(os.path.exists("trader/bands.json"))

    def test_the_preflight_at_the_top(self):
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        subprocess.run([sys.executable, os.path.join(root, "trader-broker", "forward.py"), "start", "--rule",
                        "orb30 short tight 2R none", "--expect-r", "0.05", "--file", "trader/forward.json",
                        "--today", "2026-11-01"], capture_output=True)
        with open("trader/config.json", "w") as fh:
            json.dump({"budget": 100000}, fh)
        code, text, err = self.morning("--now", "2026-11-09 08:30", "--no-news", "--preflight-offline",
                                       os.path.join(FIX, "trader-timemachine", "edge"))
        self.assertEqual(code, 0, err)
        self.assertIn("**Pre-flight: NO-GO.** No new trades today until this is fixed:", text)
        self.assertIn("- rule: orb30 short tight 2R none is a short and \"allow_short\" is off", text)
        self.assertTrue(os.path.exists("trader/state/preflight.json"))

    def test_a_universe_failure_and_no_lists(self):
        code, text, _ = self.morning("--now", "2026-11-11 08:30", "--universe-offline", "/nowhere", "--no-news")
        self.assertEqual(code, 0)
        self.assertIn("NSE turned the script away (no EQUITY_L.csv in /nowhere)", text)
        self.assertIn("There is no trader/bands.json", text)
        self.assertIn("- ASM/GSM lists: no trader/surveillance.json.", text)
        self.assertNotIn("weekly F&O expiry", text)
        self.assertNotIn("## News", text)

    def test_news_in_the_brief(self):
        import importlib
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-scanner"))
        morning = importlib.import_module("morning")
        news = {"items": [{"ticker": "INFY.NS", "title": "Infosys wins a deal", "source": "Mint", "tags": ["deal"],
                           "important": True}, {"ticker": "TCS.NS", "title": "Markets today", "source": "ET",
                                                "tags": []}],
                "moves": [{"ticker": "INFY.NS", "pct": 3.47, "moving": True}]}
        text = "\n".join(morning.brief(datetime.date(2026, 11, 11), {"news": news, "scan": {"hits": []},
                                                                     "universe": {"skipped": True}}, {}))
        self.assertIn("Moving: INFY.NS +3.5 %.", text)
        self.assertIn("2 new headlines, 1 tagged.", text)
        self.assertIn("- [INFY.NS] Infosys wins a deal (Mint: deal)", text)
        self.assertNotIn("Markets today", text)
        self.assertEqual(morning.expiry_day(datetime.date(2026, 10, 19)), True)
        self.assertEqual(morning.expiry_day(datetime.date(2026, 10, 20)), False)


if __name__ == "__main__":
    unittest.main()
