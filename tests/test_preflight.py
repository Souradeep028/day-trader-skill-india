"""trader-broker's preflight.py, and the autopilot's use of it. Nifty 50's real 5-minute bars from the edge
report's fixtures (to 6 November 2026)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

preflight = load("trader-broker", "preflight")
autopilot = load("trader-broker", "autopilot")
forward = load("trader-broker", "forward")
EDGE = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class Preflight(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("config", "forward", "state", "portfolio",
                                                                 "events", "surveillance", "preflight",
                                                                 "limits-state")}
        self.write("config", {"budget": 100000})
        self.write("surveillance", {"symbols": {}, "updated": "2026-11-04"})
        quiet(forward.main, ["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05",
                             "--expect-low", "-0.1", "--file", self.p["forward"], "--today", "2026-11-01"])

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, key, doc):
        with open(self.p[key], "w") as fh:
            json.dump(doc, fh)

    def pre(self, now="2026-11-05 09:00", offline=EDGE, *extra):
        args = ["--now", now, "--json"] + (["--offline", offline] if offline else [])
        for k, v in self.p.items():
            args += ["--" + k, v]
        code, out, err = quiet(preflight.main, args + list(extra))
        return code, json.loads(out) if out.strip().startswith("{") else out, err

    def failed(self, doc):
        return {c["check"]: c["detail"] for c in doc["checks"] if not c["ok"]}

    def test_go(self):
        code, doc, err = self.pre()
        self.assertEqual(code, 0, err)
        self.assertTrue(doc["go"], doc)
        self.assertEqual([c["check"] for c in doc["checks"]],
                         ["calendar", "config", "rule", "data", "files", "leftovers", "limits", "events"])
        self.assertIn("Yahoo has Nifty 50 to 2026-11-04", [c["detail"] for c in doc["checks"]])
        # it saved the result and the day's limits for trader-risk and the autopilot
        with open(self.p["limits-state"]) as fh:
            lim = json.load(fh)
        self.assertEqual((lim["date"], lim["limits"]["max_loss_per_trade_inr"]), ("2026-11-05", 500))
        with open(self.p["preflight"]) as fh:
            self.assertTrue(json.load(fh)["go"])

    def test_closed_days(self):
        code, doc, _ = self.pre("2026-11-10 09:00")
        self.assertEqual((code, doc["closed"], doc["go"]), (0, True, False))
        code, doc, _ = self.pre("2026-11-08 09:00")
        self.assertIn("a special session (Diwali Muhurat trading", doc["checks"][0]["detail"])

    def test_no_go_reasons(self):
        os.remove(self.p["forward"])
        code, doc, _ = self.pre()
        self.assertEqual(code, 1)
        self.assertIn("no frozen rule", self.failed(doc)["rule"])
        quiet(forward.main, ["start", "--rule", "orb30 short tight 2R none", "--expect-r", "0.05", "--file",
                             self.p["forward"], "--today", "2026-11-01"])
        self.assertIn("is a short and \"allow_short\" is off", self.failed(self.pre()[1])["rule"])
        self.write("config", {"budget": 100000, "allow_short": True})
        self.assertNotIn("rule", self.failed(self.pre()[1]))
        # no data at all, or a feed a session behind
        self.assertIn("no Nifty 50 bars from Yahoo", self.failed(self.pre(offline=self.d)[1])["data"])
        # the fixture's Nifty bars run to 9 November (fetched that day): on Friday the 13th it is behind
        self.assertIn("Yahoo's last Nifty 50 session is 2026-11-09, not 2026-11-12",
                      self.failed(self.pre("2026-11-13 09:00")[1])["data"])

    def test_leftovers(self):
        self.write("state", {"orders": [], "paper_legs": [{"symbol": "INFY.NS", "status": "open", "date": "2026-11-04"}]})
        self.write("portfolio", {"cash": 50000, "positions": [{"ticker": "INFY.NS", "shares": 41}]})
        doc = self.pre()[1]
        self.assertIn("an open paper stop and target on INFY.NS from 2026-11-04", self.failed(doc)["leftovers"])
        self.assertIn("41 INFY.NS in the portfolio", self.failed(doc)["leftovers"])
        self.assertFalse(doc["go"])

    def test_limits_and_events(self):
        # Monday's loss used 2,700 of the 3,000 week: 300 left, under one full 500 trade
        self.write("state", {"orders": [
            {"status": "filled", "date": "2026-11-02", "symbol": "ABC.NS", "side": "buy", "qty": "100", "price": 100.0},
            {"status": "filled", "date": "2026-11-02", "symbol": "ABC.NS", "side": "sell", "qty": "100", "price": 73.0}]})
        self.assertIn("the week's loss limit has", self.failed(self.pre()[1])["limits"])
        os.remove(self.p["state"])
        self.write("events", {"events": [{"symbol": "MARKET", "date": "2026-11-05", "what": "RBI policy"}]})
        self.assertIn("RBI policy today", self.failed(self.pre()[1])["events"])
        self.write("events", {"events": [{"symbol": "INFY.NS", "date": "2026-11-05", "what": "Q2 results"}]})
        doc = self.pre()[1]
        self.assertTrue(doc["go"])
        self.assertIn("stocks off limits today: INFY.NS (Q2 results)", [c["detail"] for c in doc["checks"]])

    def test_the_text(self):
        args = ["--now", "2026-11-05 09:00", "--offline", EDGE]
        for k, v in self.p.items():
            args += ["--" + k, v]
        code, out, _ = quiet(preflight.main, args)
        self.assertTrue(out.startswith("Pre-flight 2026-11-05 at 09:00 IST: GO\n  calendar  ok   NSE session"))


class AutopilotGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "five.txt")
        with open(self.wl, "w") as fh:
            fh.write("\n".join(FIVE) + "\n")
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("forward", "state", "portfolio", "config",
                                                                 "sectors", "events", "surveillance", "preflight",
                                                                 "limits-state")}
        with open(self.p["surveillance"], "w") as fh:
            json.dump({"symbols": {}, "updated": "2026-11-05"}, fh)
        with open(self.p["config"], "w") as fh:
            json.dump({"budget": 1000000, "max_same_side_risk_inr": 0, "max_entries_per_sector": 0,
                       "max_charges_pct_of_risk": 0}, fh)
        quiet(forward.main, ["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05", "--expect-low",
                             "-0.1", "--file", self.p["forward"], "--today", "2026-11-05"])

    def tearDown(self):
        shutil.rmtree(self.d)

    def pass_at(self, hhmm):
        args = ["run", "--once", "--now", "2026-11-05 " + hhmm, "--offline", EDGE, "--watchlist", self.wl]
        for k, v in self.p.items():
            args += ["--" + k, v]
        return quiet(autopilot.main, args)

    def test_no_go_means_no_new_trades(self):
        with open(self.p["events"], "w") as fh:
            json.dump({"events": [{"symbol": "MARKET", "date": "2026-11-05", "what": "RBI policy"}]}, fh)
        out = "".join(self.pass_at(t)[1] for t in ("09:35", "09:40", "09:45"))
        self.assertIn("Pre-flight 2026-11-05 at 09:35 IST: NO-GO", out)
        self.assertIn("NO-GO this morning", out)
        self.assertNotIn("BUY", out)

    def test_the_mornings_limits_hold_in_the_autopilot(self):
        self.pass_at("09:30")                                     # the pre-flight records a 500 max loss
        with open(self.p["config"]) as fh:
            c = json.load(fh)
        c["max_loss_per_trade_inr"] = 5000                       # raised mid-morning
        with open(self.p["config"], "w") as fh:
            json.dump(c, fh)
        out = self.pass_at("09:40")[1]
        self.assertIn("max_loss_per_trade_inr was loosened today (to 5000); this morning's 500 holds", out)
        self.assertIn("BUY 41 INFY.NS at 1160.60", out)          # sized at 500, not 5,000
        self.assertNotIn("loosened", self.pass_at("09:45")[1])   # said once a day


if __name__ == "__main__":
    unittest.main()
