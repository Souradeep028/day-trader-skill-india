"""trader-timemachine's openclose.py: intraday rules on ten years of daily bars. Five Nifty 50 stocks' real daily
bars (to 9 November 2026) and their 60 days of 5-minute bars, from the swing and edge fixtures."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

oc = load("trader-timemachine", "openclose")
edge = load("trader-timemachine", "edge")
DAILY = os.path.join(FIX, "trader-timemachine", "swing")
FIVE = os.path.join(FIX, "trader-timemachine", "edge")
SYMS = "INFY.NS,TCS.NS,HDFCBANK.NS,RELIANCE.NS,SBIN.NS"
FLAT = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "open_adj": {"all": 0.0},
        "close_adj": {"all": 0.0}}


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def day(o=900.0, h=906.0, l=895.0, c=905.0, gap=0.0, atr=12.0):
    return {"date": "2026-01-05", "o": o, "h": h, "l": l, "c": c, "gap": gap, "atr": atr}


class OneTrade(unittest.TestCase):
    def test_a_long_held_to_the_close(self):
        # in at 900 plus a 0.05 tick; the 1 % stop under 900.05 is 891.0495, down on the tick to 891.00
        R, pnl, ret, stopped = oc.one_trade(day(), True, "pct1", FLAT)
        qty = edge.size_for(900.05, 891.0, 500, 75000)
        charges = edge.round_trip(qty, 900.05, 904.95)
        self.assertFalse(stopped)
        self.assertAlmostEqual(pnl, round(qty * (904.95 - 900.05) - charges, 2), places=2)
        self.assertAlmostEqual(R, round(pnl / (qty * 9.05), 4), places=4)

    def test_the_stop_and_the_mirror_short(self):
        R, pnl, _, stopped = oc.one_trade(day(l=890.0), True, "pct1", FLAT)
        self.assertTrue(stopped)
        self.assertLess(R, -1.0)        # the full risk, a tick of slippage and the charges
        self.assertGreater(R, -1.2)
        # a short: in at 899.95, the stop 1 % over it on the tick (908.95 -> 909.00); the day's high of 906 is short of it
        R, pnl, _, stopped = oc.one_trade(day(), False, "pct1", FLAT)
        self.assertFalse(stopped)
        self.assertLess(pnl, 0)         # the stock closed up at 905: a loss, bought back at 905.05
        self.assertTrue(oc.one_trade(day(h=909.0), False, "pct1", FLAT)[3])

    def test_the_adjustments_are_charged_against_the_trade(self):
        base = oc.one_trade(day(), True, "pct1", FLAT)[0]
        worse = dict(FLAT, close_adj={"all": 0.1})
        self.assertLess(oc.one_trade(day(), True, "pct1", worse)[0], base)
        self.assertLess(oc.one_trade(day(), False, "pct1", worse)[0], oc.one_trade(day(), False, "pct1", FLAT)[0])
        # the bucket of the day's own gap is used: a 2.5 % gap-up day pays the "up" rate, not the flat one
        up = dict(FLAT, open_adj={"all": 0.0, "up": 0.2, "flat": 0.0})
        self.assertLess(oc.one_trade(day(gap=2.5), False, "pct1", up)[0], oc.one_trade(day(gap=2.5), False, "pct1", FLAT)[0])
        self.assertEqual(oc.one_trade(day(gap=0.5), False, "pct1", up), oc.one_trade(day(gap=0.5), False, "pct1", FLAT))

    def test_setups_and_atr_stops(self):
        self.assertEqual((oc.side_of("gap_up_fade2"), oc.side_of("gap_up_go1"), oc.side_of("gap_down_fade1"),
                          oc.side_of("gap_down_go2")), (False, True, True, False))
        self.assertTrue(oc.fires("gap_up_fade2", 2.0))
        self.assertFalse(oc.fires("gap_up_fade2", 1.99))
        self.assertTrue(oc.fires("gap_down_go1", -1.0))
        self.assertFalse(oc.fires("gap_down_go1", 1.5))
        self.assertTrue(oc.fires("every_short", 0.0))
        # half an ATR of 12 under 900.05: 894.05, on the tick
        self.assertEqual(oc.stop_for(900.05, True, "atr0.5", 12.0, 0.05), 894.05)
        self.assertIsNone(oc.stop_for(900.05, True, "atr1", None, 0.05))
        self.assertEqual(len(oc.SETUPS) * len(oc.STOPS) * len(oc.FILTERS), 200)


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, out, err = quiet(oc.main, ["run", "--offline", DAILY, "--offline-5m", FIVE, "--symbols", SYMS,
                                         "--today", "2026-11-10", "--json"])
        assert code == 0, err
        cls.doc = json.loads(out)

    def test_the_data_check_measures_each_kind_of_day(self):
        ck = self.doc["data_check"]
        self.assertEqual(ck["days"], 300)                    # 5 stocks x 60 sessions
        self.assertEqual(sorted(ck["buckets"]), ["down", "flat", "up"])
        self.assertEqual(sum(v["days"] for v in ck["buckets"].values()), 300)
        adj = self.doc["settings"]
        # buckets with under 30 overlap days use the all-days average
        for w in ("open", "close"):
            for k, v in ck["buckets"].items():
                want = abs(v[w + "_gap_pct"]) if v["days"] >= oc.MIN_CHECK_DAYS else abs(ck[w + "_gap_pct"])
                self.assertAlmostEqual(adj[w + "_adj"][k], round(want, 3), places=3)

    def test_the_grid_the_baselines_and_the_verdict(self):
        d = self.doc
        self.assertEqual((d["rules"], d["significance"], d["symbols"]), (200, 0.00025, 5))
        self.assertTrue(all(not r["passed"] for r in d["rows"] if r["baseline"]))
        for r in d["rows"]:
            if not r["baseline"] and r["all"]["trades"]:
                self.assertEqual(r["beats_baseline"], r["all"]["avg_R"] > r["baseline_R"])
        self.assertIn("No rule passed", d["verdict"])
        lead = [r for r in d["rows"] if r.get("recent")]
        self.assertTrue(lead and all("five_min_R" in r["recent"] for r in lead if r["recent"]["trades"]))

    def test_the_auction_switch_and_the_text(self):
        code, out, err = quiet(oc.main, ["run", "--offline", DAILY, "--offline-5m", FIVE, "--symbols", SYMS,
                                         "--today", "2026-11-10", "--auction", "--top", "3"])
        self.assertEqual(code, 0, err)
        self.assertIn("Charged against every trade, entering in the pre-open auction: at the open all 0.000 %", out)
        self.assertIn("Baselines (every stock, every day, no filter):", out)
        self.assertIn("in at the auction price, out at a real 3:10", out)
        code, out, _ = quiet(oc.main, ["run", "--offline", DAILY, "--symbols", SYMS, "--today", "2026-11-10",
                                       "--top", "1"])
        self.assertIn("No 5-minute bars to check the daily bars against: the default is charged.", out)
        self.assertIn("at the open all 0.150 %; at the close all 0.150 %", out)


if __name__ == "__main__":
    unittest.main()
