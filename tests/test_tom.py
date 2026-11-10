"""trader-timemachine's tom.py: the turn-of-the-month rule against holding NIFTYBEES. The real 10-year NIFTYBEES
daily bars in the funds fixture (to 9 November 2026), and small made-up months."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tom = load("trader-timemachine", "tom")
FUNDS = os.path.join(FIX, "trader-timemachine", "funds")
CFG = {"capital": 100000.0, "cash_rate": 0.0, "slab_pct": 30.0, "slippage_pct": 0.05}


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def months(prices):
    """Five sessions in each of January, February and March 2026, at these closes."""
    dates = ["2026-%02d-%02d" % (m, d) for m in (1, 2, 3) for d in (5, 6, 7, 8, 9)]
    return [(d, p, p, p, p) for d, p in zip(dates, prices)]


class Pieces(unittest.TestCase):
    def test_the_windows(self):
        dates = [b[0] for b in months([100] * 15)]
        # last session of January (index 4) and the first 3 of February (5, 6, 7): bought at the close of 3
        self.assertEqual(tom.windows(dates, 1, 3), [(3, 7), (8, 12)])
        self.assertEqual(tom.windows(dates, 4, 1), [(0, 5), (5, 10)])
        self.assertEqual(tom.windows(dates, 5, 1), [])          # a 5-session month has no session before its last 5

    def test_financial_years_and_charges(self):
        self.assertEqual((tom.fy_of("2026-03-31"), tom.fy_of("2026-04-01")), (2026, 2027))
        # a 1,00,000 ETF sale: STT 0.001 % = 1.00, exchange 3.07, SEBI 0.10, GST 18 % of 3.17 = 0.57, DP 15.34
        self.assertEqual(tom.etf_charges("sell", 100000)["total"], 20.08)
        # the buy: no STT or DP, stamp duty 15.00
        self.assertEqual(tom.etf_charges("buy", 100000)["total"], 18.74)

    def test_flat_prices_only_cost_money(self):
        r = tom.simulate(months([100.0] * 15), (1, 3), CFG)
        self.assertEqual((r["trips"], r["tax"]), (2, 0.0))      # a loss carries forward, no tax
        self.assertLess(r["end"], 100000)
        self.assertGreater(r["end"], 100000 - 300)              # 2 trips: 0.1 % slippage and charges each
        self.assertEqual(r["in_market_pct"], round(100.0 * 8 / 15, 1))

    def test_a_rise_inside_the_windows_is_kept_and_taxed(self):
        # the price only moves on window days: up 10 % from the close of 3 to the close of 7
        p = [100, 100, 100, 100, 102, 104, 106, 110, 110, 110, 110, 110, 110, 110, 110]
        r = tom.simulate(months(p), (1, 3), CFG)
        h = tom.hold(months(p), CFG)
        self.assertGreater(r["tax"], 0)                         # 20 % on the short-term gain
        self.assertAlmostEqual(r["tax"], 0.2 * (r["end"] + r["tax"] - 100000), delta=60)
        self.assertEqual(h["tax"], 0.0)                         # under the 1.25 lakh long-term exemption
        self.assertGreater(h["end"], r["end"])


class Report(unittest.TestCase):
    def test_on_ten_years_of_niftybees(self):
        code, out, err = quiet(tom.main, ["run", "--offline", FUNDS, "--today", "2026-11-10", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["from"], doc["to"], doc["primary"]), ("2016-12-01", "2026-11-09", "1+3"))
        self.assertEqual(len(doc["grid_cagr_pct"]), 25)
        self.assertFalse(doc["replaces_holding"])
        self.assertEqual(doc["grid_beats_hold"], 0)
        a = doc["runs"]["all"]
        self.assertLess(a["rule"]["max_drawdown_pct"], 0)
        self.assertGreater(a["rule"]["max_drawdown_pct"], a["hold"]["max_drawdown_pct"])   # a smaller worst fall
        self.assertIn("It does not replace holding", doc["verdict"])
        self.assertIn("Keep the SIP.", doc["verdict"])

    def test_the_text_and_a_liquid_fund(self):
        code, out, err = quiet(tom.main, ["run", "--offline", FUNDS, "--today", "2026-11-10", "--cash-rate", "5.75"])
        self.assertEqual(code, 0, err)
        self.assertIn("cash in a liquid fund at 5.75 % a year (taxed at 30 %)", out)
        self.assertIn("windows beat holding.", out)
        self.assertIn("By year, before charges and tax", out)


if __name__ == "__main__":
    unittest.main()
