"""trader-timemachine's rotation.py. The mechanics are checked on made-up stocks whose prices follow simple formulas
(so every trade can be worked out by hand); the reality check on the real factor ETFs in
tests/fixtures/trader-timemachine/funds/ (Yahoo, 9 November 2026)."""

import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

rotation = load("trader-timemachine", "rotation")
swing = load("trader-timemachine", "swing")
FUNDS = os.path.join(FIX, "trader-timemachine", "funds")


def weekdays(start, n):
    d, out = datetime.date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def chart(sym, prices, days):
    """A Yahoo v8 chart for these daily prices (open = high = low = close), stamped 9:15 IST each day."""
    ts = [int((datetime.datetime(d.year, d.month, d.day, 3, 45) - datetime.datetime(1970, 1, 1)).total_seconds())
          for d in days]
    q = {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * len(prices)}
    return json.dumps({"chart": {"result": [{"meta": {"symbol": sym}, "timestamp": ts,
                                             "indicators": {"quote": [q]}}]}})


N = 600
DAYS = weekdays("2018-01-01", N)
GROWTH = {"FAST.NS": 1.002, "MID.NS": 1.001, "SLOW.NS": 1.0002}
WIGGLE = {"FAST.NS": 0.02, "MID.NS": 0.01, "SLOW.NS": 0.001}


def prices(sym):
    return [round(100 * GROWTH[sym] ** i * (1 + WIGGLE[sym] * (-1) ** i), 2) for i in range(N)]


def stocks_and_calendar():
    charts = {s: chart(s, prices(s), DAYS) for s in GROWTH}
    stocks = {}
    for s, t in charts.items():
        b = swing.daily_bars(t, s)
        stocks[s] = {"bars": b, "close": [x[4] for x in b], "idx": {x[0]: i for i, x in enumerate(b)}}
    cal = [d.isoformat() for d in DAYS]
    return stocks, cal


class Pieces(unittest.TestCase):
    def test_financial_years_and_month_ends(self):
        self.assertEqual((rotation.fy_of("2026-03-31"), rotation.fy_of("2026-04-01")), (2026, 2027))
        days = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02"]
        self.assertEqual(rotation.month_ends(days), [1, 3, 4])

    def test_rankings(self):
        stocks, cal = stocks_and_calendar()
        day = cal[400]
        # 12 months back to 1 month back: the fastest grower first
        self.assertEqual(rotation.ranking("mom12", stocks, day), ["FAST.NS", "MID.NS", "SLOW.NS"])
        # the steadiest first: SLOW wiggles 0.1 % a day, MID 1 %, FAST 2 %
        self.assertEqual(rotation.ranking("lowvol", stocks, day), ["SLOW.NS", "MID.NS", "FAST.NS"])
        # not enough history yet: 252 + 21 sessions are needed
        self.assertEqual(rotation.ranking("mom12", stocks, cal[250]), [])


class Simulation(unittest.TestCase):
    def test_hold_the_leader(self):
        stocks, cal = stocks_and_calendar()
        r = rotation.simulate(stocks, cal, "mom12", 1, False, {"capital": 100000.0}, {})
        buys = [x for x in r["log"] if x[2] == "buy"]
        self.assertEqual([x[1] for x in r["log"] if x[2] != "tax"], ["FAST.NS"])      # bought once, never sold
        day, sym, side, qty, px, ch = buys[0]
        # the first month end with 273 sessions behind it, bought at the next session's open
        signal = cal[next(i for i in rotation.month_ends(cal) if i >= 273)]
        self.assertEqual(day, cal[cal.index(signal) + 1])
        self.assertEqual(px, prices("FAST.NS")[cal.index(day)])
        # the most whole shares that fit 1,00,000 with the delivery charges on the buy
        self.assertEqual(qty, max(q for q in range(1, int(100000 // px) + 1)
                                  if q * px + swing.delivery_charges("buy", q * px)["total"] <= 100000))
        self.assertEqual(ch, swing.delivery_charges("buy", qty * px)["total"])
        self.assertEqual((r["taxes"], r["held"]), (0.0, {"FAST.NS": qty}))
        self.assertAlmostEqual(r["equity"][cal[-1]], r["cash"] + qty * prices("FAST.NS")[-1], places=2)

    def test_the_tax_matches_the_orders(self):
        # the leader changes: MID grows fast after session 330, so FAST is sold and MID bought; the gain on the
        # FAST shares, held under a year, is taxed at 20 % the next April
        p_fast = [round(100 * 1.002 ** min(i, 330) * 0.999 ** max(i - 330, 0), 2) for i in range(N)]
        p_mid = [round(100 * 1.001 ** i * 1.004 ** max(i - 330, 0), 2) for i in range(N)]
        p_slow = prices("SLOW.NS")
        stocks = {}
        for s, p in (("FAST.NS", p_fast), ("MID.NS", p_mid), ("SLOW.NS", p_slow)):
            b = swing.daily_bars(chart(s, p, DAYS), s)
            stocks[s] = {"bars": b, "close": [x[4] for x in b], "idx": {x[0]: i for i, x in enumerate(b)}}
        cal = [d.isoformat() for d in DAYS]
        r = rotation.simulate(stocks, cal, "mom6", 1, False, {"capital": 100000.0}, {})
        log = r["log"]
        sells = [x for x in log if x[2] == "sell"]
        self.assertTrue(sells, log)
        # rebuild the short-term gains from the orders, financial year by financial year
        lots, st = {}, {}
        for day, sym, side, qty, px, ch in log:
            if side == "buy":
                lots[sym] = (day, qty * px + ch)
            elif side == "sell":
                d0, cost = lots.pop(sym)
                held = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(d0)).days
                self.assertLess(held, 366)
                st[rotation.fy_of(day)] = st.get(rotation.fy_of(day), 0) + qty * px - ch - cost
        paid = {rotation.fy_of(cal[cal.index(d) - 1]): due for d, _, side, _, _, due in log if side == "tax"}
        for fy, g in st.items():
            if fy in paid:
                self.assertAlmostEqual(paid[fy], max(g, 0) * 0.20, places=1)
        self.assertGreater(sum(paid.values()), 0)
        # without tax the same orders happen until the first April payment changes the cash
        r0 = rotation.simulate(stocks, cal, "mom6", 1, False, {"capital": 100000.0}, {}, tax=False)
        self.assertEqual(r0["taxes"], 0.0)
        first_tax = min(d for d, _, side, *_ in log if side == "tax")
        self.assertEqual([x for x in log if x[0] < first_tax and x[2] != "tax"],
                         [x for x in r0["log"] if x[0] < first_tax])

    def test_the_trend_filter_holds_cash(self):
        stocks, cal = stocks_and_calendar()
        r = rotation.simulate(stocks, cal, "mom12", 1, True, {"capital": 100000.0}, {d: False for d in cal})
        self.assertEqual([x for x in r["log"] if x[2] == "buy"], [])
        self.assertEqual(r["cash"], 100000.0)


class Funds(unittest.TestCase):
    def test_the_reality_check(self):
        f = rotation.fund_check({s: swing.read_offline(FUNDS, s) for s in rotation.FUNDS},
                                swing.read_offline(FUNDS, rotation.FUND_BENCH), "2026-11-09")
        got = {x["fund"]: (x["from"], x["cagr"], x["bench_cagr"], x["bench_max_drawdown"]) for x in f}
        # the momentum ETF trailed Nifty 50 over its life; the low-volatility ETF matched it with a smaller fall
        self.assertEqual(got["MOMENTUM.NS"], ("2022-08-19", 8.93, 9.53, 15.23))
        self.assertEqual(got["LOWVOLIETF.NS"], ("2018-05-11", 11.93, 11.64, 36.34))

    def test_bad_prints_are_dropped(self):
        # Yahoo prints NIFTYBEES at about a tenth of its price on 19 and 20 December 2019 (a unit split it
        # adjusted everywhere else): those two days are left out
        b = swing.daily_bars(swing.read_offline(FUNDS, "NIFTYBEES.NS"), "NIFTYBEES.NS", "2026-11-09")
        self.assertNotIn("2019-12-19", [x[0] for x in b])
        self.assertTrue(all(abs(b[i][4] / b[i - 1][4] - 1) < 0.15 for i in range(1, len(b))))
        self.assertEqual(swing.drop_spikes([("a", 1, 1, 1, 100.0), ("b", 1, 1, 1, 10.0), ("c", 1, 1, 1, 101.0)]),
                         [("a", 1, 1, 1, 100.0), ("c", 1, 1, 1, 101.0)])
        # a lasting move is real, and stays
        steps = [("a", 1, 1, 1, 100.0), ("b", 1, 1, 1, 50.0), ("c", 1, 1, 1, 51.0), ("d", 1, 1, 1, 52.0),
                 ("e", 1, 1, 1, 50.0)]
        self.assertEqual(swing.drop_spikes(steps), steps)


class Report(unittest.TestCase):
    def test_a_small_run(self):
        stocks = {s: chart(s, prices(s), DAYS) for s in GROWTH}
        nifty = chart("^NSEI", [round(100 * 1.0005 ** i, 2) for i in range(N)], DAYS)
        doc = rotation.rotation_report(stocks, nifty, {"min_stocks": 3, "sizes": (1,)}, "2099-01-01")
        names = [r["name"] for r in doc["rows"]]
        self.assertEqual(sorted(names), sorted(["equal", "mom12_top1", "mom12_top1_trend", "mom6_top1",
                                                "mom6_top1_trend", "lowvol_top1", "lowvol_top1_trend"]))
        self.assertEqual(doc["tried"], 6)
        best = doc["rows"][0]
        self.assertTrue(best["name"].startswith("mom"))                  # FAST grows fastest
        self.assertIn("survivors", "\n".join(rotation.report(doc)))
        with self.assertRaises(rotation.RotationError):
            rotation.rotation_report(stocks, nifty, {"sizes": (1,)}, "2099-01-01")   # under 25 stocks
        with self.assertRaises(rotation.RotationError):
            rotation.rotation_report(stocks, None, {"min_stocks": 3, "sizes": (1,)}, "2099-01-01")


if __name__ == "__main__":
    unittest.main()
