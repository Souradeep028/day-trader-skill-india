"""The live guardian: it may only protect or reduce what is open at the broker, never open, add or unprotect."""

import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

g = load("trader-broker", "guardian")
check = g.check

TODAY = "2026-11-05"
FP = "abcdef0123456789"


def at(hhmm):
    return check.parse_now("%s %s" % (TODAY, hhmm))


def plan_state(sym="INFY.NS", side="buy", stop=1149.8, qty=41, paper=False):
    return {"orders": [{"status": "approved", "date": TODAY, "symbol": sym, "side": side, "qty": str(qty),
                        "kind": "entry", "stop": stop, "fingerprint": FP, "paper": paper}]}


CFG = {"flat_by": "15:10", "max_loss_per_trade_inr": 500, "paper": False}


class FakeKite:
    """Zerodha in memory, in Kite's own JSON shapes. MARKET orders fill at once at the last price."""

    def __init__(self, positions=(), orders=()):
        self.pos = [dict(p) for p in positions]
        self.ords = [dict(o) for o in orders]
        self.calls = []
        self.n = 900
        self.fail_place = None

    def positions(self):
        return {"data": {"net": [dict(p) for p in self.pos], "day": []}}

    def orders(self):
        return {"data": [dict(o) for o in self.ords]}

    def _p(self, sym):
        for p in self.pos:
            if p["tradingsymbol"] == sym and p["product"] == "MIS":
                return p
        p = {"tradingsymbol": sym, "exchange": "NSE", "product": "MIS", "quantity": 0, "average_price": 0,
             "last_price": 0}
        self.pos.append(p)
        return p

    def place(self, params):
        self.calls.append(("place", dict(params)))
        self.n += 1
        o = {"order_id": str(self.n), "tradingsymbol": params["tradingsymbol"], "exchange": params["exchange"],
             "transaction_type": params["transaction_type"], "order_type": params["order_type"],
             "product": params["product"], "quantity": params["quantity"], "pending_quantity": params["quantity"],
             "filled_quantity": 0, "status": "TRIGGER PENDING" if params["order_type"] == "SL-M" else "OPEN",
             "trigger_price": float(params.get("trigger_price") or 0), "price": 0, "tag": params["tag"]}
        self.ords.append(o)
        if params["order_type"] == "MARKET":
            p = self._p(params["tradingsymbol"])
            p["quantity"] += params["quantity"] * (1 if params["transaction_type"] == "BUY" else -1)
            o.update(status="COMPLETE", pending_quantity=0, filled_quantity=params["quantity"])
        if self.fail_place:
            raise g.KiteError(self.fail_place, "NetworkException")
        return o["order_id"]

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        for o in self.ords:
            if o["order_id"] == oid:
                o.update(status="CANCELLED", pending_quantity=0)

    def modify(self, oid, params):
        self.calls.append(("modify", oid, dict(params)))
        for o in self.ords:
            if o["order_id"] == oid:
                o["quantity"] = o["pending_quantity"] = params["quantity"]
                if "trigger_price" in params:
                    o["trigger_price"] = float(params["trigger_price"])

    def sends(self):
        return [c for c in self.calls]


def pos(sym, qty, avg=1160.5, ltp=1162.0, product="MIS"):
    return {"tradingsymbol": sym, "exchange": "NSE", "product": product, "quantity": qty, "average_price": avg,
            "last_price": ltp}


def order(oid, sym, side, typ, qty, trigger=0, price=0, tag="dts" + FP[:8], status=None, product="MIS"):
    return {"order_id": oid, "tradingsymbol": sym, "exchange": "NSE", "transaction_type": side, "order_type": typ,
            "product": product, "quantity": qty, "pending_quantity": qty, "filled_quantity": 0,
            "status": status or ("TRIGGER PENDING" if typ in ("SL-M", "SL") else "OPEN"),
            "trigger_price": trigger, "price": price, "tag": tag}


def run(kite, state=None, hhmm="10:05", armed=True, cfg=None):
    out = io.StringIO()
    with redirect_stdout(out):
        res = g.one_pass(kite, state if state is not None else plan_state(), cfg or CFG, at(hhmm), armed,
                         say=lambda s: print(s))
    return res


def placed(kite):
    return [c[1] for c in kite.calls if c[0] == "place"]


class Protect(unittest.TestCase):
    def test_an_unprotected_entry_gets_the_approved_stop(self):
        k = FakeKite([pos("INFY", 41)])
        run(k)
        self.assertEqual(placed(k), [{"tradingsymbol": "INFY", "exchange": "NSE", "transaction_type": "SELL",
                                      "order_type": "SL-M", "quantity": 41, "product": "MIS", "validity": "DAY",
                                      "tag": "dtsg" + FP[:8], "trigger_price": "1149.80"}])
        run(k)                                         # the next pass finds it: nothing more
        self.assertEqual(len(placed(k)), 1)

    def test_only_the_shortfall_and_a_partial_entry(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "INFY", "SELL", "SL-M", 20, 1149.8)])
        run(k)
        self.assertEqual([p["quantity"] for p in placed(k)], [21])

    def test_a_covered_position_is_left_alone(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8),
                                         order("2", "INFY", "SELL", "LIMIT", 41, price=1181.9)])
        res = run(k)
        self.assertEqual((k.calls, res["actions"], res["alerts"]), ([], [], []))

    def test_a_short_gets_a_buy_stop_above(self):
        k = FakeKite([pos("INFY", -41, 1139.5, 1138.0)])
        run(k, plan_state(side="sell", stop=1150.2))
        self.assertEqual((placed(k)[0]["transaction_type"], placed(k)[0]["trigger_price"]), ("BUY", "1150.20"))

    def test_an_unplanned_position_gets_a_stop_at_the_max_loss(self):
        k = FakeKite([pos("TCS", 10, 3000.0, 3005.0)])
        res = run(k, {"orders": []})
        # 3000 - 500 / 10 = 2950, on the 0.10 tick
        self.assertEqual((placed(k)[0]["trigger_price"], placed(k)[0]["tag"]), ("2950.00", "dtsgmanual"))
        self.assertIn("your max loss per trade", res["actions"][0]["action"]["why"])
        k = FakeKite([pos("TCS", 10, 3000.0, 3005.0)])
        res = run(k, {"orders": []}, cfg=dict(CFG, guardian_unplanned="alert"))
        self.assertEqual(placed(k), [])
        self.assertIn("TCS.NS: long 10 TCS.NS with no approved stop, and it is unprotected", res["alerts"])

    def test_paper_approvals_are_not_plans(self):
        k = FakeKite([pos("INFY", 41)])
        run(k, plan_state(paper=True))
        self.assertEqual(placed(k)[0]["tag"], "dtsgmanual")

    def test_delivery_and_other_exchanges_are_not_touched(self):
        k = FakeKite([pos("INFY", 41, product="CNC"), dict(pos("INFY", 41), exchange="BSE")])
        run(k)
        self.assertEqual(k.calls, [])

    def test_two_rejections_and_it_only_alerts(self):
        rej = [order(str(i), "INFY", "SELL", "SL-M", 41, 1149.8, tag="dtsg" + FP[:8], status="REJECTED")
               for i in (1, 2)]
        k = FakeKite([pos("INFY", 41)], rej)
        res = run(k)
        self.assertEqual(placed(k), [])
        self.assertTrue(any("2 stop orders rejected today" in a and "UNPROTECTED" in a for a in res["alerts"]))

    def test_a_timed_out_order_is_not_sent_twice(self):
        k = FakeKite([pos("INFY", 41)])
        k.fail_place = "timed out"                     # Kite took it, the answer never came
        res = run(k)
        self.assertTrue(any("failed: timed out" in a for a in res["alerts"]))
        k.fail_place = None
        run(k)
        self.assertEqual(len(placed(k)), 1)


class Repair(unittest.TestCase):
    def test_the_target_filled_so_the_stop_is_cancelled(self):
        k = FakeKite([pos("INFY", 0)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8),
                                        order("2", "TCS", "BUY", "LIMIT", 5, price=3000.0, tag="dts" + "11111111")])
        res = run(k, {"orders": plan_state()["orders"] + plan_state("TCS.NS")["orders"]})
        self.assertEqual(k.calls, [("cancel", "1")])   # TCS's pending ENTRY is left alone
        self.assertIn("orphan", res["actions"][0]["action"]["phase"])

    def test_an_orphan_that_is_not_the_packs_is_only_alerted(self):
        k = FakeKite([], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8, tag="")])
        res = run(k)
        self.assertEqual(k.calls, [])
        self.assertIn("INFY.NS is flat but stop order 1 (not the pack's) is still open: check it", res["alerts"])

    def test_a_stop_moved_away_is_moved_back(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "INFY", "SELL", "SL-M", 41, 1140.0)])
        run(k)
        self.assertEqual(k.calls, [("modify", "1", {"order_type": "SL-M", "validity": "DAY", "quantity": 41,
                                                    "trigger_price": "1149.80"})])

    def test_extra_stops_are_trimmed_to_the_position(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8),
                                         order("2", "INFY", "SELL", "SL-M", 30, 1149.8)])
        run(k)
        self.assertEqual(k.calls, [("cancel", "2")])
        k = FakeKite([pos("INFY", 20)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8)])
        run(k)                                         # the entry filled 20 of 41: the stop is cut to 20
        self.assertEqual(k.calls, [("modify", "1", {"order_type": "SL-M", "validity": "DAY", "quantity": 20,
                                                    "trigger_price": "1149.80"})])

    def test_someone_elses_extra_stop_is_only_alerted(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8, tag=""),
                                         order("2", "INFY", "SELL", "SL-M", 41, 1149.8, tag="")])
        res = run(k)
        self.assertEqual(k.calls, [])
        self.assertTrue(any("not the pack's orders" in a for a in res["alerts"]))


class Through(unittest.TestCase):
    def test_past_the_stop_with_no_stop_closes_at_market(self):
        k = FakeKite([pos("INFY", 41, ltp=1147.0)], [order("2", "INFY", "SELL", "LIMIT", 41, price=1181.9)])
        run(k)
        self.assertEqual(k.calls[0], ("cancel", "2"))  # the target first: with it the close could flip the position
        self.assertEqual((placed(k)[0]["order_type"], placed(k)[0]["quantity"]), ("MARKET", 41))
        self.assertEqual(k.pos[0]["quantity"], 0)
        run(k)
        self.assertEqual(len(placed(k)), 1)

    def test_a_stop_that_covers_it_is_left_to_the_exchange(self):
        k = FakeKite([pos("INFY", 41, ltp=1149.0)], [order("1", "INFY", "SELL", "SL-M", 41, 1149.8)])
        run(k)
        self.assertEqual(k.calls, [])

    def test_an_unplanned_position_is_never_closed_before_the_flat_by(self):
        k = FakeKite([pos("TCS", 10, 3000.0, 2900.0)])
        res = run(k, {"orders": []})
        self.assertEqual(k.calls, [])
        self.assertTrue(any("close it yourself" in a for a in res["alerts"]))


class FlatBy(unittest.TestCase):
    def test_cancel_everything_then_close_everything(self):
        k = FakeKite([pos("INFY", 41), pos("SBIN", -10, 1018.5, 1015.0)],
                     [order("1", "INFY", "SELL", "SL-M", 41, 1149.8), order("2", "INFY", "SELL", "LIMIT", 41, price=1181.9),
                      order("3", "TCS", "BUY", "LIMIT", 5, price=3000.0, tag="")])
        run(k, hhmm="15:10")
        self.assertEqual([c[1] for c in k.calls if c[0] == "cancel"], ["1", "2", "3"])
        self.assertEqual(sorted((p["tradingsymbol"], p["transaction_type"], p["quantity"]) for p in placed(k)),
                         [("INFY", "SELL", 41), ("SBIN", "BUY", 10)])
        self.assertEqual([p["quantity"] for p in k.pos], [0, 0])
        n = len(k.calls)
        run(k, hhmm="15:12")
        self.assertEqual(len(k.calls), n)

    def test_its_own_pending_close_is_not_cancelled_or_doubled(self):
        k = FakeKite([pos("INFY", 41)], [order("9", "INFY", "SELL", "MARKET", 41, tag="dtsg" + FP[:8])])
        run(k, hhmm="15:15")
        self.assertEqual(k.calls, [])


class DryRun(unittest.TestCase):
    def test_nothing_is_sent_without_arm(self):
        k = FakeKite([pos("INFY", 41)], [order("1", "TCS", "SELL", "SL-M", 5, 2900.0)])
        res = run(k, armed=False)
        self.assertEqual(k.calls, [])
        self.assertEqual([r["result"] for r in res["actions"]], ["dry run", "dry run"])

    def test_arm_needs_a_live_account(self):
        d = tempfile.mkdtemp()
        try:
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as fh:
                json.dump({"paper": True}, fh)
            err = io.StringIO()
            with redirect_stderr(err):
                code = g.main(["once", "--arm", "--config", cfg, "--session", os.path.join(d, "none")])
            self.assertEqual(code, 2)
            self.assertIn("--arm needs a live account", err.getvalue())
        finally:
            shutil.rmtree(d)

    def test_plan_reads_saved_answers(self):
        d = tempfile.mkdtemp()
        try:
            paths = {}
            for name, doc in (("pos", {"data": {"net": [pos("INFY", 41)]}}), ("ord", {"data": []}),
                              ("state", plan_state()), ("cfg", CFG)):
                paths[name] = os.path.join(d, name + ".json")
                with open(paths[name], "w") as fh:
                    json.dump(doc, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                g.main(["plan", "--positions", paths["pos"], "--broker-orders", paths["ord"], "--state", paths["state"],
                        "--config", paths["cfg"], "--now", TODAY + " 10:05"])
            self.assertIn("PROTECT  unprotected: long 41 INFY.NS, stops cover 0; SL-M SELL 41 at 1149.80 (the approved "
                          "stop)", out.getvalue())
        finally:
            shutil.rmtree(d)


class Validate(unittest.TestCase):
    """The last line of defence, whatever decide() proposes."""

    def snap(self, q=41, ltp=1162.0, orders=()):
        return g.book({"data": {"net": [pos("INFY", q, ltp=ltp)]}}, {"data": list(orders)})

    def place(self, side="sell", typ="SL-M", qty=41, trigger=1149.8):
        return {"do": "place", "symbol": "INFY.NS", "side": side, "order_type": typ, "qty": qty, "trigger": trigger,
                "tag": "dtsg", "phase": "protect"}

    def test_never_opens_or_adds(self):
        p, o, _ = self.snap()
        self.assertIn("would add to the long", g.validate(self.place(side="buy"), p, o))
        p0, o0, _ = self.snap(q=0)
        self.assertIn("is flat now", g.validate(self.place(), p0, o0))
        self.assertIn("would cover 42 of a 41-share", g.validate(self.place(qty=42), p, o))
        self.assertIn("not a whole number", g.validate(self.place(qty=1.5), p, o))
        self.assertIn("wrong side of the last price", g.validate(self.place(trigger=1170.0), p, o))
        self.assertIsNone(g.validate(self.place(), p, o))
        p, o, _ = self.snap(orders=[order("2", "INFY", "SELL", "LIMIT", 41, price=1181.9)])
        self.assertIn("closing orders for 41 shares are still open",
                      g.validate(self.place(typ="MARKET", trigger=None), p, o))

    def test_never_unprotects_or_widens(self):
        stop = order("1", "INFY", "SELL", "SL-M", 41, 1149.8)
        p, o, _ = self.snap(orders=[stop])
        cancel = {"do": "cancel", "symbol": "INFY.NS", "order_id": "1", "phase": "trim"}
        self.assertIn("under-protected", g.validate(cancel, p, o))
        self.assertIsNone(g.validate(dict(cancel, phase="flat-by"), p, o))
        wider = {"do": "modify", "symbol": "INFY.NS", "order_id": "1", "trigger": 1140.0, "phase": "restore"}
        self.assertIn("only move a stop closer", g.validate(wider, p, o))
        smaller = {"do": "modify", "symbol": "INFY.NS", "order_id": "1", "qty": 30, "phase": "trim"}
        self.assertIn("under-protected", g.validate(smaller, p, o))
        bigger = dict(smaller, qty=50)
        self.assertIn("only cut the quantity", g.validate(bigger, p, o))

    def test_random_books_never_grow_or_flip_a_position(self):
        rng = random.Random(3)
        for case in range(400):
            q = rng.choice([0, 0, 10, 41, -41, -7])
            ltp = round(rng.uniform(1130, 1180), 2)
            ords = []
            for i in range(rng.randint(0, 4)):
                ords.append(order(str(i + 1), "INFY", rng.choice(["BUY", "SELL"]),
                                  rng.choice(["SL-M", "LIMIT", "MARKET", "SL"]), rng.choice([5, 20, 41, 60]),
                                  round(rng.uniform(1130, 1180), 2), round(rng.uniform(1130, 1190), 2),
                                  rng.choice(["", "dts" + FP[:8], "dtsg" + FP[:8], "dts00000000"])))
            side = rng.choice(["buy", "sell"])
            stop = round(rng.uniform(1135, 1175), 2)
            state = plan_state(side=side, stop=stop)
            k = FakeKite([pos("INFY", q, ltp=ltp)], ords)
            hhmm = rng.choice(["10:05", "14:00", "15:10", "15:20"])
            with self.subTest(case=case, q=q, ltp=ltp, orders=ords, side=side, stop=stop, hhmm=hhmm):
                run(k, state, hhmm)
                q_after = k.pos[0]["quantity"]
                self.assertLessEqual(abs(q_after), abs(q))
                self.assertFalse(q and q_after and (q > 0) != (q_after > 0))
                pend = [o for o in k.ords if o["status"] in ("TRIGGER PENDING", "OPEN") and o["product"] == "MIS"]
                mine = [o for o in pend if o["tag"].startswith("dtsg") and o["order_id"] not in
                        {x["order_id"] for x in ords}]
                closing = "SELL" if q_after > 0 else "BUY"
                self.assertTrue(all(o["transaction_type"] == closing and q_after for o in mine))
                for p in placed(k):
                    self.assertIn(p["order_type"], ("SL-M", "MARKET"))
                    self.assertEqual(p["product"], "MIS")


class KiteWire(unittest.TestCase):
    def test_requests_and_the_login_checksum(self):
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            seen.append(req)
            return Resp(json.dumps({"status": "success", "data": {"order_id": "77", "access_token": "tok"}}).encode())

        real = g.urllib.request.urlopen
        g.urllib.request.urlopen = fake
        try:
            kite = g.Kite("key", "secret-token")
            self.assertEqual(kite.place({"tradingsymbol": "INFY", "quantity": 41}), "77")
            kite.cancel("123")
            kite.session("req", "apisecret")
        finally:
            g.urllib.request.urlopen = real
        self.assertEqual((seen[0].get_method(), seen[0].full_url), ("POST", "https://api.kite.trade/orders/regular"))
        self.assertEqual(seen[0].get_header("Authorization"), "token key:secret-token")
        self.assertEqual(seen[0].get_header("X-kite-version"), "3")
        self.assertEqual(seen[0].data, b"tradingsymbol=INFY&quantity=41")
        self.assertEqual((seen[1].get_method(), seen[1].full_url), ("DELETE", "https://api.kite.trade/orders/regular/123"))
        self.assertIsNone(seen[2].get_header("Authorization"))
        import hashlib
        self.assertIn(b"checksum=" + hashlib.sha256(b"keyreqapisecret").hexdigest().encode(), seen[2].data)


if __name__ == "__main__":
    unittest.main()
