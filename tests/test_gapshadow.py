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
DAILY = os.path.join(FIX, "trader-timemachine", "swing")
FIVE = os.path.join(FIX, "trader-timemachine", "edge")
SYMS = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class GapShadow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.file = os.path.join(self.d, "forward-gap.json")
        self.wl = os.path.join(self.d, "wl.txt")
        with open(self.wl, "w") as fh:
            fh.write("\n".join(SYMS) + "\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_freeze_then_replay(self):
        code, out, _ = quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-01"])
        self.assertEqual(code, 0)
        self.assertIn("Expected +0.283 R a trade, low end +0.147 R", out)
        self.assertEqual(quiet(gap.main, ["start", "--file", self.file])[0], 1)     # one at a time
        code, out, err = quiet(gap.main, ["shadow", "--file", self.file, "--today", "2026-11-06", "--watchlist",
                                          self.wl, "--offline", DAILY, "--offline-5m", FIVE])
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
        self.assertIn("Too early: 5 trades of 40.", out)

    def test_three_a_day_and_the_daily_report(self):
        quiet(gap.main, ["start", "--file", self.file, "--today", "2026-10-23"])
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
                                            "--surveillance", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 0, err)
        self.assertIn("## Gap-up shadow test", out)
        self.assertIn("- Today: INFY.NS opened +2.9 %, short at 1187.00, out at 15:10, +3.09 R (₹1,362).", out)
        self.assertIn("- Since the start: 2 trades", out)
        self.assertIn("- profit factor ", out)


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
