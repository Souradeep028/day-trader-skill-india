"""trader-broker's autopilot.py, replayed pass by pass through 5 November 2026 on the edge report's real fixtures
(five Nifty 50 stocks and Nifty 50: Yahoo's 5-minute bars)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

autopilot = load("trader-broker", "autopilot")
forward = load("trader-broker", "forward")
ticket = load("trader-broker", "ticket")
EDGE = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")
DAY = "2026-11-05"


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class Autopilot(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "five.txt")
        with open(self.wl, "w") as fh:
            fh.write("\n".join(FIVE) + "\n")
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("forward", "state", "portfolio", "config",
                                                                 "sectors", "events", "surveillance")}
        with open(self.p["surveillance"], "w") as fh:
            json.dump({"symbols": {}, "updated": DAY}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def config(self, **c):
        with open(self.p["config"], "w") as fh:
            json.dump(dict({"budget": 100000}, **c), fh)

    def start(self, rule="orb15 long tight 2R none"):
        quiet(forward.main, ["start", "--rule", rule, "--expect-r", "0.05", "--expect-low", "-0.1",
                             "--file", self.p["forward"], "--today", DAY])

    def pass_at(self, hhmm, day=DAY):
        args = ["run", "--once", "--now", "%s %s" % (day, hhmm), "--offline", EDGE, "--watchlist", self.wl]
        for k, v in self.p.items():
            args += ["--" + k, v]
        return quiet(autopilot.main, args)

    def whole_day(self):
        lines = []
        for m in range(9 * 60 + 20, 15 * 60 + 21, 5):
            code, out, err = self.pass_at("%02d:%02d" % divmod(m, 60))
            self.assertEqual(code, 0, err)
            lines += [x for x in out.splitlines() if "nothing to do" not in x]
        return lines

    def test_it_trades_exactly_what_the_shadow_replays(self):
        # with the account limits that the shadow does not model switched off, every 5-minute pass of the day
        # gives the shadow's three trades, to the paisa
        self.config(budget=1000000, max_same_side_risk_inr=0, max_entries_per_sector=0, max_charges_pct_of_risk=0,
                    max_daily_loss_inr=0)
        self.start()
        lines = self.whole_day()
        self.assertEqual(lines[0], "2026-11-05 9:40: BUY 41 INFY.NS at 1160.60, stop 1149.80, target 1182.20 "
                                   "(paper, autopilot)")
        self.assertIn("2026-11-05 15:10: SELL 41 INFY.NS at 1164.80 (flat-by, paper, autopilot)", lines)
        with open(self.p["state"]) as fh:
            mine, still = ticket.round_trips(json.load(fh), DAY, DAY, paper=True)
        self.assertEqual(still, [])
        quiet(forward.main, ["shadow", "--date", DAY, "--symbols", ",".join(FIVE), "--offline", EDGE,
                             "--file", self.p["forward"], "--today", DAY, "--config", self.p["config"]])
        with open(self.p["forward"]) as fh:
            shadow = json.load(fh)["shadow"]["sessions"][DAY]
        self.assertEqual(sorted((t["symbol"], t["net"]) for t in mine), sorted((t["symbol"], t["net"]) for t in shadow))
        self.assertEqual(len(mine), 3)
        # the trades carry their stop, so the journal measures R, and the approval says who said yes
        self.assertEqual(sorted(round(t["R"], 2) for t in mine), sorted(round(t["R"], 2) for t in shadow))
        with open(self.p["state"]) as fh:
            replies = {r.get("reply") for r in json.load(fh)["orders"] if r.get("status") == "approved"}
        self.assertEqual(replies, {"autopilot (paper)"})

    def test_the_risk_checks_still_apply(self):
        # the pack's own limits: after INFY, the ₹1,00,000 account has no cash left for TCS, and SBIN's charges
        # are 20 % of its risk. Each block is logged once, not on every pass
        self.config()
        self.start()
        lines = self.whole_day()
        blocks = [x for x in lines if "BLOCKED" in x]
        self.assertEqual([x.split(": ")[1].split(" ")[0] for x in blocks], ["TCS.NS", "SBIN.NS"])
        self.assertIn("cash: needs", blocks[0])
        self.assertIn("charges: ", blocks[1])
        with open(self.p["state"]) as fh:
            mine, _ = ticket.round_trips(json.load(fh), DAY, DAY, paper=True)
        self.assertEqual([(t["symbol"], t["net"]) for t in mine], [("INFY.NS", 121.5)])

    def test_a_short_rule_needs_allow_short(self):
        self.config()
        self.start("orb15 short tight 2R none")
        lines = self.whole_day()
        self.assertTrue(lines and all("short: selling more" in x for x in lines if "BLOCKED" in x), lines)

    def test_paper_only(self):
        self.config(paper=False)
        self.start()
        code, _, err = self.pass_at("10:00")
        self.assertEqual(code, 1)
        self.assertIn("The autopilot trades paper only", err)

    def test_closed_days_and_hours(self):
        self.config()
        self.start()
        self.assertEqual(self.pass_at("10:00", day="2026-11-10")[1],
                         "2026-11-10: no session today (NSE holiday: Diwali Balipratipada).\n")
        self.assertIn("has not closed a bar yet", self.pass_at("09:17")[1])
        self.assertEqual(autopilot.closed_bars(autopilot.check.to_ist(autopilot.check.parse_now("2026-11-05 09:40"))), 5)

    def test_a_missed_signal_is_not_chased(self):
        # the first pass at 10:30: INFY's 9:40 signal is ten bars old by then, so it is let go
        self.config(budget=1000000, max_same_side_risk_inr=0, max_entries_per_sector=0, max_charges_pct_of_risk=0)
        self.start()
        code, out, _ = self.pass_at("10:30")
        self.assertNotIn("INFY.NS", out)


if __name__ == "__main__":
    unittest.main()
