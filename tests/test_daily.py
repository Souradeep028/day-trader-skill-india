"""trader-broker's daily.py, on the edge report's real fixtures (five Nifty 50 stocks, Nifty 50 and India VIX:
Yahoo's 5-minute bars for the 60 sessions to 6 November 2026)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

daily = load("trader-broker", "daily")
forward = load("trader-broker", "forward")
EDGE = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = daily.main(argv)
    return code, out.getvalue(), err.getvalue()


class Daily(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "five.txt")
        with open(self.wl, "w") as fh:
            fh.write("\n".join(FIVE) + "\n")
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("forward", "state", "config", "sip", "events",
                                                                 "surveillance")}

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, date="2026-11-05"):
        return (["--date", date, "--offline", EDGE, "--watchlist", self.wl, "--sip-price", "284.22",
                 "--out", os.path.join(self.d, "daily.md")] + [x for k, v in self.p.items()
                                                               for x in ("--" + k, v)] + list(extra))

    def write(self, key, doc):
        with open(self.p[key], "w") as fh:
            json.dump(doc, fh)

    def test_a_closed_day(self):
        code, text, _ = run(self.args(date="2026-11-10"))
        self.assertEqual(code, 0)
        self.assertEqual(text, "No NSE session on 2026-11-10 (NSE holiday: Diwali Balipratipada). "
                               "The next one is 2026-11-11.\n")

    def test_the_market(self):
        code, text, err = run(self.args())
        self.assertEqual(code, 0, err)
        # Nifty closed 5 November at 24,536.60 from 23,960.60 the session before: +2.40 %, as the daily bars say
        self.assertIn("- Nifty 50 24536.60 (+2.40 %), India VIX", text)
        # all five rose that day: no "down most" line made of risers
        self.assertIn("- The watchlist: 5 up, 0 down.", text)
        self.assertIn("- Up most: INFY.NS +3.5 %, HDFCBANK.NS +3.2 %, SBIN.NS +2.8 %.", text)
        self.assertNotIn("Down most", text)
        self.assertIn("No frozen rule (trader/forward.json)", text)
        self.assertIn("No trader-broker log here", text)
        self.assertIn("- Next session: Friday 6 November.", text)
        with open(os.path.join(self.d, "daily.md")) as fh:
            self.assertEqual(fh.read(), text)

    def test_the_forward_test_fills_the_missing_sessions_once(self):
        forward.main(["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05", "--expect-low", "-0.1",
                      "--file", self.p["forward"], "--today", "2026-10-21"])
        run(self.args())
        with open(self.p["forward"]) as fh:
            doc = json.load(fh)
        sessions = doc["shadow"]["sessions"]
        # 21 October to 5 November: 12 sessions (20 October was Dussehra)
        self.assertEqual((min(sessions), max(sessions), len(sessions)), ("2026-10-21", "2026-11-05", 12))
        self.assertEqual(doc["verdicts"]["shadow"]["date"], "2026-11-05")
        n = sum(len(v) for v in sessions.values())
        code, text, _ = run(self.args())                                   # again: nothing doubled
        with open(self.p["forward"]) as fh:
            self.assertEqual(sum(len(v) for v in json.load(fh)["shadow"]["sessions"].values()), n)
        self.assertIn("Rule: orb15 long tight 2R none, frozen 2026-10-21.", text)
        self.assertIn("- So far: 12 sessions, %d trades" % n, text)

    def test_your_trading_limits_core_and_tomorrow(self):
        self.write("state", {"orders": [
            {"status": "approved", "date": "2026-11-05", "symbol": "ABC.NS", "side": "buy", "kind": "entry",
             "qty": "10", "stop": 99.0, "max_loss": 12.0},
            {"status": "filled", "date": "2026-11-05", "symbol": "ABC.NS", "side": "buy", "qty": "10", "price": 100.0,
             "paper": True},
            {"status": "filled", "date": "2026-11-05", "symbol": "ABC.NS", "side": "sell", "qty": "10", "price": 99.0,
             "paper": True},
            {"status": "filled", "date": "2026-11-05", "symbol": "XYZ.NS", "side": "buy", "qty": "5", "price": 50.0,
             "paper": True}]})
        self.write("config", {"budget": 100000})
        self.write("sip", {"fund": "NIFTYBEES.NS", "buys": [{"date": "2026-05-11", "amount": 10000, "units": 36}]})
        self.write("events", {"events": [{"symbol": "TCS.NS", "date": "2026-11-06", "what": "ex-dividend 11"},
                                         {"symbol": "INFY.NS", "date": "2026-11-05", "what": "Q2 results"}]})
        self.write("surveillance", {"updated": "2026-10-20", "symbols": {}})
        code, text, err = run(self.args())
        self.assertEqual(code, 0, err)
        # bought 10 at 100, sold at 99: -10 less 0.42 + 0.64 of charges = -11.06; 1 R was 10 x (100 - 99) = 10
        self.assertIn("- Paper: 1 trade, -₹11.06 after ₹1.06 of charges, -1.11 R.", text)
        self.assertIn("STILL OPEN after the close (paper): 5 XYZ.NS on 2026-11-05", text)
        self.assertIn("- Losing streak at the close: 1 of 2.", text)
        self.assertNotIn("Drawdown", text)                                  # 0.01 % is not worth a line
        self.assertIn("- This week: -₹11.06 realised, ₹11.06 of your ₹3,000 loss limit used.", text)
        # 36 units x 284.22 = 10,231.92 on 10,000
        self.assertIn("- NIFTYBEES.NS: ₹10,000 in, worth ₹10,231.92 (+₹231.92)", text)
        self.assertIn("- TCS.NS: ex-dividend 11.", text)
        self.assertIn("- INFY.NS: Q2 results (today, after the close: no entries tomorrow).", text)
        self.assertIn("- The ASM/GSM lists are 17 days old", text)

    def test_a_locked_live_account(self):
        self.write("state", {"orders": []})
        self.write("config", {"budget": 100000, "paper": False})
        code, text, _ = run(self.args())
        self.assertIn("- Live account LOCKED: no forward test (trader/forward.json)", text)


if __name__ == "__main__":
    unittest.main()
