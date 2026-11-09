"""trader-exit tests. Bars for the ATR stop reuse tests/fixtures/trader-entry/
(Infosys's real 5 November 2026 five-minute bars on the NSE, see test_entry.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

INFY = os.path.join(FIX, "trader-entry", "INFY.NS-2026-11-05-5m.json")

# trader/plan.json for the reel with a 1R target, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "INFY.NS", "side": "buy", "date": "2026-11-05", "setup": "orb",
             "entry": 1160.5, "entry_time": "9:40 AM", "entry_bar": 4, "interval": "5m",
             "opening_range": {"high": 1154.7, "low": 1144.9, "minutes": 15},
             "stop": 1149.8, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1171.2, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 10.7, "reward_risk": 1.0, "tick": 0.1, "flat_by": "15:10"}
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "INFY.NS", "setup": "orb", "side": "buy",
              "date": "2026-11-05", "status": "triggered", "time": "9:40 AM", "bar_start": "09:35", "bar_index": 4,
              "price": 1160.5, "level": 1154.7, "interval": "5m",
              "opening_range": {"high": 1154.7, "low": 1144.9, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-11-05T04:10:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1154.70, ol=1144.90, atr=None, n=None, tick=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(1160.50, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1149.80"))           # (1154.70 + 1144.90) / 2, already on the tick
        self.assertEqual(p["risk"], Decimal("10.70"))
        self.assertEqual(p["target"], Decimal("1171.20"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertEqual(p["tick"], Decimal("0.10"))
        self.assertNotIn("rounded down", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(1160.50, "or-mid")
        self.assertEqual(p["target"], Decimal("1181.90"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1160.5, "or-mid", "1R", 1154.699951171875, 1144.9000244140625)
        self.assertEqual((p["entry"], p["stop"], p["target"]),
                         (Decimal("1160.50"), Decimal("1149.80"), Decimal("1171.20")))

    def test_nse_tick_sizes(self):
        t = exitp.tick_for
        self.assertEqual([t(x) for x in (99, 249.99, 250, 999.95, 1000, 4999.9, 5000, 10000, 19999, 20000, 85000)],
                         [Decimal(x) for x in ("0.01", "0.01", "0.05", "0.05", "0.10", "0.10", "0.50", "1.00",
                                               "1.00", "5.00", "5.00")])
        self.assertEqual(exitp.floor_tick(Decimal("1148.895"), Decimal("0.10")), Decimal("1148.80"))
        self.assertEqual(exitp.round_tick(Decimal("1176.55"), Decimal("0.10")), Decimal("1176.60"))
        self.assertEqual(exitp.floor_tick(Decimal("402.37"), Decimal("0.05")), Decimal("402.35"))

    def test_other_stops(self):
        p = c(1160.50, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]),
                         (Decimal("1144.90"), Decimal("15.60"), Decimal("1191.70")))
        p = c(1160.50, "price:1149.83")
        self.assertEqual(p["stop"], Decimal("1149.80"))             # down to the 0.10 tick, away from the entry
        self.assertIn("rounded down to the 0.1 tick", p["stop_detail"])
        p = c(1160.50, "pct:1")                                     # 1160.50 x 0.99 = 1148.895
        self.assertEqual((p["stop"], p["risk"], p["target"]),
                         (Decimal("1148.80"), Decimal("11.70"), Decimal("1183.90")))
        p = c(1160.50, "atr:1.5", atr=5.64, n=5)                    # 1160.50 - 8.46 = 1152.04
        self.assertEqual((p["stop"], p["risk"], p["target"]),
                         (Decimal("1152.00"), Decimal("8.50"), Decimal("1177.50")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(5) under the entry")
        p = c(402.40, "price:399.12", "2R", None, None)             # a 250-1,000 stock trades in 0.05s
        self.assertEqual((p["stop"], p["target"]), (Decimal("399.10"), Decimal("409.00")))
        p = c(1160.50, "price:1149.83", tick=0.05)                  # --tick overrides the price's tick
        self.assertEqual(p["stop"], Decimal("1149.80"))

    def test_target_rounds_half_up_to_the_tick_and_price_targets(self):
        p = c(100, "price:99.01", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("101.49"))            # 100 + 1.485, a 0.01 tick
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        self.assertEqual(c(1160.50, "or-mid", "1.5R")["target"], Decimal("1176.60"))   # 1176.55 -> the 0.10 tick
        p = c(1160.50, "or-mid", "price:1172")
        self.assertEqual(p["reward_risk"], Decimal("1.07"))
        self.assertEqual(p["target_label"], "your price (1.07R)")
        self.assertEqual(p["target_r"], Decimal("1.07"))

    def test_refusals_say_why(self):
        cases = [("price:1160.50", "2R", "at or above the entry"),
                 ("price:1161", "2R", "at or above the entry"),
                 ("or-mid", "price:1160", "at or below the entry"),
                 ("price:1160.00", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1160.50, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1160.50 is 0.58: a 0.60 risk is allowed, 0.50 is not
        self.assertEqual(c(1160.50, "price:1159.90")["risk"], Decimal("0.60"))
        with self.assertRaises(exitp.PlanError):
            c(1149.00, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1160.50, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1160.50, "or-mid", oh=1144.90, ol=1154.70)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("INFY.NS", "5m", None, INFY)
        day = exitp.datetime.date(2026, 11, 5)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=4)
        self.assertEqual(len(bars), 5)                              # 9:15 ... the 9:35 entry bar
        # by hand: 5.60, 7.50, 2.50, 3.30, 9.30 (the first one without the overnight gap)
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1154.70 - 1149.10, places=3)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(atr, sum(trs) / 5, places=9)
        self.assertAlmostEqual(atr, 5.64, places=3)
        # later in the day it uses the last 14 bars. Yahoo's two null bars never count, and the
        # 15:25 bar is not closed at the file's own time (its last trade is 15:29:59)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 72)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-11-05 09:41"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "INFY.NS", "--offline", INFY, "--now", "2026-11-05 09:40", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          ₹1,149.80  opening-range midpoint", text)
        self.assertIn("Target        ₹1,171.20  1R (one times the risk)", text)
        self.assertIn("Risk a share  ₹10.70 (0.92% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1181.90)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-05 09:45"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,181.90", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-06 09:45"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "infy.ns", "--entry", "1160.50", "--or-high", "1154.70", "--or-low",
                                  "1144.90", "--time", "9:40", "--entry-bar", "4", "--date", "2026-11-05", "--target",
                                  "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", INFY, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1152.0, 8.5, 1177.5))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 5))
        self.assertAlmostEqual(p["atr"], 5.64, places=3)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", INFY, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("INFY.NS is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1154.80", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1154.8, 1149.8, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=1154.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("v2 plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        code, _, err = self.plan("--stop", "price:1160.10")
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "INFY.NS")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "INFY.NS", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "INFY.NS", "--entry", "1160.50", "--or-high", "1154.70")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:30"})
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("flat_by in the config 15:30 is at or after the 3:30 PM close", err)
        os.remove(os.path.join("trader", "config.json"))
        code, _, err = self.plan("--flat-by", "15:45")
        self.assertIn("after the 3:30 PM close", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="3:20 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money", "/trader-timemachine", "/trader-budget", "longs only", "₹1,149.80", "₹1,181.90", "₹10.70", "tick", "too tight to survive the spread"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "tsla"):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\d")                              # no dollar amounts left


if __name__ == "__main__":
    unittest.main()
