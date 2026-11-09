"""trader-timemachine's edge.py. Fixtures in tests/fixtures/trader-timemachine/edge/: the real 60-day
5-minute charts of five Nifty 50 stocks, the Nifty 50 index (^NSEI) and India VIX (^INDIAVIX), fetched
from Yahoo on 8 and 9 November 2026."""

import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

edge = load("trader-timemachine", "edge")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
DIR = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def charts():
    return {s: edge.read_offline(DIR, s) for s in FIVE}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = edge.main(argv)
    return code, out.getvalue(), err.getvalue()


class Pieces(unittest.TestCase):
    def test_the_real_tick(self):
        # Infosys at 1,160 trades in 0.05s: NSE's monthly review had not moved it to 0.10 yet
        self.assertEqual(edge.observed_tick([1160.5, 1164.0, 1151.2, 1152.35] * 4), 0.05)
        self.assertEqual(edge.observed_tick([12750.0, 12751.0, 12749.0] * 5), 1.0)
        self.assertEqual(edge.observed_tick([231.2, 231.21, 231.19] * 5), 0.01)
        self.assertIsNone(edge.observed_tick([1160.5, 1164.0]))           # too few prices to tell
        self.assertEqual(edge.observed_tick([1160.5] * 20), tm.observed_tick([1160.5] * 20))

    def test_sizing_is_trader_budgets(self):
        plan = {"symbol": "INFY.NS", "side": "buy", "entry": 1160.5, "stop": 1149.8, "target": 1181.9}
        qty = size.size_trade(plan, {"budget": 1000000, "max_loss_per_trade_inr": 500, "max_order_inr": 75000,
                                     "max_position_pct": 100, "cash_buffer_pct": 0})["qty"]
        self.assertEqual((qty, edge.size_for(1160.5, 1149.8, 500, 75000)), (41, 41))
        self.assertEqual(edge.size_for(1160.5, 1149.8, 500, 20000), 17)    # the max order caps it
        self.assertEqual(edge.size_for(1160.5, 600.0, 500, 75000), 0)      # the stop is too far for the max loss

    def test_short_sizing(self):
        # a short sale: sold at the entry, bought back at the stop, so the stamp duty is on the buy at the stop
        qty = edge.size_for(1146.4, 1154.5, 500, 75000)
        self.assertEqual(qty, 53)
        self.assertTrue(53 * 8.1 + edge.round_trip(53, 1154.5, 1146.4) <= 500)
        self.assertTrue(54 * 8.1 + edge.round_trip(54, 1154.5, 1146.4) > 500)

    def test_expiry_days(self):
        # Tuesdays, and the Monday when the Tuesday is a holiday (Dussehra, 20 October 2026)
        days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-19", "2026-10-21", "2026-10-26", "2026-10-27",
                "2026-10-28", "2026-11-02"]
        self.assertEqual(sorted(edge.expiry_dates(days)), ["2026-10-13", "2026-10-19", "2026-10-27"])
        self.assertEqual(sorted(edge.expiry_dates(days, weekday=3)), ["2026-10-14", "2026-10-21", "2026-10-28"])

    def test_bootstrap(self):
        up = [{"date": "d%02d" % i, "R": 0.5 + (i % 3) * 0.1} for i in range(40)]
        flat = [{"date": "d%02d" % i, "R": (-1) ** i * 1.0} for i in range(40)]
        a, b = edge.bootstrap(up, 500), edge.bootstrap(flat, 500)
        self.assertTrue(a["p"] < 1e-6 and a["ci95"][0] > 0.5, a)
        self.assertTrue(0.3 < b["p"] < 0.7 and b["ci95"][0] < 0 < b["ci95"][1], b)
        self.assertEqual(edge.bootstrap(up, 500), a)                       # seeded: the same every run
        self.assertIsNone(edge.bootstrap(up[:1]))
        # one big day is one sample, not twenty: twenty winners on one day and losers on the rest
        lumpy = [{"date": "d00", "R": 3.0}] * 20 + [{"date": "d%02d" % i, "R": -0.5} for i in range(1, 20)]
        self.assertTrue(edge.bootstrap(lumpy, 500)["p"] > 0.2)

    def _infy(self):
        with gzip.open(os.path.join(FIX, "trader-timemachine", "INFY.NS-60d5m.json.gz"), "rt") as fh:
            text = fh.read()
        return text, edge.sessions_of(text, "INFY.NS")

    def test_the_trades_match_the_time_machines_history(self):
        # no slippage, entries allowed to the flat-by: the same trades as timemachine.py's history replay,
        # long and short
        text, days = self._infy()
        last = edge.hhmm_slot("15:10", "x")
        cfg = {"slippage_ticks": 0, "first_slot": 3, "last_entry_slot": last, "flat_slot": last,
               "max_loss": 500, "max_order": 1e9, "max_cost_pct": 15, "dates": {d for d in days if d <= "2026-11-04"}}
        sess = edge.prepare(days, {}, {}, cfg)
        for side, tm_side, entry, target in (("long", "buy", 1160.5, 1181.9), ("short", "sell", 1140.0, 1120.4)):
            mine = edge.trades_for("INFY.NS", sess, "orb15", side, "tight", 2.0, cfg)
            plan = tm.normalize_plan({"symbol": "INFY.NS", "side": tm_side, "date": "2026-11-05", "setup": "orb",
                                      "entry": entry, "entry_time": "9:40 AM", "entry_bar": 4,
                                      "opening_range": {"high": 1154.7, "low": 1144.9, "minutes": 15},
                                      "stop": 1149.8, "stop_rule": "or_mid", "target": target, "flat_by": "15:10"})
            hist = tm.time_machine(text, plan, tm.parse_now("2026-11-05 09:45"), replays=10)["history"]["trades"]
            self.assertEqual(len(mine), {"long": 20, "short": 42}[side])
            self.assertEqual([(t["date"], t["entry"], t["stop"], t["target"], t["outcome"], round(t["R_gross"], 2))
                              for t in mine],
                             [(t["date"], t["entry"], t["stop"], t["target"], t["outcome"], t["R"]) for t in hist])

    def _cfg(self):
        return {"slippage_ticks": 1, "first_slot": 3, "last_entry_slot": edge.hhmm_slot("14:45", "x"),
                "flat_slot": edge.hhmm_slot("15:10", "x"), "max_loss": 500, "max_order": 75000, "max_cost_pct": 15}

    def test_a_trade_by_hand(self):
        # The reel's day, INFY on 5 November 2026. Every INFY price that day is a multiple of 0.10 (NSE's
        # November review moved it up after it closed October above 1,000; on 1 October it still traded
        # in 0.05s, at 1022.35), so one tick of slippage is 0.10.
        _, days = self._infy()
        self.assertEqual(edge.observed_tick([x for b in days["2026-11-05"].values() for x in b[:4]]), 0.10)
        self.assertEqual(edge.observed_tick([x for b in days["2026-10-01"].values() for x in b[:4]]), 0.05)
        sess = edge.prepare(days, {}, {}, self._cfg())
        t = [x for x in edge.trades_for("INFY.NS", sess, "orb15", "long", "tight", 2.0, self._cfg())
             if x["date"] == "2026-11-05"][0]
        # bought at the 9:35 bar's close 1160.50 plus a tick; risk 1160.60 - 1149.80 = 10.80; 2R target 1182.20
        self.assertEqual((t["entry"], t["stop"], t["target"], t["qty"]), (1160.6, 1149.8, 1182.2, 41))
        # neither side by 15:10: sold at the 15:05 bar's close 1164.90 less a tick
        self.assertEqual((t["outcome"], t["exit"]), ("close", 1164.8))
        self.assertEqual(t["charges"], edge.round_trip(41, 1160.6, 1164.8))
        self.assertEqual(t["pnl"], round(41 * (1164.8 - 1160.6) - t["charges"], 2))

    def test_a_short_by_hand(self):
        # INFY, 3 November 2026, short the 30-minute range break. The first six bars: high 1161.70 (9:20),
        # low 1147.20 (9:15). The first close under 1147.20 is the 9:55 bar's (closes 10:00) at 1146.50.
        _, days = self._infy()
        b = days["2026-11-03"]
        self.assertEqual((max(b[k][1] for k in range(6)), min(b[k][2] for k in range(6)), b[8][3]),
                         (1161.7, 1147.2, 1146.5))
        self.assertTrue(all(b[k][3] >= 1147.2 for k in range(6, 8)))
        sess = edge.prepare(days, {}, {}, self._cfg())
        t = [x for x in edge.trades_for("INFY.NS", sess, "orb30", "short", "tight", 2.0, self._cfg())
             if x["date"] == "2026-11-03"][0]
        # sold at 1146.50 less a tick; the midpoint 1154.45 rounds UP to 1154.50; risk 8.10; 2R target 1130.20
        self.assertEqual((t["side"], t["time"], t["entry"], t["stop"], t["target"]),
                         ("short", "10:00", 1146.4, 1154.5, 1130.2))
        # 53 shares: 53 x 8.10 = 429.30, plus 64.77 of charges at the stop = 494.07, inside 500
        self.assertEqual((t["qty"], edge.round_trip(53, 1154.5, 1146.4)), (53, 64.77))
        # charges at the stop are 15.09 % of the risk: just over the 15 % low-cost limit
        self.assertEqual(t["cost_pct"], 15.09)
        self.assertFalse(t["filters"]["low_cost"])
        # bought back at the 15:05 bar's close 1139.40 plus a tick
        self.assertEqual((b[70][3], t["outcome"], t["exit"]), (1139.4, "close", 1139.5))
        self.assertEqual(t["charges"], edge.round_trip(53, 1139.5, 1146.4))
        self.assertEqual(t["pnl"], round(53 * (1146.4 - 1139.5) - t["charges"], 2))
        self.assertEqual(t["pnl"], 301.27)

    def test_a_stop_entry_by_hand(self):
        # INFY, 5 November: a buy-stop a tick over the 1154.70 range high, at 1154.80. The 9:30 bar tops out at
        # 1152.30; the 9:35 bar opens at 1151.30 and runs to 1160.50, so the stop fills at 1154.80, plus one tick
        # of slippage: 1154.90, 5.70 better than the close entry's 1160.60
        _, days = self._infy()
        b = days["2026-11-05"]
        self.assertEqual((b[3][1], b[4][0], b[4][1]), (1152.3, 1151.3, 1160.5))
        sess = edge.prepare(days, {}, {}, self._cfg())
        t = [x for x in edge.trades_for("INFY.NS", sess, "orb15s", "long", "tight", 2.0, self._cfg())
             if x["date"] == "2026-11-05"][0]
        # risk 1154.90 - 1149.80 = 5.10, 2R target 1165.10; 64 shares (the 75,000 max order binds)
        self.assertEqual((t["time"], t["entry"], t["stop"], t["target"], t["qty"]), ("9:35", 1154.9, 1149.8, 1165.1, 64))
        # the target fills: 64 x 10.20 = 652.80 less 28.58 + 45.02 of charges = 579.20
        self.assertEqual((t["outcome"], t["exit"], t["charges"], t["pnl"]), ("target", 1165.1, 73.6, 579.2))
        # no lookahead: the filters read the 9:30 bar, the last one closed before the fill (its 1,77,357 shares
        # are under the opening bars' average, so the volume filter is off)
        self.assertFalse(t["filters"]["volume"])
        self.assertTrue(t["filters"]["not_10_to_11"])

    def test_a_stop_entry_that_gaps_fills_at_the_open(self):
        _, days = self._infy()
        sess = edge.prepare(days, {}, {}, self._cfg())
        for t in edge.trades_for("INFY.NS", sess, "orb15s", "long", "wide", 2.0, self._cfg()):
            s_ = [x for x in sess if x["date"] == t["date"]][0]
            bar = s_["bars"][t["slot"]]
            trig = edge.tick_up(max(c[1] for c in s_["bars"][:3]) + s_["tick"], s_["tick"])
            self.assertEqual(t["entry"], edge.tick_up(max(trig, bar[0]) + s_["tick"], s_["tick"]))

    def test_every_setup_finds_trades(self):
        _, days = self._infy()
        sess = edge.prepare(days, {}, {}, self._cfg())
        for setup in edge.SETUPS:
            for side in edge.SIDES:
                ts = edge.trades_for("INFY.NS", sess, setup, side, "wide", 2.0, self._cfg())
                self.assertTrue(ts, (setup, side))
                for t in ts:
                    long = side == "long"
                    self.assertTrue(t["stop"] < t["entry"] < t["target"] if long else t["target"] < t["entry"] < t["stop"])
                    self.assertTrue(3 <= t["slot"] <= edge.hhmm_slot("14:45", "x"))
        with self.assertRaises(edge.EdgeError):
            edge.trigger(sess[1], "gap_and_go", True, self._cfg())


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.vix = edge.read_offline(DIR, "^NSEI"), edge.read_offline(DIR, "^INDIAVIX")
        cls.doc = edge.edge_report(charts(), cls.idx, cls.vix)

    def test_the_grid_and_the_split(self):
        d = self.doc
        # 6 setups x 2 sides x 2 stops x 4 targets x 16 filters (8 alone, low cost with 7 others, none)
        self.assertEqual((d["symbols"], d["sessions"], d["combinations"]), (5, 58, 1536))
        self.assertEqual(d["in_sample"], {"from": "2026-08-13", "to": "2026-10-08", "sessions": 39})
        self.assertEqual(d["out_of_sample"], {"from": "2026-10-09", "to": "2026-11-06", "sessions": 19})
        self.assertEqual(d["significance"], float("%.3g" % (0.05 / 1536)))
        self.assertEqual(d["passed"], [])
        self.assertIn("2026-10-19", d["expiry_sessions"])                  # Dussehra Tuesday: the Monday
        self.assertEqual(len(d["by_setup"]), 12)

    def test_the_default_rule(self):
        b = self.doc["baseline"]["all"]
        self.assertEqual((b["trades"], b["avg_R"], b["avg_R_gross"]), (144, -0.365, -0.16))
        self.assertEqual((b["targets"] + b["stops"] + b["closes"]), b["trades"])
        self.assertTrue(b["charges"] > 0)
        self.assertEqual(list(self.doc["baseline"]["by_time"]), ["9:30-10:00", "10:00-11:00", "11:00-14:45"])

    def test_filters_only_ever_remove_trades(self):
        rows = [r for r in self.doc["rows"] if r["rule"] == edge.BASELINE]
        n = {r["filter"]: r["all"]["trades"] for r in rows}
        self.assertEqual(set(n), set(edge.FILTERS))
        self.assertTrue(all(v <= n["none"] for v in n.values()))
        self.assertTrue(all(n["low_cost+" + f] <= min(n[f], n["low_cost"]) for f in edge.SINGLE[1:] if f != "low_cost"))
        self.assertEqual((n["with_nifty"], n["low_cost"], n["not_expiry"]), (93, 50, 119))

    def test_held_up_rules_get_a_p_value(self):
        for r in self.doc["held_up"]:
            self.assertIsNotNone(r["bootstrap"])
            self.assertFalse(r["passed"])                                  # none clears 0.05 / 1536
        self.assertIn("none by more than luck would explain across 1536 tests", self.doc["verdict"])

    def test_a_tighter_cost_limit_keeps_fewer_trades(self):
        n = {}
        for pct in (10, 15, 25):
            doc = edge.edge_report(charts(), self.idx, self.vix, {"max_cost_pct": pct, "setups": ("orb15",),
                                                                  "sides": ("long",), "bootstrap": 50})
            n[pct] = [r for r in doc["rows"] if r["rule"] == edge.BASELINE and r["filter"] == "low_cost"][0]["all"]["trades"]
        self.assertTrue(n[10] < n[15] < n[25], n)
        self.assertEqual(n[15], 50)

    def test_slippage_only_ever_costs(self):
        r = [edge.edge_report(charts(), self.idx, self.vix, {"slippage_ticks": s, "setups": ("orb15",),
                                                             "sides": ("long",), "bootstrap": 50})["baseline"]["all"]["avg_R"]
             for s in (0, 1, 2)]
        self.assertTrue(r[0] > r[1] > r[2], r)

    def test_what_it_would_have_made(self):
        e = self.doc["earnings"]["baseline"]
        # 144 trades lost 17,476.73 in 58 sessions: 2.48 a session (under the cap of 3), so all of it; no tax on a loss
        self.assertEqual((e["trades_per_day"], e["per_trade"], e["rupees"], e["tax"]), (2.48, -121.37, -17476.73, 0.0))
        self.assertEqual(e["pct_of_capital"], -17.48)
        self.assertEqual(self.doc["benchmark"]["nifty_pct"], 2.46)
        fake = {"all": {"trades": 580, "pnl": 5800.0}}
        self.assertEqual(edge.earnings(fake, 58, {"trades_per_day": 3, "tax_pct": 30, "capital": 100000}),
                         {"per_trade": 10.0, "trades_per_day": 3.0, "rupees": 1740.0, "tax": 522.0,
                          "after_tax": 1218.0, "pct_of_capital": 1.22})

    def test_the_verdicts(self):
        held = {"rule": edge.rule_key("orb30", "short", "tight", 3.0), "filter": "low_cost+with_nifty",
                "in_sample": {"avg_R": 0.07}, "out_of_sample": {"avg_R": 0.10}}
        v = edge.verdict(dict(self.doc, held_up=[held], passed=[]))
        self.assertIn("1 of 1536 combinations made money after charges in both halves, but none", v)
        self.assertIn("short 30-min range break, midpoint stop, 3R, low cost + with nifty", v)
        v = edge.verdict(dict(self.doc, held_up=[held], passed=[held]))
        self.assertIn("AND by more than luck would explain", v)
        self.assertIn("paper trade it", v)
        v = edge.verdict(dict(self.doc, held_up=[], passed=[]))
        self.assertIn("none of the 1536 combinations had an edge after costs", v)

    def test_without_the_index_its_filters_are_left_out(self):
        doc = edge.edge_report(charts(), cfg={"bootstrap": 50})
        self.assertEqual(doc["combinations"], 96 * 10)      # no with_nifty, stronger or calm_vix, alone or with cost

    def test_bad_settings(self):
        short = {s: json.dumps({"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {"quote": [{}]}}]}})
                 for s in FIVE}
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(short)
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(charts(), cfg={"no_entries_after": "15:15", "flat_by": "15:10"})
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(charts(), cfg={"setups": ("orb5",)})
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(charts(), cfg={"sides": ("sideways",)})


class Cli(unittest.TestCase):
    def test_offline_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "edge.json")
            code, text, err = run(["run", "--symbols", ",".join(FIVE) + ",ZZZZ.NS", "--offline", DIR,
                                   "--write", out, "--top", "5"])
            self.assertEqual(code, 0, err)
            self.assertIn("Edge report: 5 stocks, 58 sessions", text)
            self.assertIn("The pack's default (long 15-min range break, midpoint stop, 2R, no filter): 144 trades", text)
            self.assertIn("1536 combinations", text)
            self.assertIn("What it would have made for one person", text)
            self.assertIn("Holding Nifty 50 over the same sessions: +2.46 %", text)
            self.assertIn("Best of each setup", text)
            self.assertIn("No data for: ZZZZ.NS", text)
            self.assertIn("Not financial advice", text)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["combinations"], 1536)
            code, text, err = run(["run", "--symbols", ",".join(FIVE), "--offline", DIR, "--setups", "orb30",
                                   "--sides", "short", "--max-cost-pct", "10", "--bootstrap", "50"])
            self.assertEqual(code, 0, err)
            self.assertIn("128 combinations: 30-min range break; short;", text)
            self.assertIn("low-cost limit of 10 %", text)
        code, _, err = run(["run", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertIn("which stocks?", err)


if __name__ == "__main__":
    unittest.main()
