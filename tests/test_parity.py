"""The money and tick arithmetic is copied into several scripts so each skill runs on its own. These tests hold the
copies to one answer: if one drifts, sizing, the risk check, the paper fills and the backtests stop agreeing."""

import random
import unittest

from _load import load

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
size = load("trader-budget", "size")
edge = load("trader-timemachine", "edge")
tm = load("trader-timemachine", "timemachine")
exit_ = load("trader-exit", "exit")

CHARGES = {"check": check, "ticket": ticket, "size": size, "edge": edge, "timemachine": tm}
ROUND_TRIPS = {"check": check, "size": size, "edge": edge, "timemachine": tm}
TICKS = {"edge": edge, "timemachine": tm}
# order values around Zerodha's 20-rupee brokerage cap (0.03 % reaches 20 at 66,666.67) and far from it
VALUES = [0.01, 1, 99.99, 500, 10000, 66666.66, 66666.67, 66666.68, 75000, 123456.78, 1e6, 2.5e7]
RATES = [None, {}, {"brokerage_pct": 0.0, "brokerage_max": 0.0}, {"stt_sell_pct": 0.1, "gst_pct": 0.0}]


class Charges(unittest.TestCase):
    def test_order_charges_agree(self):
        for side in ("buy", "sell"):
            for v in VALUES:
                for rates in RATES:
                    want = check.order_charges(side, v, rates)
                    for name, mod in CHARGES.items():
                        with self.subTest(module=name, side=side, value=v, rates=rates):
                            self.assertEqual(mod.order_charges(side, v, rates), want)

    def test_round_trips_agree(self):
        rng = random.Random(11)
        for _ in range(300):
            qty = rng.randint(1, 2000)
            buy = round(rng.uniform(5, 6000), 2)
            sell = round(buy * rng.uniform(0.9, 1.1), 2)
            want = check.round_trip(qty, buy, sell)
            for name, mod in ROUND_TRIPS.items():
                with self.subTest(module=name, qty=qty, buy=buy, sell=sell):
                    self.assertEqual(mod.round_trip(qty, buy, sell), want)


class Fingerprints(unittest.TestCase):
    def test_risk_and_broker_fingerprint_the_same_order_the_same_way(self):
        # trader-broker only shows an order as READY when its fingerprint matches trader-risk's PASS
        base = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "41",
                "type": "market", "time_in_force": "day", "order_class": "bracket",
                "take_profit_limit_price": "1181.90", "stop_loss_stop_price": "1149.80",
                "why": "not part of the order", "plan": {"entry": 1160.5}}
        variants = [base, dict(base, qty="42"), dict(base, side="sell"), dict(base, limit_price="1160.00",
                                                                              type="limit"),
                    dict(base, client_order_id=None), {k: v for k, v in base.items() if k != "order_class"}]
        for o in variants:
            with self.subTest(order=o):
                self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))
        self.assertEqual(check.fingerprint(base), check.fingerprint(dict(base, why="other words", plan={})))
        self.assertNotEqual(check.fingerprint(base), check.fingerprint(dict(base, qty="42")))


class Ticks(unittest.TestCase):
    PRICES = [0.05, 1, 249.99, 250, 250.01, 999.95, 1000, 1000.05, 4999.9, 5000, 9999.5, 10000, 19999, 20000, 50000]

    def test_tick_sizes_agree_with_trader_exit(self):
        for p in self.PRICES:
            want = float(exit_.tick_for(str(p)))
            for name, mod in TICKS.items():
                with self.subTest(module=name, price=p):
                    self.assertEqual(mod.tick_for(p), want)

    def test_tick_rounding_agrees(self):
        rng = random.Random(5)
        for _ in range(500):
            x = round(rng.uniform(10, 9000), 4)
            tick = edge.tick_for(x)
            with self.subTest(x=x, tick=tick):
                self.assertEqual(tm.tick_round(x, tick), edge.tick_round(x, tick))
                self.assertEqual(tm.tick_away(x, True, tick), edge.tick_down(x, tick))
                self.assertEqual(tm.tick_away(x, False, tick), edge.tick_up(x, tick))


if __name__ == "__main__":
    unittest.main()
