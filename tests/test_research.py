"""trader-timemachine's research.py: a vendor's 1-minute CSV rolled up to the 5-minute grid, fills a minute
after the signal, point-in-time membership, walk-forward folds, the selection test and the holdout. Real
5-minute bars of five Nifty 50 stocks (the edge fixtures, to 6 November 2026), and 1-minute files built from them."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

rs = load("trader-timemachine", "research")
edge = load("trader-timemachine", "edge")
EDGE = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")
HYPS = [{"name": "orb30 short wide", "rule": "orb30 short wide 2R low_cost", "why": "test"},
        {"name": "orb30 long wide", "rule": "orb30 long wide 2R low_cost", "why": "test"}]


def minute_csv(sym):
    """A 1-minute CSV whose roll-up is exactly the fixture's 5-minute bars: (o, h, min(o,c), o), (o, max(o,c), l, c),
    then three minutes at the close; the volume in the first minute. Times in India time, no offset."""
    days = edge.sessions_of(edge.read_offline(EDGE, sym), sym)
    out = ["datetime,open,high,low,close,volume"]
    for d in sorted(days):
        for slot, (o, h, l, c, v) in sorted(days[d].items()):
            start = datetime.datetime.fromisoformat(d) + datetime.timedelta(minutes=edge.OPEN_MIN + 5 * slot)
            mins = [(o, h, min(o, c), o, v), (o, max(o, c), l, c, 0)] + [(c, c, c, c, 0)] * 3
            for i, (a, b, x, y, vol) in enumerate(mins):
                out.append("%s,%.2f,%.2f,%.2f,%.2f,%d" % ((start + datetime.timedelta(minutes=i)).strftime(
                    "%Y-%m-%d %H:%M:%S"), a, b, x, y, vol))
    return "\n".join(out) + "\n", days


def quiet(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rs.main(argv)
    return code, out.getvalue(), err.getvalue()


class Data(unittest.TestCase):
    def test_times(self):
        self.assertEqual(rs.parse_time("2026-11-06 09:15:00"), rs.parse_time("2026-11-06T03:45:00Z"))
        self.assertEqual(rs.parse_time("2026-11-06 09:15:00+05:30"), rs.parse_time("1793936700"))
        self.assertEqual(rs.parse_time("1793936700000"), 1793936700)                      # milliseconds
        self.assertEqual(rs.parse_time("06-11-2026 09:15"), rs.parse_time("2026-11-06 09:15"))
        with self.assertRaises(rs.ResearchError):
            rs.parse_time("yesterday")

    def test_a_vendor_s_1_minute_file_rolls_up_to_the_same_5_minute_bars(self):
        text, days = minute_csv("INFY.NS")
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "INFY.NS.csv"), "w") as fh:
                fh.write(text)
            charts, minutes, kind = rs.load_folder(d)
            self.assertEqual(kind, "1m")
            got = edge.sessions_of(charts["INFY.NS"], "INFY.NS")
            self.assertEqual(got, days)
            day = sorted(days)[-1]
            # the minute opens are kept for the fills: 9:21 is the second minute of the 9:20 bar, its open
            self.assertEqual(minutes["INFY.NS"][day][9 * 60 + 21], days[day][1][0])
        finally:
            shutil.rmtree(d)

    def test_the_header_is_checked(self):
        with self.assertRaises(rs.ResearchError):
            rs.read_csv("when,price\n2026-11-06 09:15,1\n", "X.NS")
        rows = rs.read_csv("Date,Time,Open,High,Low,Close,Volume\n2026-11-06,09:15:00,1,2,0.5,1.5,10\n", "X.NS")
        self.assertEqual(rows, [[rs.parse_time("2026-11-06 09:15"), 1.0, 2.0, 0.5, 1.5, 10.0]])

    def test_rules_are_checked(self):
        self.assertEqual(rs.parse_rule("orb30 short wide 2R low_cost+with_nifty")[1], "low_cost+with_nifty")
        for bad in ("orb30 short wide", "orb99 short wide 2R", "orb30 short wide 2R magic"):
            with self.assertRaises(rs.ResearchError):
                rs.parse_rule(bad)


class Fills(unittest.TestCase):
    def test_a_minute_after_the_signal(self):
        days = edge.sessions_of(edge.read_offline(EDGE, "INFY.NS"), "INFY.NS")
        cfg = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "max_cost_pct": 15.0,
               "first_slot": edge.hhmm_slot("09:30", "x"), "last_entry_slot": edge.hhmm_slot("14:45", "x"),
               "flat_slot": edge.hhmm_slot("15:10", "x"), "expiry": set()}
        sess = edge.prepare(days, {}, {}, dict(cfg, dates=set(days)))
        s, e0 = next((s, e) for s, e in ((s, edge.entry_at(s, "orb30", "short", "tight", 2.0, cfg)) for s in sess) if e)
        close_min = edge.OPEN_MIN + 5 * (e0["slot"] + 1)
        s = dict(s, minutes={close_min + 1: 1000.00})
        e = edge.entry_at(s, "orb30", "short", "tight", 2.0, dict(cfg, latency_min=1, slippage_bp=3.0))
        self.assertEqual(e["entry"], edge.tick_down(1000.00 * (1 - 3e-4) - s["tick"], s["tick"]))
        self.assertEqual((e["slot"], e["delayed"], e["time"]), (e0["slot"] + 1, True,
                                                                  "%d:%02d" % divmod(close_min + 1, 60)))
        # no minute bar there: no trade, rather than a made-up fill
        self.assertIsNone(edge.entry_at(dict(s, minutes={0: 1.0}), "orb30", "short", "tight", 2.0,
                                        dict(cfg, latency_min=1)))


class Walk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.charts, cls.minutes, _ = rs.load_folder(EDGE)

    def test_folds(self):
        ds = ["d%02d" % i for i in range(53)]
        self.assertEqual([len(f) for f in rs.folds_of(ds, 15)], [15, 15, 15, 8])
        self.assertEqual([len(f) for f in rs.folds_of(ds[:47], 20)], [20, 27])             # a short tail joins

    def test_the_report_on_real_bars(self):
        doc = rs.run_research(self.charts, self.minutes, HYPS, {"fold_sessions": 15, "bootstrap": 200})
        self.assertEqual((doc["hypotheses"], doc["significance"], doc["data"]), (2, 0.025, "5-minute"))
        self.assertTrue(doc["survivorship"])
        r = doc["rows"][0]
        self.assertEqual(sum(f["trades"] for f in r["folds"]), r["all"]["trades"])
        self.assertFalse(any(x["passed"] for x in doc["rows"]))
        sel = doc["selection"]
        self.assertEqual(sum(p["trades"] for p in sel["picks"]), sel["all"]["trades"])
        self.assertTrue(all(p["fold"] >= 1 for p in sel["picks"]))                       # never the first fold
        self.assertIn("None of the 2 hypotheses passed", doc["verdict"])
        self.assertIn("No --members file", doc["verdict"])

    def test_members_and_the_holdout(self):
        d = tempfile.mkdtemp()
        try:
            m = os.path.join(d, "members.csv")
            with open(m, "w") as fh:
                fh.write("symbol,from,to\nTCS.NS,2020-01-01,\nINFY.NS,2020-01-01,2026-09-30\n")
            members = rs.read_members(m)
            doc = rs.run_research(self.charts, self.minutes, HYPS, {"fold_sessions": 15, "bootstrap": 100},
                                  members, holdout_from="2026-10-15")
            self.assertEqual(doc["symbols"], 5)
            self.assertFalse(doc["survivorship"])
            self.assertLess(doc["to"], "2026-10-15")
            self.assertEqual(doc["holdout"]["included"], False)
            # trades only where the stock was a member: none for HDFCBANK, RELIANCE or SBIN, no INFY after September
            every = rs.run_research(self.charts, self.minutes, HYPS, {"fold_sessions": 15, "bootstrap": 100},
                                    None, holdout_from="2026-10-15")
            self.assertLess(doc["rows"][0]["all"]["trades"], every["rows"][0]["all"]["trades"])
            self.assertEqual(doc["rows"][0]["baseline"]["trades"] < every["rows"][0]["baseline"]["trades"], True)
            full = rs.run_research(self.charts, self.minutes, HYPS, {"fold_sessions": 15, "bootstrap": 100},
                                   members, holdout_from="2026-10-15", final=True)
            self.assertGreater(full["to"], "2026-10-15")
            # the holdout is recorded the first time it is looked at, and the second look says so
            f = os.path.join(d, "holdout.json")
            self.assertIsNone(rs.holdout_note(f, "2026-10-15", "2026-11-14"))
            self.assertIn("already looked at on 2026-11-14", rs.holdout_note(f, "2026-10-15", "2026-11-15"))
        finally:
            shutil.rmtree(d)

    def test_membership_drops_the_sessions(self):
        self.assertTrue(rs.member_on(None, "X.NS", "2026-01-01"))
        mem = {"INFY.NS": [("2020-01-01", "2026-09-30")]}
        self.assertTrue(rs.member_on(mem, "INFY.NS", "2026-09-30"))
        self.assertFalse(rs.member_on(mem, "INFY.NS", "2026-10-01"))
        self.assertFalse(rs.member_on(mem, "TCS.NS", "2026-10-01"))

    def test_the_command(self):
        d = tempfile.mkdtemp()
        try:
            h = os.path.join(d, "h.json")
            with open(h, "w") as fh:
                json.dump(HYPS, fh)
            code, out, err = quiet(["run", "--data", EDGE, "--hypotheses", h, "--fold-sessions", "20",
                                    "--bootstrap", "100", "--write", os.path.join(d, "r.json")])
            self.assertEqual(code, 0, err)
            self.assertIn("Research: 2 hypotheses, 5 stocks", out)
            self.assertIn("The selection, walk-forward", out)
            with open(os.path.join(d, "r.json")) as fh:
                self.assertEqual(json.load(fh)["hypotheses"], 2)
            # the shipped hypotheses are a short list, each with its reason
            with open(rs.HYPOTHESES) as fh:
                shipped = json.load(fh)
            self.assertLessEqual(len(shipped), 12)
            for x in shipped:
                rs.parse_rule(x["rule"])
                self.assertGreater(len(x["why"]), 30, x["name"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()


class Check(unittest.TestCase):
    def rows(self, day, start, n=75, step=300, px=100.0):
        t0 = rs.parse_time("%s 09:15" % day)
        return [[t0 + step * i, px, px + 0.5, px - 0.5, px, 10] for i in range(n)]

    def test_faults_are_found_and_refused(self):
        good = self.rows("2026-11-05", "09:15") + self.rows("2026-11-06", "09:15")
        split = self.rows("2026-11-05", "09:15", px=2000.0) + self.rows("2026-11-06", "09:15", px=1000.0)
        short = self.rows("2026-11-05", "09:15") + self.rows("2026-11-06", "09:15", n=30)
        spike = self.rows("2026-11-06", "09:15")
        spike[10] = [spike[10][0], 100.0, 120.0, 99.5, 100.0, 10]                 # one bar 20 % up, then back
        dup = self.rows("2026-11-06", "09:15")
        dup.insert(5, list(dup[4]))
        sat = self.rows("2026-11-07", "09:15")
        chk = rs.check_data({"GOOD.NS": good, "SPLIT.NS": split, "SHORT.NS": short, "SPIKE.NS": spike,
                             "DUP.NS": dup, "SAT.NS": sat}, members={"GOOD.NS": [], "GONE.NS": []},
                            holidays=set())
        bad = chk["bad"]
        self.assertNotIn(("GOOD.NS", "2026-11-06"), bad)
        self.assertIn("unadjusted split or bonus", bad[("SPLIT.NS", "2026-11-06")])
        self.assertIn("30 of about 75 bars", bad[("SHORT.NS", "2026-11-06")])
        self.assertIn("a bad print at 10:05", bad[("SPIKE.NS", "2026-11-06")])
        self.assertEqual(chk["symbols"]["DUP.NS"]["duplicates"], 1)
        self.assertEqual(chk["symbols"]["SAT.NS"]["closed_days"], ["2026-11-07"])
        self.assertEqual(chk["missing_members"], ["GONE.NS"])
        text = "\n".join(rs.check_lines(chk))
        self.assertIn("split-like jumps on 2026-11-06 (x0.50)", text)

    def test_the_fixtures(self):
        chk = rs.check_data(rs.raw_rows(EDGE))
        # the stocks are clean; Yahoo's index files end on a half-finished 9 November
        self.assertEqual(sorted(chk["bad"]), [("^INDIAVIX", "2026-11-09"), ("^NSEI", "2026-11-09")])


class Bhavcopy(unittest.TestCase):
    OLD = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
           "INFY,EQ,1500,1520,1490,1510,1510,1495,100000,1,%s,1,INE009A01021,\n"
           "INFY,BE,1,1,1,1,1,1,1,1,%s,1,X,\n")
    NEW = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
           "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
           "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,"
           "Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
           "%s,%s,CM,NSE,STK,1594,INE009A01021,INFY,EQ,,,,,INFOSYS LTD,%s,%s,%s,%s,%s,%s,,%s,,,200000,1,1,F1,1,,,,,\n")

    def test_both_formats_and_a_split_adjusted_away(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "out")
        try:
            with open(os.path.join(d, "a.csv"), "w") as fh:
                fh.write(self.OLD % ("03-NOV-2026", "03-NOV-2026"))
            # a 1:2 split on 4 November: NSE's previous close is the adjusted 755, not 1,510
            with open(os.path.join(d, "b.csv"), "w") as fh:
                fh.write(self.NEW % ("2026-11-04", "2026-11-04", 760, 770, 750, 765, 765, 755, 765))
            got = rs.read_bhavcopy(self.OLD % ("03-NOV-2026", "03-NOV-2026"))
            self.assertEqual(got[("INFY", "EQ")], ("2026-11-03", 1500.0, 1520.0, 1490.0, 1510.0, 1495.0, 100000.0))
            self.assertIn(("INFY", "BE"), got)
            charts, actions = rs.bhavcopy_charts(d)
            self.assertEqual(actions, {"INFY.NS": [("2026-11-04", 0.5)]})
            swing = load("trader-timemachine", "swing")
            bars = swing.daily_bars(charts["INFY.NS"], "INFY.NS")
            self.assertEqual(bars, [("2026-11-03", 750.0, 760.0, 745.0, 755.0), ("2026-11-04", 760.0, 770.0, 750.0,
                                                                                 765.0)])
            self.assertEqual(rs.main(["bhavcopy", "--data", d, "--out", out]), 0)
            self.assertTrue(os.path.exists(os.path.join(out, "INFY.NS.json")))
            with self.assertRaises(rs.ResearchError):
                rs.read_bhavcopy("a,b\n1,2\n")
        finally:
            shutil.rmtree(d)


class LateSetup(unittest.TestCase):
    def session(self, nifty_move_pct):
        B = [(100.0, 100.5, 99.5, 100.0, 10)] * 75
        n_open = 20000.0
        nifty = {k: (n_open, n_open, n_open, n_open * (1 + nifty_move_pct / 100.0), 0) for k in range(75)}
        return {"bars": B, "nifty": nifty, "n_open": n_open, "tick": 0.05}

    def test_with_the_market_into_the_close(self):
        cfg = {"first_slot": 2, "last_entry_slot": 65, "flat_slot": 70}
        k, tight, wide, fill = edge.trigger(self.session(0.8), "late", True, cfg)
        self.assertEqual((k, tight, wide, fill), (62, 99.5, 99.5, None))
        self.assertIsNone(edge.trigger(self.session(0.3), "late", True, cfg))      # under the 0.5 % move
        self.assertIsNone(edge.trigger(self.session(0.8), "late", False, cfg))     # no short on an up day
        self.assertEqual(edge.trigger(self.session(-0.8), "late", False, cfg)[0], 62)
        self.assertNotIn("late", edge.SETUPS)                                     # the grid stays 1,536
        self.assertEqual(rs.parse_rule("late long tight 1R")[0]["setup"], "late")


class PowerAndPlan(unittest.TestCase):
    def test_a_strong_planted_edge_is_found_and_the_nulls_are_not(self):
        p = rs.power(n_stocks=3, n_sessions=130, drift_bp=2.0, reps=2, fold_sessions=40, bootstrap=100)
        self.assertGreater(p["planted_true_avg_R"], 0.2)
        self.assertEqual((p["planted_pass_pct"], p["null_pass_pct"]), (100.0, 0.0))
        self.assertIn("planted edge passed: 100 % of runs", "\n".join(rs.power_lines(p)))

    def test_the_synthetic_market_holds_still(self):
        # the planted edge must be the same size early and late, or the power test measures the wrong thing
        doc = rs.run_research(rs.synthetic(6, 400, 1.0, seed=3), {}, rs.POWER_HYPOTHESES[:1],
                              {"fold_sessions": 200, "bootstrap": 20})
        a, b = [f["avg_R"] for f in doc["rows"][0]["folds"]]
        self.assertLess(abs(a - b), 0.12)

    def test_a_frozen_plan_runs_with_its_own_settings(self):
        d = tempfile.mkdtemp()
        try:
            plan = os.path.join(d, "plan.json")
            h = os.path.join(d, "h.json")
            with open(h, "w") as fh:
                json.dump(HYPS[:1], fh)
            code, out, err = quiet(["plan", "--hypotheses", h, "--slippage-bp", "5", "--fold-sessions", "20",
                                    "--file", plan])
            self.assertEqual(code, 0, err)
            self.assertIn("1 hypotheses", out)
            self.assertEqual(quiet(["plan", "--hypotheses", h, "--file", plan])[0], 1)     # one at a time
            with open(plan) as fh:
                doc = json.load(fh)
            self.assertEqual((doc["settings"]["slippage_bp"], len(doc["hypotheses_sha"])), (5.0, 12))
            code, out, err = quiet(["run", "--data", EDGE, "--plan", plan, "--bootstrap", "50", "--slippage-bp", "0"])
            self.assertEqual(code, 0, err)
            self.assertIn("Research: 1 hypotheses", out)
            self.assertIn("5 bp of slippage", out)                                 # the plan's, not the flag's
            self.assertIn("Plan frozen", out)
            self.assertIn("break-even", out)
        finally:
            shutil.rmtree(d)
