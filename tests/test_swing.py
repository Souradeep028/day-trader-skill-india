"""trader-timemachine's swing.py. Fixtures in tests/fixtures/trader-timemachine/swing/: the real ten-year daily
charts of five Nifty 50 stocks and the Nifty 50 index (^NSEI), fetched from Yahoo on 9 November 2026 (kept to
the open, high, low, close and volume)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

swing = load("trader-timemachine", "swing")
DIR = os.path.join(FIX, "trader-timemachine", "swing")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")
TODAY = "2026-11-09"


def charts():
    return {s: swing.read_offline(DIR, s) for s in FIVE}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = swing.main(argv)
    return code, out.getvalue(), err.getvalue()


class Pieces(unittest.TestCase):
    def test_delivery_charges_by_hand(self):
        # a 20,000 buy: STT 0.1 % = 20.00, exchange 0.61, SEBI 0.02, stamp 0.015 % = 3.00, GST 18 % of 0.63 = 0.11
        self.assertEqual(swing.delivery_charges("buy", 20000)["total"], 23.74)
        # a 21,000 sale: STT 21.00, exchange 0.64, SEBI 0.02, GST 0.12, and the 15.34 DP charge
        sell = swing.delivery_charges("sell", 21000)
        self.assertEqual((sell["dp"], sell["stamp"], sell["total"]), (15.34, 0.0, 37.12))
        self.assertEqual(swing.delivery_charges("buy", 20000)["brokerage"], 0.0)       # no brokerage on delivery

    def test_indicators(self):
        # Wilder's RSI(2) on 1, 2, 3, 2, 3, 4: gains 1, 1 -> 100; then a loss of 1: averages 0.5 / 0.5 -> 50;
        # +1: 0.75 / 0.25 -> 75; +1: 0.875 / 0.125 -> 87.5
        self.assertEqual(swing.rsi([1, 2, 3, 2, 3, 4], 2), [None, None, 100.0, 50.0, 75.0, 87.5])
        self.assertEqual(swing.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_todays_bar_is_left_out(self):
        bars = swing.daily_bars(swing.read_offline(DIR, "INFY.NS"), "INFY.NS", TODAY)
        self.assertEqual(bars[-1][0], "2026-11-06")
        self.assertEqual(len(bars), 2471)
        self.assertEqual(swing.daily_bars(swing.read_offline(DIR, "INFY.NS"), "INFY.NS", "2099-01-01")[-1][0],
                         "2026-11-09")

    def test_the_trades_match_an_independent_replay(self):
        # breakout20 with a time10 exit on INFY, written again from scratch: the 20-day closing high, bought at the
        # next open, a 2 x ATR(14) stop (Wilder), the stop first in every day, sold at the 10th day's close
        bars = swing.daily_bars(swing.read_offline(DIR, "INFY.NS"), "INFY.NS", TODAY)
        mine = swing.trades_for("INFY.NS", swing.prepare(bars), "breakout20", "time10",
                                {"risk": 1000.0, "max_position": 20000.0})
        closes = [b[4] for b in bars]
        tr = [None] + [max(bars[i][2] - bars[i][3], abs(bars[i][2] - bars[i - 1][4]), abs(bars[i][3] - bars[i - 1][4]))
                       for i in range(1, len(bars))]
        atr = [None] * len(bars)
        a = sum(tr[1:15]) / 14
        atr[14] = a
        for i in range(15, len(bars)):
            a = (a * 13 + tr[i]) / 14
            atr[i] = a

        def ch(side, v):
            stt, ex, sebi = round(v * 0.001, 2), round(v * 0.0000307, 2), round(v * 1e-6, 2)
            return stt + ex + sebi + (round(v * 0.00015, 2) if side == "buy" else 0) + round((ex + sebi) * 0.18, 2) \
                + (15.34 if side == "sell" else 0)
        out, i = [], 200
        while i < len(bars) - 1:
            if not closes[i] > max(closes[i - 20:i]):
                i += 1
                continue
            k = i + 1
            e = bars[k][1]
            stop = round(e - 2 * atr[i], 2)
            q = min(int(1000 // (e - stop)), int(20000 // e))
            x = None
            for j in range(k, min(k + 10, len(bars))):
                o, h, l, c = bars[j][1:5]
                if l <= stop:
                    x = (j, stop if j == k else min(stop, o))
                    break
                if j - k + 1 == 10:
                    x = (j, c)
                    break
            if x is None:
                break
            j, px = x
            out.append((bars[k][0], e, stop, q, px, q * (px - e) - ch("buy", q * e) - ch("sell", q * px)))
            i = j + 1
        self.assertEqual(len(mine), 93)
        self.assertEqual([(t["date"], t["entry"], t["stop"], t["qty"], t["exit"]) for t in mine], [o[:5] for o in out])
        self.assertTrue(all(abs(t["pnl"] - o[5]) < 0.011 for t, o in zip(mine, out)))

    def test_no_lookahead(self):
        bars = swing.daily_bars(swing.read_offline(DIR, "TCS.NS"), "TCS.NS", TODAY)
        dates = [b[0] for b in bars]
        for entry in swing.ENTRIES:
            for t in swing.trades_for("TCS.NS", swing.prepare(bars), entry, "time5", {"risk": 1000.0,
                                                                                      "max_position": 20000.0}):
                self.assertEqual(dates.index(t["date"]), dates.index(t["signal"]) + 1)   # bought the day after
                self.assertEqual(t["entry"], bars[dates.index(t["date"])][1])            # at that day's open
                self.assertLessEqual(t["days"], 5)
                self.assertTrue(t["exit"] >= t["stop"] - 1e-9 or t["why"] == "stop")


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = swing.swing_report(charts(), swing.read_offline(DIR, "^NSEI"), today=TODAY)

    def test_the_grid_and_the_split(self):
        d = self.doc
        self.assertEqual((d["symbols"], d["rules"], d["from"], d["to"], d["split"]),
                         (5, 40, "2017-08-30", "2026-11-06", "2023-10-13"))
        self.assertEqual(d["significance"], float("%.3g" % (0.05 / 40)))
        self.assertEqual(d["nifty_cagr"], 10.64)

    def test_one_rule(self):
        r = [x for x in self.doc["rows"] if (x["entry"], x["exit"], x["filter"]) == ("breakout20", "time10", "none")][0]
        a = r["all"]
        self.assertEqual((a["trades"], a["avg_R"], a["avg_ret_pct"], a["avg_days"]), (424, 0.017, 0.124, 8.5))
        # it makes money on these survivors, but no more than buying them on any day and holding 8 days does
        self.assertEqual(r["drift_pct"], 0.0457)
        self.assertTrue(r["beats_drift"])
        self.assertFalse(r["passed"])

    def test_nothing_passes_and_the_verdict_says_why(self):
        self.assertEqual(self.doc["passed"], [])
        self.assertIn("No rule passed every test", self.doc["verdict"])
        self.assertIn("not by more than luck explains across 40 rules", self.doc["verdict"])

    def test_without_the_index_the_regime_filter_is_left_out(self):
        doc = swing.swing_report(charts(), today=TODAY)
        self.assertEqual(doc["rules"], 20)
        self.assertIsNone(doc["nifty_cagr"])

    def test_the_report_carries_the_warnings(self):
        text = "\n".join(swing.report(self.doc))
        for line in ("Swing edge report: 5 stocks, 2017-08-30 to 2026-11-06 (10 years)",
                     "Holding Nifty 50 over the same years: +10.64 % a year",
                     "survivors of the last ten years", "Not financial advice"):
            self.assertIn(line, text)


class Cli(unittest.TestCase):
    def test_offline_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "swing.json")
            code, text, err = run(["run", "--symbols", ",".join(FIVE) + ",ZZZZ.NS", "--offline", DIR,
                                   "--today", TODAY, "--write", out, "--top", "5"])
            self.assertEqual(code, 0, err)
            self.assertIn("Left out: ZZZZ.NS", text)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["rules"], 40)
        code, _, err = run(["run", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertIn("which stocks?", err)


if __name__ == "__main__":
    unittest.main()
