import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-infy.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-infy.json")
NOW = "2026-11-05 09:40"
TODAY = "2026-11-05"
ITC = {"symbol": "ITC.NS", "exchange": "NSE", "product": "CNC", "side": "buy", "qty": "61", "type": "market",
       "time_in_force": "day"}
BATCH = ["ITC.NS", "TCS.NS", "SBIN.NS"]
INFY_PLAIN = ("BUY 41 INFY.NS, market, intraday (MIS), good for today / stop-loss 1149.80 / target 1181.90 / "
              "max loss about ₹489 with charges if the stop fills at 1149.80 (it can fill worse in a fast market)")


def infy(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def kite(side, order_type, tag, price=None, trigger=None, qty=41):
    c = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": side,
         "quantity": qty, "product": "MIS", "order_type": order_type, "validity": "DAY"}
    if price is not None:
        c["price"] = price
    if trigger is not None:
        c["trigger_price"] = trigger
    c["tag"] = tag
    return c


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=ITC, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes ITC", "yes ITC.NS", "yes 61 ITC",
                  "yes 61"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "haan", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes TCS.NS", "yes 60", "yes but make it 50", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 280"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = dict(ITC, side="sell", qty="3")
        self.assertTrue(self.yes("yes sell 3 ITC", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_bracket_yes(self):
        o = infy()
        for r in ("yes", "yes 41 INFY", "yes 1149.80", "yes max loss 489", "yes target 1181.90"):
            self.assertTrue(self.yes(r, o, ["INFY.NS"]), r)
        for r in ("yes 42", "yes 50 INFY", "yes but stop at 1148", "yes, stop 1149.80", "yes TCS", "sure"):
            self.assertFalse(self.yes(r, o, ["INFY.NS", "TCS.NS"]), r)


class TicketShape(unittest.TestCase):
    def test_a_plain_order_is_one_kite_call(self):
        tag = "dts" + ticket.fingerprint(ITC)[:8]
        self.assertEqual(ticket.tool_input(dict(ITC, why="from a list")),
                         [{"step": "entry", "when": "now", "tool": "place_order", "input": {
                             "variety": "regular", "exchange": "NSE", "tradingsymbol": "ITC", "transaction_type": "BUY",
                             "quantity": 61, "product": "CNC", "order_type": "MARKET", "validity": "DAY", "tag": tag}}])
        lim = dict(ITC, type="limit", limit_price="283.00", side="sell")
        call = ticket.tool_input(lim)[0]
        self.assertEqual((call["step"], call["input"]["order_type"], call["input"]["price"],
                          call["input"]["transaction_type"]), ("order", "LIMIT", 283.0, "SELL"))

    def test_the_bracket_is_three_kite_orders(self):
        o = infy()
        tag = "dts" + ticket.fingerprint(o)[:8]
        self.assertEqual(ticket.tool_input(o), [
            {"step": "entry", "when": "now", "tool": "place_order", "input": kite("BUY", "MARKET", tag)},
            {"step": "stop-loss", "when": "after the entry fills", "tool": "place_order",
             "input": kite("SELL", "SL-M", tag, trigger=1149.8)},
            {"step": "target", "when": "after the entry fills", "tool": "place_order",
             "input": kite("SELL", "LIMIT", tag, price=1181.9)}])
        sl = ticket.tool_input(infy(stop_loss_limit_price="1148.00"))[1]["input"]
        self.assertEqual((sl["order_type"], sl["trigger_price"], sl["price"]), ("SL", 1149.8, 1148.0))
        self.assertLessEqual(len(tag), 20)                            # Kite's tag limit
        self.assertEqual(ticket.fingerprint(dict(o, why="x", plan={})), ticket.fingerprint(o))

    def test_plain_words(self):
        self.assertEqual(ticket.plain(ITC), "BUY 61 shares of ITC.NS, market order, delivery (CNC), good for today")
        self.assertEqual(ticket.plain(dict(ITC, side="sell", qty="1", type="limit", limit_price="295")),
                         "SELL 1 share of ITC.NS, limit order at ₹295, delivery (CNC), good for today")
        self.assertEqual(ticket.plain(infy()), INFY_PLAIN)
        lim = infy(type="limit", limit_price="1160.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 41 INFY.NS, limit 1160.00, intraday (MIS), good for today")
        self.assertEqual(ticket.max_loss(lim), (468.5, 1149.8))       # 41 x 10.20 + 50.30 charges
        no_plan = {k: v for k, v in infy().items() if k != "plan"}
        self.assertEqual(ticket.max_loss(no_plan), (None, 1149.8))

    def test_tradingsymbol(self):
        self.assertEqual([ticket.tradingsymbol(s) for s in ("INFY.NS", "m&m.ns", "BAJAJ-AUTO.NS")],
                         ["INFY", "M&M", "BAJAJ-AUTO"])


class DesktopConfig(unittest.TestCase):
    def test_example_is_a_self_hosted_kite_server_with_placeholder_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["kite"]
        self.assertEqual(server["env"]["APP_MODE"], "stdio")
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NEVER_IN_CHAT", server["env"][k])
        self.assertNotIn("place_order", server["env"]["EXCLUDED_TOOLS"])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def live(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["paper"] = False
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--bands", self.none, "--now", now,
                               "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def portfolio(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def paper_buy(self):
        self.assertEqual(self.run_risk(), 0)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes"))[0], 0)
        code, text, err = run(self.args("paper", "--index", "1", "--price", "1160.50", "--portfolio", self.pf,
                                        "--prices", self.none, now="2026-11-05 09:41"))
        self.assertEqual(code, 0, err)
        return text

    def check_paper(self, price, now):
        return run(["paper-check", "--price", "INFY.NS=%s" % price, "--prices", self.none, "--portfolio", self.pf,
                    "--config", self.cfg, "--state", self.state, "--now", now])

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_paper_ticket(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER account (filled here, no broker)", text)
        for part in INFY_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn('stop-loss (after the entry fills): {"variety": "regular"', text)
        self.assertIn('"order_type": "SL-M", "validity": "DAY", "trigger_price": 1149.8', text)
        self.assertIn("Kite has no bracket for stocks", text)
        self.assertIn('Reply "yes" to paper trade this one order', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertNotIn("Allow once", text)                          # no broker card on paper

    def test_a_paper_day_trade_closed_at_the_flat_by(self):
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        text = self.paper_buy()
        self.assertIn("PAPER BUY 41 INFY.NS at 1160.50 (₹20.05 charges)", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 489.0, TODAY))
        self.assertEqual(appr["at"], "2026-11-05T09:40:00+05:30")
        self.assertTrue(plc["broker_order_id"].startswith("PAPER-"))
        self.assertEqual(st["paper_legs"][0]["status"], "open")
        self.assertEqual(self.portfolio()["cash"], 52399.45)           # 1,00,000 - 47,580.50 - 20.05
        self.assertEqual(self.portfolio()["positions"][0]["shares"], 41)

        code, text, _ = self.check_paper("1171.90", "2026-11-05 09:55")   # the day's high, short of 2R
        self.assertIn("between the stop 1149.80 and the target 1181.90, still open", text)
        code, text, _ = self.check_paper("1164.90", "2026-11-05 15:10")
        self.assertIn("past flat_by 15:10: ticket.py flatten --symbol INFY.NS", text)

        late = "2026-11-05 15:10"
        code, text, _ = run(["flatten", "--symbol", "INFY.NS", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 41 shares of INFY.NS, market order, intraday (MIS), good for today (flat by 15:10 IST)",
                      text)
        self.assertIn("ticket.py paper --index 1", text)
        self.assertEqual(self.run_risk(now=late), 0)       # after the 14:45 entry cutoff: an exit still passes
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 41 INFY", now=late))[0], 0)
        code, text, err = run(self.args("paper", "--index", "1", "--price", "1164.90", "--portfolio", self.pf,
                                        "--prices", self.none, now=late))
        self.assertEqual(code, 0, err)
        self.assertIn("PAPER SELL 41 INFY.NS at 1164.90 (₹30.64 charges)", text)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"], pf["charges_paid"]), (100129.71, [], 50.69))
        self.assertEqual(self.state_doc()["paper_legs"][0]["status"], "canceled")
        code, text, _ = run(["log", "--state", self.state, "--now", late])
        self.assertIn("2 placed today", text)
        self.assertIn("INFY.NS: P&L after ₹50.69 charges +₹129.71", text)   # 41 x 4.40 = 180.40 - 50.69
        code, _, err = run(["flatten", "--symbol", "INFY.NS", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_the_paper_stop_fills_at_the_market(self):
        self.paper_buy()
        code, text, _ = self.check_paper("1148.00", "2026-11-05 10:40")
        self.assertIn("PAPER stop-loss filled: SELL 41 INFY.NS at 1148.00", text)
        self.assertIn("under the stop 1149.80: an SL-M fills at the market", text)
        self.assertIn("the target is canceled", text)
        self.assertIn("INFY.NS: closed, P&L after ₹50.24 charges -₹562.74", text)   # -512.50 - 50.24
        code, text, _ = self.check_paper("1190.00", "2026-11-05 11:00")
        self.assertIn("No open paper orders", text)

    def test_the_paper_target_fills_at_its_limit(self):
        self.paper_buy()
        code, text, _ = self.check_paper("1182.50", "2026-11-05 11:00")
        self.assertIn("PAPER target filled: SELL 41 INFY.NS at 1181.90", text)
        self.assertIn("P&L after ₹51.13 charges +₹826.27", text)       # the gain trader-budget showed

    def test_paper_needs_a_yes_and_a_price(self):
        self.run_risk()
        code, _, err = run(self.args("paper", "--index", "1", "--price", "1160.50", "--portfolio", self.pf))
        self.assertIn("approve comes first", err)
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, _, err = run(self.args("paper", "--index", "1", "--portfolio", self.pf, "--prices", self.none))
        self.assertEqual(code, 1)
        self.assertIn("no live price for INFY.NS", err)
        prices = os.path.join(self.d, "prices.json")
        with open(prices, "w") as fh:
            json.dump({"prices": {"INFY.NS": 1160.6}, "detail": {"INFY.NS": {"time": "2026-11-05T04:11:00Z"}}}, fh)
        code, text, err = run(self.args("paper", "--index", "1", "--portfolio", self.pf, "--prices", prices,
                                        now="2026-11-05 09:42"))
        self.assertEqual(code, 0, err)
        self.assertIn("at 1160.60", text)

    def test_a_paper_limit_buy_waits_for_its_price(self):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0].update(type="limit", limit_price="1160.00")
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.args("paper", "--index", "1", "--price", "1160.50", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("Not filled: INFY.NS is at 1160.50, above your limit 1160.00", text)
        code, text, _ = run(self.args("paper", "--index", "1", "--price", "1159.20", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("PAPER BUY 41 INFY.NS at 1160.00", text)

    def test_a_live_ticket_and_the_kite_flow(self):
        self.live()
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("LIVE ACCOUNT, REAL MONEY, Zerodha Kite", text)
        self.assertIn("Claude will call the Kite connector's place_order with exactly:", text)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        first = text[text.index("{"):text.index("}") + 1]
        tag = "dts" + ticket.fingerprint(infy())[:8]
        self.assertEqual(json.loads(first), kite("BUY", "MARKET", tag))
        self.assertIn("When the entry fills, the stop-loss: place_order", text)
        code, _, err = run(self.args("paper", "--index", "1", "--price", "1160.50", "--portfolio", self.pf))
        self.assertIn("LIVE account", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id",
                                      "251105000123456", "--filled-qty", "41", "--filled-price", "1160.60",
                                      "--portfolio", self.pf, now="2026-11-05 09:41"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        self.assertEqual((st["orders"][1]["broker_order_id"], st["orders"][1]["max_loss"]), ("251105000123456", 489.0))
        self.assertEqual(self.portfolio()["cash"], 52395.34)          # 1,00,000 - 47,584.60 - 20.06
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        # trader-risk now counts it: a second INFY buy today is a duplicate, and 489 is used
        res = check.check([infy()], {"budget": 100000}, state=st, now=check.parse_now("2026-11-05 10:00"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 489.0))
        code, text, _ = run(["flatten", "--symbol", "INFY.NS", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", "2026-11-05 15:10"])
        self.assertIn("cancel_order", text)
        self.assertIn("3:20 PM", text)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "infy.ns", "--qty", "41", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against the broker", text)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual((o["exchange"], o["product"], o["qty"]), ("NSE", "MIS", "41"))
        self.assertEqual(run(["flatten", "--symbol", "INFY.NS", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "42"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-11-04 09:40")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-11-05 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-05 09:20"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-11-05 09:20"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_fill_sell_relieves_the_oldest_lot_and_pays_charges(self):
        pf = {"cash": 0, "positions": [{"ticker": "ITC.NS", "shares": 3, "lots": [
            {"id": "ITC-1", "date": "2025-01-02", "shares": 2, "cost": 900.0},
            {"id": "ITC-2", "date": "2026-01-02", "shares": 1, "cost": 280.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "ITC.NS", "--side", "sell", "--qty", "2", "--price", "300",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("about ₹0.39 charges", text)                     # 0.18 + 0.15 STT + 0.02 + 0.04 GST
        pf = self.portfolio()
        self.assertEqual(pf["cash"], 599.61)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["ITC-2"])
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        self.assertEqual(self.state_doc()["orders"][-1]["charges"], 0.39)
        code, _, err = run(["fill", "--symbol", "ITC.NS", "--side", "sell", "--qty", "5", "--price", "300",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
