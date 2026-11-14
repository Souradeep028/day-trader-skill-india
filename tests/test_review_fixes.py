"""The fixes from the trading review: the sequential forward-test gate, the live lock that needs it, realistic
fills in edge.py (the next bar's open, targets traded through, slippage in basis points), the unconditional
baseline a rule must beat, and the measured slippage in basis points. Real 5-minute bars of five Nifty 50 stocks
(the edge fixtures, to 6 November 2026)."""

import datetime
import os
import unittest

from _load import load, FIX

edge = load("trader-timemachine", "edge")
forward = load("trader-broker", "forward")
check = load("trader-risk", "check")
EDGE = os.path.join(FIX, "trader-timemachine", "edge")
FIVE = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def test_doc(rules=None):
    return {"rule": {"setup": "orb30", "side": "short", "stop": "tight", "target_r": 2.0}, "filter": "none",
            "started": "2026-11-10", "expected": {"avg_R": 0.076, "low": -0.145},
            "stop_rules": rules or forward.sequential_rules(), "signals": []}


def trades(rs):
    return [{"date": "2026-11-%02d" % (1 + i % 28), "symbol": "X%d.NS" % i, "R": r, "net": r * 400} for i, r in
            enumerate(rs)]


class SequentialGate(unittest.TestCase):
    def verdict(self, rs, **rules):
        return forward.evaluate(test_doc(forward.sequential_rules(**rules)), trades(rs))

    def test_no_pass_before_300_and_then_only_when_the_whole_range_is_above_zero(self):
        good = [1.6, -1.0] * 150                       # +0.30 R a trade, SD 1.30: 2.5 SE = 0.19
        self.assertEqual(self.verdict(good[:298])["state"], "running")
        res = self.verdict(good)
        self.assertEqual((res["state"], res["gate"]), ("passed", "sequential"))
        self.assertIn("PASSED: after 300 trades it is making money by more than chance", res["verdict"])
        # +0.10 R a trade on the same spread: real, maybe, but not shown by 300 trades: it keeps running
        small = [1.2, -1.0] * 150
        self.assertEqual(self.verdict(small)["state"], "running")

    def test_a_clear_loser_stops_early_and_a_small_edge_is_killed_at_300(self):
        loser = [-1.0, -1.0, -1.0, 1.0] * 10           # -0.50 R a trade over 40 (20 R down, under the 30 R stop)
        self.assertEqual(self.verdict(loser[:39])["state"], "running")          # too early
        res = self.verdict(loser)
        self.assertEqual(res["state"], "stop")
        self.assertIn("losing by more than chance", res["verdict"])
        tiny = [1.06, -1.0] * 150                      # +0.03 R: under the +0.05 R worth trading
        res = self.verdict(tiny)
        self.assertEqual(res["state"], "stop")
        self.assertIn("too small to be worth trading", res["verdict"])
        # at 1,300 trades a rule still not proven stops
        meh = [1.12, -1.0] * 650                       # +0.06 R, SD 1.06: 2.5 SE = 0.073
        self.assertEqual(self.verdict(meh[:1298])["state"], "running")
        self.assertIn("still not proven", self.verdict(meh)["verdict"])

    def test_the_drawdown_stop_is_30_R(self):
        self.assertEqual(self.verdict([-1.0] * 29 + [1.0])["state"], "running")   # 29 R down, under 40 trades
        res = self.verdict([-1.0] * 30)
        self.assertIn("past the 30 R limit", res["verdict"])

    def test_old_tests_keep_their_40_trade_verdict(self):
        res = forward.evaluate(test_doc({"min_trades": 40, "max_drawdown_R": 8.0}), trades([1.6, -1.0] * 20))
        self.assertEqual((res["state"], res["gate"]), ("passed", "legacy-40"))


class LiveLock(unittest.TestCase):
    TODAY = "2026-11-12"

    def fw(self, gate):
        v = {"state": "passed", "date": self.TODAY, "passed_on": self.TODAY}
        if gate:
            v["gate"] = gate
        return {"verdicts": {"shadow": dict(v), "paper": dict(v)}}

    def test_only_a_sequential_pass_opens_it(self):
        ok, why = check.live_gate(self.fw(None), self.TODAY)
        self.assertFalse(ok)
        self.assertIn("passed the old 40-trade gate", why)
        self.assertFalse(check.live_gate(self.fw("legacy-40"), self.TODAY)[0])
        self.assertEqual(check.live_gate(self.fw("sequential"), self.TODAY), (True, ""))

    def test_record_keeps_the_gate(self):
        test = test_doc()
        res = forward.evaluate(test, trades([1.6, -1.0] * 150))
        forward.record(test, "shadow", res, self.TODAY)
        self.assertEqual(test["verdicts"]["shadow"]["gate"], "sequential")


class Fills(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.charts = {s: edge.read_offline(EDGE, s) for s in FIVE}
        cls.cfg = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "max_cost_pct": 15.0,
                   "first_slot": edge.hhmm_slot("09:30", "x"), "last_entry_slot": edge.hhmm_slot("14:45", "x"),
                   "flat_slot": edge.hhmm_slot("15:10", "x")}
        cls.cfg["dates"] = None
        cls.sessions = {s: edge.prepare(edge.sessions_of(t, s), {}, {}, dict(cls.cfg, dates=set(
            edge.sessions_of(t, s)), expiry=set())) for s, t in cls.charts.items()}

    def cfg_for(self, sym, **fills):
        c = dict(self.cfg, expiry=set())
        c.update(fills)
        return c

    def test_a_delayed_entry_meets_the_next_bar_s_open_with_its_slippage(self):
        s = self.sessions["INFY.NS"]
        hits = [(x, edge.entry_at(x, "orb30", "short", "tight", 2.0, self.cfg)) for x in s]
        x, e0 = next((x, e) for x, e in hits if e)
        e1 = edge.entry_at(x, "orb30", "short", "tight", 2.0, dict(self.cfg, entry_delay=1, slippage_bp=3.0))
        B, t = x["bars"], x["tick"]
        k = e0["slot"]
        self.assertEqual(e1["slot"], k + 1)
        # a short sale: the next bar's open, less 3 bp, less a tick, on the tick
        self.assertEqual(e1["entry"], edge.tick_down(B[k + 1][0] * (1 - 3e-4) - t, t))
        self.assertTrue(e1["delayed"])
        self.assertEqual(e1["stop"], e0["stop"])                                 # the stop is the chart's
        self.assertEqual(e1["filters"], e0["filters"])                           # filters saw the same bars

    def test_a_target_must_trade_through(self):
        e = {"stop": 110.0, "target": 100.0, "through": 1}
        bar = (104.0, 105.0, 100.0, 101.0, 1000)                                 # touched 100.00, no lower
        self.assertIsNone(edge.exit_on(bar, e, False, 1, 0.05))
        self.assertEqual(edge.exit_on(bar, dict(e, through=0), False, 1, 0.05), ("target", 100.0))
        bar = (104.0, 105.0, 99.95, 101.0, 1000)
        self.assertEqual(edge.exit_on(bar, e, False, 1, 0.05), ("target", 100.0))
        # a stop is a market order once triggered: it pays the bp too (a short's stop: 110 + 3 bp + a tick)
        bar = (109.0, 110.5, 108.0, 110.2, 1000)
        self.assertEqual(edge.exit_on(bar, dict(e, slip_bp=3.0), False, 1, 0.05), ("stop", 110.1))

    def test_the_grid_s_default_is_unchanged(self):
        e = {"stop": 110.0, "target": 100.0}
        self.assertEqual(edge.exit_on((104.0, 105.0, 100.0, 101.0, 1), e, False, 1, 0.05), ("target", 100.0))

    def test_the_baseline_takes_every_session_not_just_the_rule_s_days(self):
        c = self.cfg_for("x", **edge.FILLS["realistic"])
        ts = []
        for sym, sess in self.sessions.items():
            ts += edge.trades_for(sym, sess, "orb30", "short", "tight", 2.0, c)
        bl = edge.baseline_trades(ts, self.sessions, c, 2.0)
        rule_days = {(t["symbol"], t["date"]) for t in ts}
        base_days = {(t["symbol"], t["date"]) for t in bl}
        self.assertGreater(len(base_days), len(rule_days))
        self.assertTrue(base_days - rule_days)                                   # days the rule never fired
        self.assertEqual({t["side"] for t in bl}, {"short"})
        self.assertEqual({t["time"] for t in bl}, {"9:30"})       # in at the window's start, whatever the chart did


class Report(unittest.TestCase):
    def test_the_best_rule_is_shown_with_realistic_fills_and_its_baseline(self):
        charts = {s: edge.read_offline(EDGE, s) for s in FIVE}
        doc = edge.edge_report(charts, edge.read_offline(EDGE, edge.INDEX), edge.read_offline(EDGE, edge.VIX),
                               {"setups": ("orb30",), "sides": ("short",), "bootstrap": 200})
        rz = doc["best"]["realism"]["by_bp"]
        self.assertEqual([x["bp"] for x in rz], [0.0, 3.0, 6.0])
        self.assertGreater(rz[0]["avg_R"], rz[1]["avg_R"])                         # each bp costs
        self.assertGreater(rz[1]["avg_R"], rz[2]["avg_R"])
        text = "\n".join(edge.report(doc))
        self.assertIn("The best rule with realistic fills", text)
        self.assertIn("baseline", text)
        self.assertEqual(doc["settings"]["fills"], "close")
        real = edge.edge_report(charts, edge.read_offline(EDGE, edge.INDEX), edge.read_offline(EDGE, edge.VIX),
                                {"setups": ("orb30",), "sides": ("short",), "bootstrap": 200, "fills": "realistic"})
        self.assertEqual((real["settings"]["entry_delay"], real["settings"]["slippage_bp"]), (1, 3.0))
        self.assertIn("realistic (the next bar's open, targets traded through)", "\n".join(edge.report(real)))

    def test_the_header_counts_match_the_grid(self):
        n = len(edge.SETUPS) * len(edge.SIDES) * len(edge.STOPS) * len(edge.TARGETS)
        self.assertEqual((n, len(edge.FILTERS), n * len(edge.FILTERS)), (96, 16, 1536))
        self.assertIn("= 96 rules", edge.__doc__)
        self.assertIn("(16 in all)", edge.__doc__)
        self.assertIn("With 1,536 combinations", edge.__doc__)


class ShadowFills(unittest.TestCase):
    def test_a_new_shadow_fills_like_the_autopilot_and_an_old_one_as_before(self):
        charts = {s: edge.read_offline(EDGE, s) for s in FIVE}
        idx, vix = edge.read_offline(EDGE, edge.INDEX), edge.read_offline(EDGE, edge.VIX)
        days = sorted(edge.sessions_of(idx, edge.INDEX))[-20:]
        old = dict(test_doc(), filter="none")
        new = dict(old, fills={"model": "realistic", "slippage_bp": 3.0})
        a = forward.shadow_trades(old, charts, idx, vix, days)
        b = forward.shadow_trades(new, charts, idx, vix, days)
        ta = [t for d in days for t in a[d]]
        tb = [t for d in days for t in b[d]]
        self.assertTrue(ta and tb)
        self.assertLess(sum(t["R"] for t in tb) / len(tb), sum(t["R"] for t in ta) / len(ta) + 0.5)
        self.assertNotEqual([(t["symbol"], t["entry"]) for t in ta], [(t["symbol"], t["entry"]) for t in tb])


if __name__ == "__main__":
    unittest.main()
