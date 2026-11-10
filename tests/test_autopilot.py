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
                                                                 "sectors", "events", "surveillance", "preflight",
                                                                 "limits-state")}
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
        # nobody ran the pre-flight this morning, so the first pass (9:20) does, and it says GO
        self.assertEqual(lines[0], "Pre-flight 2026-11-05 at 09:20 IST: GO")
        trades = [x for x in lines if x.startswith("2026-11-05 ")]
        self.assertEqual(trades[0], "2026-11-05 9:40: BUY 41 INFY.NS at 1160.60, stop 1149.80, target 1182.20 "
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


class Hardening(unittest.TestCase):
    """The fixes for a machine running unattended: unfinished bars, live-price fills, one pass at a time, and a
    heartbeat the reports read."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "infy.txt")
        with open(self.wl, "w") as fh:
            fh.write("INFY.NS\n")
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("forward", "state", "portfolio", "config",
                                                                 "sectors", "events", "surveillance", "preflight",
                                                                 "limits-state", "heartbeat")}
        self.p["lock"] = os.path.join(self.d, "autopilot.lock")
        with open(self.p["surveillance"], "w") as fh:
            json.dump({"symbols": {}, "updated": DAY}, fh)
        with open(self.p["config"], "w") as fh:
            json.dump({"budget": 1000000, "max_charges_pct_of_risk": 0}, fh)
        quiet(forward.main, ["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05", "--expect-low",
                             "-0.1", "--file", self.p["forward"], "--today", DAY])

    def tearDown(self):
        shutil.rmtree(self.d)

    def pass_at(self, hhmm, offline=EDGE):
        args = ["run", "--once", "--now", "%s %s" % (DAY, hhmm), "--offline", offline, "--watchlist", self.wl]
        for k, v in self.p.items():
            args += ["--" + k, v]
        return quiet(autopilot.main, args)

    def test_a_bar_yahoo_has_not_finished_does_not_count(self):
        text = autopilot.edge.read_offline(EDGE, "INFY.NS")
        doc = json.loads(text)
        now = autopilot.check.parse_now("2026-11-05 09:40:30")
        n_clock = autopilot.closed_bars(autopilot.check.to_ist(now))
        self.assertEqual(n_clock, 5)                                       # 9:15 to 9:35 bars, closed by 9:40
        self.assertEqual(autopilot.settled(text, now, n_clock, DAY), 5)    # the saved chart runs to 9 November
        # Yahoo's last trade at 9:39:30: the 9:35 bar (closing 9:40) is still being filled in
        doc["chart"]["result"][0]["meta"]["regularMarketTime"] = 1793851770     # 9:15 IST is 1793850300
        self.assertEqual(autopilot.settled(json.dumps(doc), now, n_clock, DAY), 4)
        # no data time at all: a minute's grace after the close
        del doc["chart"]["result"][0]["meta"]["regularMarketTime"]
        self.assertEqual(autopilot.settled(json.dumps(doc), autopilot.check.parse_now("2026-11-05 09:40:30"),
                                           n_clock, DAY), 4)
        self.assertEqual(autopilot.settled(json.dumps(doc), autopilot.check.parse_now("2026-11-05 09:41:00"),
                                           n_clock, DAY), 5)

    def test_a_late_pass_fills_at_the_price_then(self):
        # the first pass at 9:45: the 9:40 signal is still fresh, but the price has moved from 1160.50 to the
        # 9:40 bar's close 1164.00: filled there a tick worse, 1164.10, and sized again to the same max loss:
        # risk 14.30 a share, 32 x 14.30 = 457.60 + 39.30 of charges = 496.90 (33 would be 512.40)
        code, out, err = self.pass_at("09:45")
        self.assertEqual(code, 0, err)
        self.assertIn("BUY 32 INFY.NS at 1164.10, stop 1149.80, target 1182.20 (paper, autopilot; signal 1160.60, "
                      "the price had moved on)", out)
        with open(self.p["state"]) as fh:
            leg = json.load(fh)["paper_legs"][0]
        self.assertEqual((leg["signal"], leg["entry"], leg["slippage"]), (1160.6, 1164.1, 3.5))

    def test_one_pass_at_a_time(self):
        import fcntl
        with open(self.p["lock"], "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, _ = self.pass_at("09:45")
        self.assertEqual(code, 0)
        self.assertIn("another autopilot pass is still running: this one is skipped", out)
        self.assertFalse(os.path.exists(self.p["state"]))                  # it touched nothing
        self.assertIn("BUY", self.pass_at("09:45")[1])                       # free again

    def test_the_heartbeat(self):
        self.pass_at("09:40")
        self.pass_at("09:45")
        with open(self.p["heartbeat"]) as fh:
            hb = json.load(fh)
        self.assertEqual((hb["date"], hb["passes"], hb["last_pass"], hb["errors"]), (DAY, 2, "09:45", []))
        with open(self.p["config"], "w") as fh:
            json.dump({"budget": 1000000, "paper": False}, fh)
        self.assertEqual(self.pass_at("09:50")[0], 1)
        with open(self.p["heartbeat"]) as fh:
            hb = json.load(fh)
        self.assertEqual((hb["passes"], hb["last_ok"], hb["errors"][0]["at"]), (3, "09:45", "09:50"))
        self.assertIn("paper only", hb["errors"][0]["error"])

    def test_the_reports_read_the_heartbeat(self):
        daily = load("trader-broker", "daily")
        stopped = daily.autopilot_part({"date": DAY, "passes": 20, "last_pass": "11:05", "errors": [
            {"at": "11:05", "error": "Yahoo did not answer"}]}, DAY)
        self.assertIn("Autopilot: 20 passes, the last at 11:05, 1 failed (the last at 11:05: Yahoo did not answer).",
                      stopped)
        self.assertIn("It stopped before the 15:10 flat-by", stopped)
        self.assertIn("did NOT run today", daily.autopilot_part({"date": "2026-11-04", "passes": 70}, DAY))
        self.assertIsNone(daily.autopilot_part(None, DAY))
        pf = load("trader-broker", "preflight")
        doc = pf.run_checks(autopilot.check.parse_now("2026-11-06 09:00"), {"budget": 100000}, None, None, None,
                            None, {"symbols": {}, "updated": "2026-11-06"}, autopilot.edge.read_offline(EDGE, "^NSEI"),
                            self.d, {"date": DAY, "passes": 20, "last_pass": "11:05", "errors": []})
        self.assertTrue(any("the autopilot's last pass on 2026-11-05 was at 11:05, before the 15:10 flat-by" in x
                            for x in doc["notes"]))


if __name__ == "__main__":
    unittest.main()
