import copy
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
INFY = os.path.join(DIR, "INFY.NS-60d5m.json.gz")  # Yahoo v8 chart, range=60d interval=5m, fetched 2026-11-08
with gzip.open(INFY, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-11-05 09:45"
NOW_UTC = tm.parse_now(NOW)

# the reel's plan, as trader-exit writes it
PLAN = {"created_by": "trader-exit", "symbol": "INFY.NS", "side": "buy", "date": "2026-11-05", "setup": "orb",
        "entry": 1160.5, "entry_time": "9:40 AM", "entry_bar": 4, "interval": "5m",
        "opening_range": {"high": 1154.7, "low": 1144.9, "minutes": 15},
        "stop": 1149.8, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1181.9, "target_r": 2.0, "target_label": "2R (two times the risk)",
        "risk_per_share": 10.7, "reward_risk": 2.0, "tick": 0.1, "flat_by": "15:10"}

# fingerprints of this run (seed 19), pinned so any change to the draws shows up. The history
# replay is checked against a separate implementation in Reel.test_history_matches_an_independent_replay.
SIM_BANDS = "54267ac677bf2208"
SIM_PATHS = "6689b32b9f49c76e"
SIM_TRADES = "ad325d187020960d"
SIM_HIST = {"lo": 1127.32, "hi": 1185.76,
            "bins": [118, 101, 133, 205, 287, 376, 476, 529, 624, 761, 763, 760, 794, 734, 661, 570, 474, 413,
                     325, 262, 175, 142, 106, 58]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def replay_by_hand(text, before, last_slot, target_r=2.0):
    """The opening-range rule written out a second way, in exact decimals, straight from the
    raw chart JSON: the 15-minute range to the paisa, the first close above it, the stop at
    the midpoint rounded down to the NSE tick, the target 2R on the tick (half up), the stop
    checked before the target, else out at the last_slot bar's close."""
    from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
    r = json.loads(text)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    days = {}
    for i, t in enumerate(r["timestamp"]):
        if q["close"][i] is None or t % 300:
            continue
        local = ist(t)
        slot = (local.hour * 60 + local.minute - 555) // 5
        days.setdefault(local.date().isoformat(), {})[slot] = tuple(
            Decimal(repr(q[k][i])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for k in ("high", "low", "close"))

    def tick(p):
        for below, size in ((250, "0.01"), (1000, "0.05"), (5000, "0.10"), (10000, "0.50"), (20000, "1.00")):
            if p < below:
                return Decimal(size)
        return Decimal("5.00")

    sessions = 0
    out = []
    for d in sorted(days):
        bars = days[d]
        if d >= before or not all(k in bars for k in range(last_slot + 1)):
            continue
        sessions += 1
        oh, ol = max(bars[k][0] for k in range(3)), min(bars[k][1] for k in range(3))
        k = next((k for k in range(3, last_slot + 1) if bars[k][2] > oh), None)
        if k is None:
            continue
        e = bars[k][2]
        tk = tick(e)
        s = ((oh + ol) / 2 / tk).to_integral_value(rounding=ROUND_FLOOR) * tk
        t = ((e + Decimal(str(target_r)) * (e - s)) / tk).to_integral_value(rounding=ROUND_HALF_UP) * tk
        o = "close"
        for j in range(k + 1, last_slot + 1):
            if bars[j][1] <= s:
                o = "stop"
                break
            if bars[j][0] >= t:
                o = "target"
                break
        out.append((d, float(e), float(s), float(t), o))
    return sessions, out


class Reel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_reel_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:10"))
        self.assertEqual(m["pool_sessions"], 57)
        self.assertEqual(m["bars_left"], 66)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-12", "2026-11-04"))
        self.assertEqual(m["p_target"], 0.0486)
        self.assertEqual(m["p_stop"], 0.5349)
        self.assertEqual(m["p_neither"], 0.4165)
        self.assertEqual(m["p_green"], 0.3125)
        self.assertEqual(m["median_close"], 1155.92)
        self.assertEqual(m["p5_close"], 1135.79)
        self.assertEqual(m["p95_close"], 1177.07)
        self.assertEqual(m["avg_R"], -0.316)
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)

    def test_charges_in_r(self):
        ch = self.doc["mc"]["charges"]
        # trader-budget's 41 shares; a round trip at the entry price, 20.05 + 30.52
        self.assertEqual((ch["qty"], ch["round_trip"], ch["r"]), (41, 50.57, 0.1153))
        self.assertEqual(ch["avg_R_net"], round(-0.316 - 0.1153, 4))
        self.assertEqual(tm.round_trip(41, 1160.5, 1160.5), 50.57)
        self.assertEqual(self.doc["history"]["avg_R_net"], round(-0.349 - 0.1153, 4))
        more = machine(replays=200, qty=100)["mc"]["charges"]
        self.assertEqual(more["qty"], 100)
        self.assertLess(more["r"], 0.1153)                             # brokerage is capped at 20 an order
        big = machine(replays=200, max_loss=1000)["mc"]["charges"]
        self.assertEqual(big["qty"], 85)                               # 85 x 10.70 + 81.94 = 991.44; 86 is 1,002.54

    def test_pinned_draws(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [1136.04, 1147.72, 1156.21, 1164.38, 1176.69])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (57, 20))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (1, 10, 9, 6))
        self.assertEqual(h["avg_R"], -0.349)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["entry"], first["stop"], first["target"], first["outcome"]),
                         ("2026-08-12", 1189.0, 1186.1, 1194.8, "stop"))

    def test_history_matches_an_independent_replay(self):
        sessions, mine = replay_by_hand(TEXT, "2026-11-05", 70)
        h = self.doc["history"]
        self.assertEqual(sessions, h["sessions"])
        self.assertEqual(mine, [(t["date"], t["entry"], t["stop"], t["target"], t["outcome"]) for t in h["trades"]])

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % "
                                              "of the replays that hit one side; it did in 8 % (target first 4.9 % "
                                              "vs stop first 53.5 %).")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.083, 0.333))
        self.assertEqual(self.doc["verdict_notes"],
                         ["41.6 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1160.5] * 5)
        self.assertEqual(len(m["bands"]), 67)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:40", "15:10", 67))
        self.assertTrue(all(p[0] == 1160.5 and len(p) == 67 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("INFY.NS", 1160.5, 1149.8, 1181.9, 4, "9:40 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (10.7, 2.0, "15:10"))
        self.assertEqual(self.doc["warnings"], [])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("INFY.NS buy, Thursday 2026-11-05: entry 1160.50 at 9:40 AM (bar 4), stop 1149.80, "
                     "target 1181.90", "10,000 replays", "66 five-minute bars", "seed 19", "57 full sessions",
                     "target first   4.9 %", "stop first     53.5 %", "neither        41.6 %   still open at 3:10 PM",
                     "green          31.2 %", "median 1155.92, 90 % of replays between 1135.79 and 1177.07",
                     "Average R: -0.316", "After charges: -0.431 R. A round trip of 41 shares costs about ₹50.57",
                     "fired 20 times: 1 target, 10 stop, 9 neither. Average R -0.349, -0.464 after charges",
                     "Left out 1 session(s) with missing bars: 2026-10-15.", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$"):
            self.assertNotIn(word, text)

    def test_charges_can_flip_the_sign(self):
        mc = dict(self.doc["mc"], avg_R=0.05, charges={"qty": 41, "round_trip": 50.57, "r": 0.1153,
                                                       "avg_R_net": -0.0653})
        text = "\n".join(tm.report(dict(self.doc, mc=mc)))
        self.assertIn("Charges turn the average replay from a small gain into a loss", text)
        self.assertNotIn("Charges turn", "\n".join(tm.report(self.doc)))


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.0486, 0.5349, 1155.92))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), SIM_BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the pinned numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "INFY.NS")

    def test_the_in_progress_bar_and_the_live_tick_are_dropped(self):
        tick = 1793850300 + 9 * 300 + 4 * 60 + 45                       # 2026-11-05 10:04:45 IST

        def live(res):
            keep_bars(res, lambda ts: ts <= 1793850300 + 9 * 300)       # up to the 10:00 bar, still forming
            res["timestamp"].append(tick)
            for k, v in res["indicators"]["quote"][0].items():
                v.append(v[-1])
            res["meta"]["regularMarketTime"] = tick
        result = tm.chart_result(edited(live), "INFY.NS")
        meta, bars, dropped = tm.parse_bars(result, tm.parse_now("2026-11-05 10:05"))
        self.assertEqual((dropped["off_grid"], dropped["not_complete"]), (1, 1))
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-11-05", "09:55", 8))
        entry = [b for b in bars if b["date"] == "2026-11-05" and b["slot"] == 4][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:35", 1160.5))

    def test_yahoos_null_bars_are_counted(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        self.assertEqual(dropped["no_price"], 116)                      # the 15:15 to 15:25 bars, mostly

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-11-05 09:37"))[1]
        today = [b for b in bars if b["date"] == "2026-11-05"]
        self.assertEqual(len(today), 4)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-05 09:37"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:30 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-05 09:40"), replays=200)["mc"]["pool_sessions"], 57)

    def test_ist_has_no_daylight_saving(self):
        stamps = [1773027900,                       # 2026-03-09 03:45Z = 9:15 IST
                  1793850300,                       # 2026-11-05 03:45Z = 9:15 IST
                  1793850000,                       # 9:10 IST, before the open
                  1793872500]                       # 15:25 IST, the last bar
        res = {"meta": {"symbol": "X"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 4, "high": [1] * 4, "low": [1] * 4, "close": [1] * 4,
                                         "volume": [0] * 4}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-11-06T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-03-09", "09:15", 0), ("2026-11-05", "09:15", 0), ("2026-11-05", "15:25", 74)])
        self.assertEqual(dropped["outside_hours"], 1)
        self.assertEqual(tm.ist_offset(tm.utc_of(1773027900)), 5.5)

    def test_the_replay_only_needs_bars_up_to_its_end(self):
        # Yahoo never sent the 15:20 bar in these 60 days: replaying to the 15:30 close leaves no session
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:30", replays=10)
        self.assertIn("only 0 full session(s)", str(cm.exception))
        # a plan without its own flat_by still stops at 15:10 (the config's, or the default)
        self.assertEqual(machine(plan(flat_by=None), replays=10)["mc"]["until"], "15:10")
        self.assertEqual(machine(plan(flat_by=None), replays=10, flat_by="15:00")["mc"]["until"], "15:00")

    def test_a_session_with_a_gap_is_left_out(self):
        def cut(res):  # take one bar out of 2026-09-04, like a halt
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and
                                           ist(ts).strftime("%H:%M") == "12:00"))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 56)
        left = [x["date"] for x in doc["data"]["short_sessions_left_out"]]
        self.assertEqual(left, ["2026-09-04", "2026-10-15"])
        self.assertEqual(doc["history"]["sessions"], 56)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-11-04")
        self.assertNotIn("2026-11-05", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 117)
        self.assertEqual(doc["mc"]["pool_sessions"], 56)   # 2026-08-12 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "INFY.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-07")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-06")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS.NS"))
        self.assertIn("data is for INFY.NS", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-09"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 9:50 has not happened in this clock yet
            machine(plan(entry_time="9:50 AM", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:42 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="4:00 PM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_bar=5), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1165.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "ticker"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Nov 5"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:40 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40", entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40am", entry_bar=None)["entry_bar"], 4)

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:00", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:00", 64, "15:00"))
        self.assertIn("still open at 3:00 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:00 PM", replays=500)["mc"], m)
        for bad, words in (("9:40 AM", "nothing to replay"), ("15:35", "no later than 3:30 PM"),
                           ("15:07", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1170.0, stop=1160.0, target=1190.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stop_rounds_away_from_the_entry(self):
        self.assertEqual(tm.cents_away(1149.805, True), 1149.8)
        self.assertEqual(tm.cents_away(1149.805, False), 1149.81)
        self.assertEqual(tm.cents_away(1144.9000244140625, True), 1144.9)
        self.assertEqual(tm.cents_away(1144.8999999999999, True), 1144.9)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1144.9, target=1176.1), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1152.0, target=1177.5), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.7324, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1171.2, target=1149.8, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 3:10 PM: the price at 3:10 PM decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        # made-up odds: 2R needs the target first in a third of the replays that hit one side
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_1r_on_the_fixture(self):
        doc = machine(plan(target=1171.2, target_r=1.0))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (1.0, 0.2573, 0.5181, 0.2246, -0.268))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: target first 25.7 % vs stop first 51.8 %.")
        self.assertEqual(doc["verdict_notes"], [])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("infy.ns"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/INFY.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("m&m.ns", "query1"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; day-trader-skill/2.0; "
                                "+https://github.com/Jakeschincariol/day-trader-skill)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("INFY.NS"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("INFY.NS"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("INFY.NS"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("INFY.NS")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ.NS")
        self.assertIn("check the ticker", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", INFY, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   4.9 %", text)
        self.assertIn("Strongly toward the stop: at 2R", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.0486, -0.316))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), SIM_PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (20, -0.349))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_the_config_sets_the_size_for_the_charges(self):
        cfg = os.path.join(self.d, "trader", "config.json")
        with open(cfg, "w") as fh:
            json.dump({"max_loss_per_trade_inr": 1000}, fh)
        code, text, _ = run(["run", "--plan", self.plan, "--offline", INFY, "--now", NOW, "--replays", "200",
                             "--config", cfg, "--json"])
        self.assertEqual(json.loads(text)["mc"]["charges"]["qty"], 85)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", INFY, "--now", NOW, "--json",
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.5349)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:24], "Strongly toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "infy.ns", "--date", "2026-11-05", "--entry", "1160.50",
                               "--stop", "1149.80", "--target", "1181.90", "--entry-time", "9:40 AM",
                               "--offline", INFY, "--now", NOW, "--json", "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["until"]), (0.0486, 0.3125, "15:10"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (1, 10, -0.349))
        code, _, err = run(["run", "--symbol", "INFY.NS", "--entry", "1160.50", "--offline", INFY])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(INFY, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", INFY, "--until", "9:00"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", INFY, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 9:40 AM entry (seed 19)", lines[0])
        self.assertTrue(any(l.strip().startswith("1181.90 |") and l.endswith("  target") for l in lines), text)
        self.assertTrue(any(l.strip().startswith("1149.80 |") and l.endswith("  stop") for l in lines), text)
        self.assertTrue(any(l.strip().startswith("1160.50 |*") and l.endswith("  entry") for l in lines), text)
        self.assertIn("9:40", text)
        self.assertIn("3:10", text)
        self.assertEqual(lines[-2:], [machine()["verdict"],
                                      "41.6 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those."])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", INFY, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
