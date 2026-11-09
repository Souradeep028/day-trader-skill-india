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

    def test_the_trades_match_the_time_machines_history(self):
        # no slippage, the same entry hours: the same trades as timemachine.py's history replay
        with gzip.open(os.path.join(FIX, "trader-timemachine", "INFY.NS-60d5m.json.gz"), "rt") as fh:
            text = fh.read()
        cfg = {"slippage_ticks": 0, "first_slot": 3, "last_entry_slot": edge.hhmm_slot("15:05", "x"),
               "flat_slot": edge.hhmm_slot("15:10", "x"), "max_loss": 500, "max_order": 1e9, "to": "2026-11-04"}
        mine = edge.trades_for("INFY.NS", edge.sessions_of(text, "INFY.NS"), {}, {}, 15, "or_mid", 2.0, cfg)
        plan = tm.normalize_plan({"symbol": "INFY.NS", "side": "buy", "date": "2026-11-05", "setup": "orb",
                                  "entry": 1160.5, "entry_time": "9:40 AM", "entry_bar": 4,
                                  "opening_range": {"high": 1154.7, "low": 1144.9, "minutes": 15},
                                  "stop": 1149.8, "stop_rule": "or_mid", "target": 1181.9, "flat_by": "15:10"})
        hist = tm.time_machine(text, plan, tm.parse_now("2026-11-05 09:45"), replays=10)["history"]["trades"]
        self.assertEqual([(t["date"], t["entry"], t["stop"], t["target"], t["outcome"]) for t in mine],
                         [(t["date"], t["entry"], t["stop"], t["target"], t["outcome"]) for t in hist])
        self.assertEqual([round(t["R_gross"], 2) for t in mine], [t["R"] for t in hist])

    def test_a_trade_by_hand(self):
        # The reel's day, INFY on 5 November 2026. Every INFY price that day is a multiple of 0.10 (NSE's
        # November review moved it up after it closed October above 1,000; on 1 October it still traded
        # in 0.05s, at 1022.35), so one tick of slippage is 0.10.
        with gzip.open(os.path.join(FIX, "trader-timemachine", "INFY.NS-60d5m.json.gz"), "rt") as fh:
            days = edge.sessions_of(fh.read(), "INFY.NS")
        self.assertEqual(edge.observed_tick([x for b in days["2026-11-05"].values() for x in b[:4]]), 0.10)
        self.assertEqual(edge.observed_tick([x for b in days["2026-10-01"].values() for x in b[:4]]), 0.05)
        cfg = {"slippage_ticks": 1, "first_slot": 3, "last_entry_slot": edge.hhmm_slot("14:45", "x"),
               "flat_slot": edge.hhmm_slot("15:10", "x"), "max_loss": 500, "max_order": 75000}
        t = [x for x in edge.trades_for("INFY.NS", days, {}, {}, 15, "or_mid", 2.0, cfg) if x["date"] == "2026-11-05"][0]
        # bought at the 9:35 bar's close 1160.50 plus a tick; risk 1160.60 - 1149.80 = 10.80; 2R target 1182.20
        self.assertEqual((t["entry"], t["stop"], t["target"], t["qty"]), (1160.6, 1149.8, 1182.2, 41))
        # neither side by 15:10: sold at the 15:05 bar's close 1164.90 less a tick
        self.assertEqual((t["outcome"], t["exit"]), ("close", 1164.8))
        self.assertEqual(t["charges"], edge.round_trip(41, 1160.6, 1164.8))
        self.assertEqual(t["pnl"], round(41 * (1164.8 - 1160.6) - t["charges"], 2))


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.vix = edge.read_offline(DIR, "^NSEI"), edge.read_offline(DIR, "^INDIAVIX")
        cls.doc = edge.edge_report(charts(), cls.idx, cls.vix)

    def test_the_grid_and_the_split(self):
        d = self.doc
        self.assertEqual((d["symbols"], d["sessions"], d["combinations"]), (5, 58, 96))
        self.assertEqual(d["in_sample"], {"from": "2026-08-13", "to": "2026-10-08", "sessions": 39})
        self.assertEqual(d["out_of_sample"], {"from": "2026-10-09", "to": "2026-11-06", "sessions": 19})
        self.assertEqual(d["held_up"], [])

    def test_the_default_rule(self):
        b = self.doc["baseline"]["all"]
        self.assertEqual((b["trades"], b["avg_R"], b["avg_R_gross"]), (144, -0.365, -0.16))
        self.assertEqual((b["targets"] + b["stops"] + b["closes"]), b["trades"])
        self.assertTrue(b["charges"] > 0)
        self.assertEqual(list(self.doc["baseline"]["by_time"]), ["9:30-10:00", "10:00-11:00", "11:00-14:45"])
        self.assertIn("had no edge after costs", self.doc["verdict"])

    def test_filters_only_ever_remove_trades(self):
        rows = [r for r in self.doc["rows"] if r["rule"] == {"or_minutes": 15, "stop": "or_mid", "target_r": 2.0}]
        n = {r["filter"]: r["all"]["trades"] for r in rows}
        self.assertEqual(set(n), set(edge.FILTERS))
        self.assertTrue(all(v <= n["none"] for v in n.values()))
        self.assertEqual(n["nifty_up"], 93)

    def test_slippage_only_ever_costs(self):
        r = [edge.edge_report(charts(), self.idx, self.vix, {"slippage_ticks": s})["baseline"]["all"]["avg_R"]
             for s in (0, 1, 2)]
        self.assertTrue(r[0] > r[1] > r[2], r)

    def test_held_up_needs_both_halves(self):
        doc = dict(self.doc, held_up=[{"rule": {"or_minutes": 15, "stop": "or_low", "target_r": 2.0},
                                       "filter": "nifty_up", "in_sample": {"avg_R": 0.12}, "out_of_sample": {"avg_R": 0.05}}])
        v = edge.verdict(doc)
        self.assertIn("1 of 96 combinations made money after charges in both halves", v)
        self.assertIn("15-min range, range-low stop, 2R target, nifty up", v)
        self.assertIn("paper trade it before believing it", v)

    def test_without_the_index_its_filters_are_left_out(self):
        doc = edge.edge_report(charts())
        self.assertEqual(doc["combinations"], 16 * 3)                      # none, volume, no_gap

    def test_too_little_data(self):
        short = {s: json.dumps({"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {"quote": [{}]}}]}})
                 for s in FIVE}
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(short)
        with self.assertRaises(edge.EdgeError):
            edge.edge_report(charts(), cfg={"no_entries_after": "15:15", "flat_by": "15:10"})


class Cli(unittest.TestCase):
    def test_offline_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "edge.json")
            code, text, err = run(["run", "--symbols", ",".join(FIVE) + ",ZZZZ.NS", "--offline", DIR,
                                   "--write", out, "--top", "5"])
            self.assertEqual(code, 0, err)
            self.assertIn("Edge report: 5 stocks, 58 sessions", text)
            self.assertIn("The pack's default (15-min range, midpoint stop, 2R, no filter): 144 trades", text)
            self.assertIn("had no edge after costs", text)
            self.assertIn("No data for: ZZZZ.NS", text)
            self.assertIn("Not financial advice", text)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["combinations"], 96)
        code, _, err = run(["run", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertIn("which stocks?", err)


if __name__ == "__main__":
    unittest.main()
