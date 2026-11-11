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


class Gaps(unittest.TestCase):
    """Bad prints, a stock whose data stops coming, and the daily report keeping the paper verdict."""
    setUp, tearDown, pass_at = Hardening.setUp, Hardening.tearDown, Hardening.pass_at

    def test_bad_prints(self):
        b = {0: (100.0, 100.5, 99.5, 100.0, 9), 1: (100.0, 100.2, 99.8, 100.1, 9), 2: (100.1, 100.1, 50.0, 52.0, 9),
             3: (100.2, 100.4, 100.0, 100.3, 9)}
        clean, n, bad = autopilot.clean_bars(b, 4)
        self.assertEqual((bad, clean[2], n), ([2], (100.1, 100.1, 100.1, 100.1, 0), 4))   # 50 then back to 100
        # the same jump on the last finished bar waits for the next one
        clean, n, bad = autopilot.clean_bars({k: b[k] for k in (0, 1, 2)}, 3)
        self.assertEqual((bad, n), ([], 2))
        # a real move stays: the next bar holds the new level
        real = dict(b)
        real[3] = (52.0, 53.0, 51.5, 52.5, 9)
        self.assertEqual(autopilot.clean_bars(real, 4)[2], [])

    def test_a_held_stock_with_no_data(self):
        self.assertIn("BUY", self.pass_at("09:41")[1])                    # INFY bought on the 9:35 bar
        blind = os.path.join(self.d, "blind")
        os.makedirs(blind)
        for f in os.listdir(EDGE):
            if not f.startswith("INFY"):
                shutil.copy(os.path.join(EDGE, f), blind)
        out = self.pass_at("09:46", offline=blind)[1] + self.pass_at("09:51", offline=blind)[1]
        self.assertIn("WARNING 2026-11-05 09:46: no data for INFY.NS: the open position was NOT checked this pass "
                      "(1 in a row)", out)
        self.assertIn("(2 in a row)", out)
        with open(self.p["heartbeat"]) as fh:
            hb = json.load(fh)
        self.assertEqual(len(hb["errors"]), 2)
        self.assertNotEqual(hb.get("last_ok"), "09:51")
        # past the flat-by with still no data: closed at the last price seen, a tick worse, and flagged
        out = self.pass_at("15:16", offline=blind)[1]
        self.assertIn("flat-by, no data: closed at the last price seen", out)
        with open(self.p["state"]) as fh:
            legs = json.load(fh)["paper_legs"]
        self.assertEqual(legs[0]["status"], "filled")

    def test_the_daily_report_keeps_the_paper_verdict(self):
        for t in ("09:41", "15:11"):
            self.pass_at(t)
        daily = load("trader-broker", "daily")
        code, out, err = quiet(daily.main, ["--date", DAY, "--offline", EDGE, "--watchlist", self.wl, "--forward",
                                            self.p["forward"], "--state", self.p["state"], "--config", self.p["config"],
                                            "--heartbeat", self.p["heartbeat"], "--out", os.path.join(self.d, "d.md"),
                                            "--sip", os.path.join(self.d, "none"), "--events", os.path.join(self.d, "none"),
                                            "--surveillance", self.p["surveillance"]])
        self.assertEqual(code, 0, err)
        self.assertIn("- Paper trades since the start (the autopilot's and yours): 1, average", out)
        with open(self.p["forward"]) as fh:
            v = json.load(fh)["verdicts"]
        self.assertEqual((v["paper"]["date"], v["paper"]["trades"]), (DAY, 1))
        self.assertIn("shadow", v)


class Accuracy(unittest.TestCase):
    """Paper fills as a broker would make them: 1-minute order of events, targets that trade through, gaps,
    and no entries on stale data."""
    LEG = {"stop": 99.0, "target": 102.0}

    def test_the_minute_that_came_first_decides(self):
        bar5 = (100.0, 102.5, 98.5, 100.0, 9)                     # touched both: the 5-minute bar cannot say
        target_first = {10: (100.0, 101.0, 99.8, 100.9), 11: (100.9, 102.2, 100.8, 102.1),
                        12: (102.1, 102.1, 98.5, 99.0), 13: (99.0, 99.5, 98.9, 99.2), 14: (99.2, 100.0, 99.1, 100.0)}
        self.assertEqual(autopilot.exit_in(bar5, 2, target_first, self.LEG, True, 1, 0.05), ("target", 102.0, 11))
        stop_first = dict(target_first)
        stop_first[11] = (100.9, 101.0, 98.9, 99.1)
        self.assertEqual(autopilot.exit_in(bar5, 2, stop_first, self.LEG, True, 1, 0.05), ("stop", 98.95, 11))
        # without 1-minute bars: the stop, the cautious call
        self.assertEqual(autopilot.exit_in(bar5, 2, {}, self.LEG, True, 1, 0.05), ("stop", 98.95, None))

    def test_a_target_has_to_trade_through(self):
        touch = (100.0, 102.0, 99.5, 101.5, 9)                     # reached 102.00, never 102.05
        self.assertIsNone(autopilot.exit_in(touch, 0, {}, self.LEG, True, 1, 0.05))
        through = (100.0, 102.05, 99.5, 101.5, 9)
        self.assertEqual(autopilot.exit_in(through, 0, {}, self.LEG, True, 1, 0.05), ("target", 102.0, None))
        # a short's target is under it: 98.00, through at 97.95
        short = {"stop": 101.0, "target": 98.0}
        self.assertIsNone(autopilot.exit_in((100.0, 100.5, 98.0, 98.5, 9), 0, {}, short, False, 1, 0.05))
        self.assertEqual(autopilot.exit_in((100.0, 100.5, 97.95, 98.5, 9), 0, {}, short, False, 1, 0.05)[0], "target")

    def test_a_gap_through_the_stop_fills_at_the_minutes_open(self):
        bar5 = (99.5, 99.8, 97.0, 97.5, 9)
        mins = {0: (100.0, 100.1, 99.6, 99.7), 1: (98.0, 98.2, 97.0, 97.4), 2: (97.4, 97.8, 97.2, 97.5),
                3: (97.5, 97.6, 97.3, 97.4), 4: (97.4, 97.6, 97.4, 97.5)}
        # the 9:15 minute stays over 99, the 9:16 minute opens at 98.00: filled there, a tick worse
        self.assertEqual(autopilot.exit_in(bar5, 0, mins, self.LEG, True, 1, 0.05), ("stop", 97.95, 1))

    def test_minutes_of_reads_a_one_minute_chart(self):
        ts = [1793850300 + 60 * k for k in range(3)]                # 9:15, 9:16, 9:17 IST on 5 November
        q = {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3]}
        text = json.dumps({"chart": {"result": [{"meta": {}, "timestamp": ts, "indicators": {"quote": [q]}}]}})
        self.assertEqual(autopilot.minutes_of(text, DAY), {0: (1, 1, 1, 1), 1: (2, 2, 2, 2), 2: (3, 3, 3, 3)})

    def test_no_entries_on_stale_data(self):
        d = tempfile.mkdtemp()
        try:
            wl = os.path.join(d, "infy.txt")
            with open(wl, "w") as fh:
                fh.write("INFY.NS\n")
            p = {k: os.path.join(d, k + ".json") for k in ("forward", "state", "portfolio", "config", "sectors",
                                                           "events", "surveillance", "preflight", "limits-state",
                                                           "heartbeat")}
            with open(p["config"], "w") as fh:
                json.dump({"budget": 1000000, "max_charges_pct_of_risk": 0}, fh)
            with open(p["surveillance"], "w") as fh:
                json.dump({"symbols": {}, "updated": DAY}, fh)
            quiet(forward.main, ["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05", "--file",
                                 p["forward"], "--today", DAY])

            def stale(sym, rng="60d", interval="5m"):
                doc = json.loads(autopilot.edge.read_offline(EDGE, sym))
                doc["chart"]["result"][0]["meta"]["regularMarketTime"] = 1793850300 + 15 * 60   # 9:30, 11 min old
                return json.dumps(doc)
            real_fetch, real_sleep = autopilot.edge.fetch, autopilot.edge._sleep
            autopilot.edge.fetch, autopilot.edge._sleep = stale, (lambda s: None)
            try:
                args = ["run", "--once", "--now", "2026-11-05 09:41", "--watchlist", wl,
                        "--lock", os.path.join(d, "lock")]
                for k, v in p.items():
                    args += ["--" + k, v]
                code, out, err = quiet(autopilot.main, args)
            finally:
                autopilot.edge.fetch, autopilot.edge._sleep = real_fetch, real_sleep
            self.assertEqual(code, 0, err)
            self.assertIn("WARNING 2026-11-05 09:41: Yahoo's prices are stale for INFY.NS (11 min)", out)
            self.assertNotIn("BUY", out)
        finally:
            shutil.rmtree(d)


class Reporting(unittest.TestCase):
    """The late 9:45 fill (1164.10 against the 1160.60 signal, 32 shares) in the journal and the daily report."""
    setUp, tearDown, pass_at = Hardening.setUp, Hardening.tearDown, Hardening.pass_at

    def test_slippage_and_the_execution_gap(self):
        self.pass_at("09:45")
        self.pass_at("15:11")
        with open(self.p["state"]) as fh:
            st = json.load(fh)
        trades, _ = ticket.round_trips(st, DAY, DAY, paper=True)
        # 3.50 a share worse than the signal; the risk at the signal was 1160.60 - 1149.80 = 10.80: 0.324 R;
        # 32 shares x 3.50 = 112
        self.assertEqual(ticket.entry_slippage(st, trades), {"trades": 1, "per_share": 3.5, "R": 0.324, "rupees": 112.0})
        code, out, _ = quiet(ticket.main, ["journal", "--paper", "--state", self.p["state"], "--config", self.p["config"]])
        self.assertIn("Entry slippage (the autopilot's fills against their signals): +3.50 a share, +0.324 R a trade "
                      "on average, ₹112 over 1 trade.", out)
        daily = load("trader-broker", "daily")
        code, out, err = quiet(daily.main, ["--date", DAY, "--offline", EDGE, "--watchlist", self.wl, "--forward",
                                            self.p["forward"], "--state", self.p["state"], "--config", self.p["config"],
                                            "--heartbeat", self.p["heartbeat"], "--out", os.path.join(self.d, "d.md"),
                                            "--sip", os.path.join(self.d, "none"), "--events",
                                            os.path.join(self.d, "none"), "--surveillance", self.p["surveillance"]])
        self.assertEqual(code, 0, err)
        self.assertIn("- Entry slippage: +3.50 a share, +0.324 R a trade on average (₹112 over 1 trades)", out)
        # the shadow took the same INFY trade at 1160.60: the paper one, five minutes later, gave some of it back
        self.assertIn("- Execution gap: paper ", out)
        self.assertIn("on the 1 trades both took", out)


class DataLag(unittest.TestCase):
    def test_the_heartbeat_keeps_the_lag_and_the_report_says_it(self):
        import datetime as _dt
        import tempfile as _tf
        ap, daily = load("trader-broker", "autopilot"), load("trader-broker", "daily")
        d = _tf.mkdtemp()
        p = os.path.join(d, "hb.json")
        t = _dt.datetime(2026, 11, 11, 4, 0)                                   # 9:30 IST
        ap.heartbeat(p, t, None, [], {"median": 40, "worst": 70, "symbols": 51})
        ap.heartbeat(p, t + _dt.timedelta(minutes=5), None, [], {"median": 200, "worst": 260, "symbols": 51})
        ap.heartbeat(p, t + _dt.timedelta(minutes=10), None, [], {"median": 50, "worst": 90, "symbols": 51})
        with open(p) as fh:
            hb = json.load(fh)
        self.assertEqual(hb["lag"], [["09:30", 40, 70], ["09:35", 200, 260], ["09:40", 50, 90]])
        line = daily.autopilot_part(hb, "2026-11-11")
        self.assertIn("Yahoo's data lag: 50 s typical over 3 passes, 260 s at worst; 1 pass over the 3-minute limit "
                      "(no entries then).", line)
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
