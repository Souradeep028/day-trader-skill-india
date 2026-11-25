"""trader-broker's gapshadow.py: the gap-up short judged on sessions after it was frozen. Five Nifty 50 stocks'
real daily and 5-minute bars (the swing and edge fixtures, to 6 November 2026)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

gap = load("trader-broker", "gapshadow")
daily = load("trader-broker", "daily")
preopen = load("trader-broker", "preopen")
swing_ = load("trader-timemachine", "swing")
DAILY = os.path.join(FIX, "trader-timemachine", "swing")
FIVE = os.path.join(FIX, "trader-timemachine", "edge")
SYMS = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def snaps(folder, start, end, at="09:07:30", change=None):
    """Pre-open snapshots for every session from start to end whose prices ARE the official opens (so the picks match
    the old way), saved as preopen.py saves them. change {(date, sym): price} puts in a different indicative price."""
    bars = {s: swing_.daily_bars(swing_.read_offline(DAILY, s), s) for s in SYMS}
    days = sorted({b[0] for v in bars.values() for b in v if start <= b[0] <= end})
    for d in days:
        stocks = {}
        for s, v in bars.items():
            i = next((k for k, b in enumerate(v) if b[0] == d), None)
            if i:
                price = (change or {}).get((d, s), v[i][1])
                stocks[s] = {"price": price, "prev_close": v[i - 1][4],
                             "gap_pct": round(100.0 * (price / v[i - 1][4] - 1), 3)}
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, d + ".json"), "w") as fh:
            json.dump({"date": d, "time": at, "source": "test", "format": "csv", "stocks": stocks}, fh)


class GapShadow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.file = os.path.join(self.d, "forward-gap.json")
        self.pre = os.path.join(self.d, "preopen")
        self.wl = os.path.join(self.d, "wl.txt")
        with open(self.wl, "w") as fh:
            fh.write("\n".join(SYMS) + "\n")

    def shadow(self, today="2026-11-06"):
        return quiet(gap.main, ["shadow", "--file", self.file, "--today", today, "--watchlist", self.wl, "--offline",
                                DAILY, "--offline-5m", FIVE, "--preopen", self.pre])

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_freeze_then_replay(self):
        code, out, _ = quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-01"])
        self.assertEqual(code, 0)
        self.assertIn("Expected at most +0.283 R a trade, low end +0.147 R", out)
        self.assertIn("picked on the 9:07 snapshot", out)
        self.assertEqual(quiet(gap.main, ["start", "--file", self.file])[0], 1)     # one at a time
        snaps(self.pre, "2026-10-01", "2026-11-06")
        code, out, err = self.shadow()
        self.assertEqual(code, 0, err)
        with open(self.file) as fh:
            doc = json.load(fh)
        s = doc["shadow"]["sessions"]
        self.assertEqual(min(s), "2026-10-01")                  # nothing from before the freeze
        trades = [t for d in sorted(s) for t in s[d]]
        self.assertEqual(len(trades), 5)
        # 23 October: INFY opened 2.92 % up at 1,187.00 (short at 1,186.95), the biggest gap of the day first;
        # it never reached the stop and was bought back at 15:10 for +3.09 R. TCS, +2.44 %, hit its stop.
        oct23 = s["2026-10-23"]
        self.assertEqual([(t["symbol"], t["outcome"]) for t in oct23], [("INFY.NS", "flat"), ("TCS.NS", "stop")])
        self.assertAlmostEqual(oct23[0]["R"], 3.0928, places=4)
        self.assertLess(oct23[1]["R"], -1.0)
        self.assertEqual(doc["verdicts"]["shadow"]["state"], "running")
        self.assertIn("Too early: 5 trades of the 40 before any verdict (no PASS before 300).", out)
        self.assertNotIn("WARNING", out)

    def test_it_decides_on_the_snapshot_and_fills_at_the_open(self):
        quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-23"])
        # at 9:07 INFY showed only +1.5 % (it opened +2.92 %), so the rule cannot pick it: only TCS is taken,
        # still sold at its official open
        prev = 1187.0 / 1.0292
        snaps(self.pre, "2026-10-23", "2026-10-23", change={("2026-10-23", "INFY.NS"): round(prev * 1.015, 2)})
        code, out, err = self.shadow("2026-10-23")
        self.assertEqual(code, 0, err)
        with open(self.file) as fh:
            day = json.load(fh)["shadow"]["sessions"]["2026-10-23"]
        self.assertEqual([t["symbol"] for t in day], ["TCS.NS"])
        self.assertEqual(day[0]["gap_pct"], day[0]["gap_open_pct"])
        # a snapshot taken after 9:08 knew the open: the session is skipped, with the reason
        snaps(self.pre, "2026-10-23", "2026-10-23", at="09:09:10")
        code, out, err = self.shadow("2026-10-23")
        with open(self.file) as fh:
            doc = json.load(fh)
        self.assertNotIn("2026-10-23", doc["shadow"]["sessions"])
        self.assertIn("after the auction closed at 9:08", doc["shadow"]["skipped"]["2026-10-23"])
        self.assertIn("- Today: skipped, the snapshot was taken at 09:09:10", out)
        # no snapshot at all: skipped too, never counted as a day with no trade
        os.remove(os.path.join(self.pre, "2026-10-23.json"))
        code, out, err = self.shadow("2026-10-23")
        self.assertIn("- Today: skipped, no pre-open snapshot", out)

    def test_an_old_test_says_it_picks_on_the_open(self):
        quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-01"])
        with open(self.file) as fh:
            doc = json.load(fh)
        del doc["decide_on"]                               # frozen before snapshots existed
        with open(self.file, "w") as fh:
            json.dump(doc, fh)
        code, out, err = self.shadow()
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING: this test picks stocks on the official open", out)
        with open(self.file) as fh:
            self.assertEqual(len([t for v in json.load(fh)["shadow"]["sessions"].values() for t in v]), 5)

    def test_three_a_day_and_the_daily_report(self):
        quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-23"])
        snaps(self.pre, "2026-10-23", "2026-10-23")
        dtexts = {s: load("trader-timemachine", "swing").read_offline(DAILY, s) for s in SYMS}
        ftexts = {s: load("trader-timemachine", "edge").read_offline(FIVE, s) for s in SYMS}
        swing, edge = load("trader-timemachine", "swing"), load("trader-timemachine", "edge")
        bars = {s: swing.daily_bars(t, s) for s, t in dtexts.items()}
        five = {s: edge.sessions_of(t, s) for s, t in ftexts.items()}
        got = gap.session_trades("2026-10-23", bars, five)
        self.assertLessEqual(len(got), 3)
        self.assertEqual([t["gap_pct"] for t in got], sorted([t["gap_pct"] for t in got], reverse=True))
        code, out, err = quiet(daily.main, ["--date", "2026-10-23", "--offline", FIVE, "--offline-daily", DAILY,
                                            "--watchlist", self.wl, "--gap-forward", self.file, "--no-write",
                                            "--forward", os.path.join(self.d, "none.json"),
                                            "--state", os.path.join(self.d, "none.json"),
                                            "--config", os.path.join(self.d, "none.json"),
                                            "--heartbeat", os.path.join(self.d, "none.json"),
                                            "--sip", os.path.join(self.d, "none.json"),
                                            "--events", os.path.join(self.d, "none.json"),
                                            "--surveillance", os.path.join(self.d, "none.json"),
                                            "--preopen", self.pre])
        self.assertEqual(code, 0, err)
        self.assertIn("## Gap-up shadow test", out)
        self.assertIn("- Today: INFY.NS +2.9 % in the snapshot (opened +2.9 %), short at the open 1187.00, out at "
                      "15:10, +3.09 R (₹1,362).", out)
        self.assertIn("- Since the start: 2 trades", out)
        self.assertIn("- profit factor ", out)


class Preopen(unittest.TestCase):
    """preopen.py: what a pre-open order can see, from NSE's page, a Kite quote or a CSV."""
    NSE = {"advances": 1, "data": [
        {"metadata": {"symbol": "INFY", "iep": 1531.0, "lastPrice": 1531.0, "previousClose": 1500.0},
         "detail": {"preOpenMarket": {"IEP": 1531.0, "lastUpdateTime": "25-Nov-2026 09:07:41"}}},
        {"metadata": {"symbol": "TCS", "lastPrice": 3010.0, "previousClose": 3000.0},
         "detail": {"preOpenMarket": {"IEP": 3010.0, "lastUpdateTime": "25-Nov-2026 09:07:45"}}}]}
    KITE = {"status": "success", "data": {
        "NSE:INFY": {"last_price": 1531.0, "ohlc": {"close": 1500.0}, "timestamp": "2026-11-25 09:07:50"},
        "NSE:M&M": {"last_price": 3100.0, "ohlc": {"close": 3000.0}, "timestamp": "2026-11-25 09:07:52"}}}

    def test_three_formats(self):
        nse = preopen.snapshot(json.dumps(self.NSE), "nse.json")
        self.assertEqual((nse["format"], nse["date"], nse["time"]), ("nse", "2026-11-25", "09:07:45"))
        self.assertEqual(nse["stocks"]["INFY.NS"], {"price": 1531.0, "prev_close": 1500.0, "gap_pct": 2.067})
        kite = preopen.snapshot(json.dumps(self.KITE), "kite.json")
        self.assertEqual((kite["format"], kite["time"], kite["stocks"]["M&M.NS"]["gap_pct"]), ("kite", "09:07:52", 3.333))
        csv = preopen.snapshot("symbol,iep,prev_close\nINFY,1531,1500\nSBIN.NS,800,\n", "p.csv", "2026-11-25",
                               "09:07")
        self.assertEqual((csv["format"], csv["time"], csv["stocks"]["SBIN.NS"]["gap_pct"]), ("csv", "09:07:00", None))
        with self.assertRaises(preopen.PreopenError):
            preopen.snapshot("symbol,iep\nINFY,1531\n", "p.csv")             # no time: give --at
        with self.assertRaises(preopen.PreopenError):
            preopen.snapshot("a,b\n1,2\n", "p.csv", at="09:07")

    def test_only_while_the_auction_takes_orders(self):
        self.assertEqual(preopen.usable({"time": "09:07:59"}), (True, ""))
        self.assertFalse(preopen.usable({"time": "09:08:30"})[0])
        self.assertFalse(preopen.usable({"time": "08:59:00"})[0])
        self.assertFalse(preopen.usable(None)[0])

    def test_save_and_remind(self):
        d = tempfile.mkdtemp()
        try:
            f, pre, gapf = os.path.join(d, "nse.json"), os.path.join(d, "pre"), os.path.join(d, "gap.json")
            with open(f, "w") as fh:
                json.dump(self.NSE, fh)
            code, out, err = quiet(preopen.main, ["save", f, "--dir", pre])
            self.assertEqual(code, 0, err)
            self.assertIn("Saved 2026-11-25 at 09:07:45 IST: 2 stocks (nse), 1 of them 2 % or more up: INFY.NS "
                          "+2.07 %.", out)
            late = dict(self.NSE, data=[dict(self.NSE["data"][0], detail={"preOpenMarket": {
                "IEP": 1531.0, "lastUpdateTime": "26-Nov-2026 09:15:02"}})])
            with open(f, "w") as fh:
                json.dump(late, fh)
            code, _, err = quiet(preopen.main, ["save", f, "--dir", pre])
            self.assertEqual(code, 1)
            self.assertIn("after the auction closed at 9:08", err)
            # remind: nothing to do without a snapshot-deciding gap test; exit 3 when one needs today's
            self.assertEqual(quiet(preopen.main, ["remind", "--date", "2026-11-26", "--dir", pre, "--gap-file", gapf])[0], 0)
            quiet(gap.main, ["start", "--file", gapf, "--today", "2026-11-20"])
            self.assertEqual(quiet(preopen.main, ["remind", "--date", "2026-11-26", "--dir", pre, "--gap-file", gapf])[0], 3)
            self.assertEqual(quiet(preopen.main, ["remind", "--date", "2026-11-25", "--dir", pre, "--gap-file", gapf])[0], 0)
        finally:
            shutil.rmtree(d)


class CodeVersion(unittest.TestCase):
    def test_a_fingerprint_that_changes_with_the_code(self):
        import shutil
        import tempfile
        forward = load("trader-broker", "forward")
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "trader-x"))
            with open(os.path.join(d, "trader-x", "a.py"), "w") as fh:
                fh.write("A = 1\n")
            v1 = forward.code_version(d)
            self.assertEqual(v1, forward.code_version(d))
            with open(os.path.join(d, "trader-x", "notes.md"), "w") as fh:
                fh.write("docs do not count\n")
            self.assertEqual(v1, forward.code_version(d))
            with open(os.path.join(d, "trader-x", "a.py"), "w") as fh:
                fh.write("A = 2\n")
            v2 = forward.code_version(d)
            self.assertNotEqual(v1, v2)
        finally:
            shutil.rmtree(d)
        test = {}
        forward.note_code(test, v1, "2026-11-11")
        forward.note_code(test, v1, "2026-11-12")
        self.assertEqual(forward.code_text(test), "Code %s, the same on every day since 2026-11-11." % v1)
        forward.note_code(test, v2, "2026-11-13")
        self.assertIn("The code changed during the test: %s from 2026-11-11, %s from 2026-11-13." % (v1, v2),
                      forward.code_text(test))


class Performance(unittest.TestCase):
    def test_worked_by_hand(self):
        ticket = load("trader-broker", "ticket")
        nets = [300, -100, -200, 500, -100]
        p = ticket.performance([{"net": n} for n in nets], 100000)
        # won 800, lost 400: profit factor 2.00; the average win 400 against the average loss 133.33: payoff 3.00;
        # 400 net over 5 trades: 80 a trade; the running net 300, 200, 0, 500, 400 fell 300 from its high of 300
        self.assertEqual(p, {"trades": 5, "profit_factor": 2.0, "payoff": 3.0, "expectancy": 80.0,
                             "max_drawdown": 300.0, "max_drawdown_pct": 0.3})
        self.assertEqual(ticket.performance_text(p), "profit factor 2.00, payoff 3.00, expectancy +₹80 a trade, "
                                                     "worst fall ₹300 (0.30 % of the budget)")
        none_lost = ticket.performance([{"net": 10}], None)
        self.assertEqual((none_lost["profit_factor"], none_lost["payoff"], none_lost["max_drawdown_pct"]),
                         (None, None, None))
        self.assertIn("profit factor n/a (no losing trade)", ticket.performance_text(none_lost))


if __name__ == "__main__":
    unittest.main()
