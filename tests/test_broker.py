import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
forward = load("trader-broker", "forward")
edge = load("trader-timemachine", "edge")
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


class FlowBase(unittest.TestCase):
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
        self.forward = os.path.join(self.d, "forward.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def live(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["paper"] = False
        # the first 20 live trades go at 25 % size: with a 2,000 limit that is the reel's 500
        c["max_loss_per_trade_inr"] = 2000
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        # a forward test that PASSED on both the shadow replay and paper, today: the live lock opens
        with open(self.forward, "w") as fh:
            json.dump({"expected": {"avg_R": 0.08, "low": -0.1}, "verdicts": {
                "shadow": {"state": "passed", "date": TODAY, "passed_on": TODAY},
                "paper": {"state": "passed", "date": TODAY, "passed_on": TODAY}}}, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--bands", self.none, "--now", now,
                               "--out", self.risk, "--limits-state", os.path.join(self.d, "limits.json"),
                               "--forward", self.forward])

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

class Flow(FlowBase):
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
        code, text, _ = run(["journal", "--state", self.state, "--paper", "--config", self.cfg])
        self.assertIn("+₹129.71", text)
        self.assertIn("+0.30  paper", text)                                  # 129.71 / 438.70 at the stop
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

    def test_reconcile_on_paper(self):
        self.paper_buy()
        code, text, _ = run(["reconcile", "--paper", "--state", self.state, "--portfolio", self.pf,
                             "--config", self.cfg, "--now", "2026-11-05 10:00"])
        self.assertEqual(code, 0, text)
        self.assertIn("OK       INFY.NS: long 41, stop SL-M 1149.80, target 1181.90", text)
        self.check_paper("1182.50", "2026-11-05 11:00")
        code, text, _ = run(["reconcile", "--paper", "--state", self.state, "--portfolio", self.pf,
                             "--config", self.cfg, "--now", "2026-11-05 11:05"])
        self.assertEqual(code, 0)
        self.assertIn("nothing open", text)

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


class ShortFlow(FlowBase):
    """trader-budget's mirrored-reel short on paper: sell 41 INFY.NS at 1139.50, stop 1150.20, target 1118.10."""

    def setUp(self):
        super().setUp()
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["allow_short"] = True
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        order = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "41",
                 "type": "market", "time_in_force": "day", "order_class": "bracket",
                 "take_profit_limit_price": "1118.10", "stop_loss_stop_price": "1150.20",
                 "plan": {"entry": 1139.5, "stop": 1150.2, "target": 1118.1, "max_loss": 488.55, "flat_by": "15:10"}}
        with open(self.orders, "w") as fh:
            json.dump({"orders": [order]}, fh)

    def paper_short(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("SELL SHORT 41 INFY.NS, market", text)
        self.assertIn('"transaction_type": "BUY", "quantity": 41, "product": "MIS", "order_type": "SL-M"', text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes"))[0], 0)
        code, text, err = run(self.args("paper", "--index", "1", "--price", "1139.50", "--portfolio", self.pf,
                                        "--prices", self.none, now="2026-11-05 09:41"))
        self.assertEqual(code, 0, err)
        return text

    def test_the_kite_calls_reverse(self):
        with open(self.orders) as fh:
            order = json.load(fh)["orders"][0]
        calls = ticket.tool_input(order)
        self.assertEqual([(c["step"], c["input"]["transaction_type"], c["input"]["order_type"]) for c in calls],
                         [("entry", "SELL", "MARKET"), ("stop-loss", "BUY", "SL-M"), ("target", "BUY", "LIMIT")])
        self.assertEqual(ticket.max_loss(order), (488.55, 1150.2))

    def test_a_paper_short_to_its_target(self):
        text = self.paper_short()
        # the sale pays STT: 14.02 + 11.68 + 1.43 + 0.05 + 2.79 = 29.97; the cash comes in
        self.assertIn("PAPER SELL SHORT 41 INFY.NS at 1139.50 (₹29.97 charges)", text)
        self.assertIn("Open paper orders: buy-back stop-loss 1150.20 and target 1118.10", text)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"]), (146689.53, -41))   # 1,00,000 + 46,719.50 - 29.97
        code, text, _ = self.check_paper("1125.00", "2026-11-05 10:30")
        self.assertIn("between the stop 1150.20 and the target 1118.10, still open", text)
        code, text, _ = self.check_paper("1118.00", "2026-11-05 11:00")
        self.assertIn("PAPER target filled: BUY 41 INFY.NS at 1118.10", text)
        # 41 x 21.40 = 877.40 less 49.30 charges: trader-budget's gain at the target
        self.assertIn("P&L after ₹49.30 charges +₹828.10", text)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"]), (100828.1, []))
        code, text, _ = run(["journal", "--state", self.state, "--paper", "--config", self.cfg, "--json"])
        t = json.loads(text)["trades"][0]
        self.assertEqual((t["side"], t["net"], t["risk"], t["R"]), ("short", 828.1, 438.7, 1.888))

    def test_a_paper_short_stopped_out_above_the_stop(self):
        self.paper_short()
        code, text, _ = self.check_paper("1150.60", "2026-11-05 10:40")
        self.assertIn("PAPER stop-loss filled: BUY 41 INFY.NS at 1150.60", text)
        self.assertIn("over the stop 1150.20: an SL-M fills at the market", text)
        self.assertIn("INFY.NS: closed, P&L after ₹49.86 charges -₹504.96", text)   # -455.10 - 49.86

    def test_flattening_a_short_buys_it_back(self):
        self.paper_short()
        late = "2026-11-05 15:10"
        code, text, _ = run(["flatten", "--symbol", "INFY.NS", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", self.none, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("BUY 41 shares of INFY.NS, market order", text)
        self.assertIn("buys back", text)
        self.assertEqual(self.run_risk(now=late), 0)            # buying back a short is an exit
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now=late))[0], 0)
        code, text, err = run(self.args("paper", "--index", "1", "--price", "1135.00", "--portfolio", self.pf,
                                        "--prices", self.none, now=late))
        self.assertEqual(code, 0, err)
        self.assertIn("PAPER BUY 41 INFY.NS at 1135.00", text)
        self.assertEqual(self.portfolio()["positions"], [])
        self.assertEqual(self.state_doc()["paper_legs"][0]["status"], "canceled")
        code, text, _ = run(["flatten", "--symbol", "INFY.NS", "--qty", "41", "--short", "--orders", self.orders,
                             "--portfolio", self.pf, "--config", self.cfg, "--now", late])
        self.assertIn("BUY 41 shares of INFY.NS", text)

    def test_shorts_off_in_the_config(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["allow_short"] = False
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        self.assertEqual(self.run_risk(), 1)                     # trader-risk blocks it


class Reconcile(unittest.TestCase):
    """The broker's view (Kite's get_positions and get_orders shapes) against today's approved entry."""
    FP = "a8795375" + "0" * 56
    TAG = "dtsa8795375"

    def state(self, side="buy", qty="41", stop=1149.8):
        return {"orders": [{"status": "approved", "date": TODAY, "symbol": "INFY.NS", "side": side, "qty": qty,
                            "kind": "entry", "stop": stop, "fingerprint": self.FP}]}

    def order(self, oid, side, typ, qty, status, trigger=None, price=None, tag=None, filled=0):
        return {"order_id": oid, "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": side,
                "order_type": typ, "quantity": qty, "pending_quantity": 0 if status == "COMPLETE" else qty,
                "filled_quantity": filled, "status": status, "trigger_price": trigger, "price": price,
                "tag": tag or self.TAG}

    def run_(self, qty, orders, now="10:30", state=None):
        pos, book, prod = ticket.kite_book({"net": [{"exchange": "NSE", "tradingsymbol": "INFY", "quantity": qty,
                                                      "product": "MIS"}] if qty else []}, {"data": orders})
        return ticket.reconcile(pos, book, state or self.state(), TODAY, now, "15:10", prod)

    def entry(self, oid="1", side="BUY"):
        return self.order(oid, side, "MARKET", 41, "COMPLETE", filled=41)

    def test_a_protected_long_is_ok(self):
        r = self.run_(41, [self.entry(), self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1149.8),
                           self.order("3", "SELL", "LIMIT", 41, "OPEN", price=1181.9)])
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["ok"], ["INFY.NS: long 41, stop SL-M 1149.80, target 1181.90"])

    def test_an_unprotected_position(self):
        r = self.run_(41, [self.entry(), self.order("2", "SELL", "SL-M", 41, "REJECTED", trigger=1149.8)])
        pr = r["problems"][0]
        self.assertEqual(pr["what"], "unprotected: long 41 INFY.NS with no open stop-loss")
        fix = json.loads(pr["fix"][len("place_order "):])
        self.assertEqual((fix["transaction_type"], fix["order_type"], fix["quantity"], fix["trigger_price"]),
                         ("SELL", "SL-M", 41, 1149.8))

    def test_an_orphan_stop_after_the_target_filled(self):
        # the target sold the 41 shares; the stop is still waiting and would open a short
        r = self.run_(0, [self.entry(), self.order("3", "SELL", "LIMIT", 41, "COMPLETE", price=1181.9, filled=41),
                          self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1149.8)])
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("orphan: SL-M SELL 41 INFY.NS is still open but INFY.NS is flat; if it fills it opens a new "
                      "short position", r["problems"][0]["what"])
        self.assertEqual(r["problems"][0]["fix"], "cancel_order 2 (Needs approval)")

    def test_a_double_entry(self):
        r = self.run_(82, [self.entry("1"), self.entry("4"),
                           self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1149.8)])
        what = [p["what"] for p in r["problems"]]
        self.assertTrue(any(w.startswith("duplicate: 2 entry orders with tag dtsa8795375 filled (1, 4)") for w in what))
        self.assertIn("under-protected: the stop covers 41 of 82 shares", what)
        self.assertIn("INFY.NS: 82 shares held, 41 approved today", r["warnings"])

    def test_a_pending_entry_must_not_be_retried(self):
        r = self.run_(0, [self.order("1", "BUY", "MARKET", 41, "OPEN PENDING")])
        self.assertIn("Do NOT place it again", r["warnings"][0])

    def test_a_stop_moved_away(self):
        r = self.run_(41, [self.entry(), self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1140.0)])
        self.assertEqual(r["problems"][0]["what"], "stop moved away: order 2 triggers at 1140.00, the plan's stop "
                                                   "is 1149.80, so the loss can be bigger than the one you approved")
        # moved CLOSER to the entry is fine
        r = self.run_(41, [self.entry(), self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1155.0)])
        self.assertEqual(r["problems"], [])

    def test_a_short(self):
        st = self.state(side="sell", stop=1150.2)
        ok = self.run_(-41, [self.entry(side="SELL"),
                             self.order("2", "BUY", "SL-M", 41, "TRIGGER PENDING", trigger=1150.2)], state=st)
        self.assertEqual(ok["ok"], ["INFY.NS: short 41, stop SL-M 1150.20, no target"])
        bad = self.run_(-41, [self.entry(side="SELL"),
                              self.order("2", "BUY", "SL-M", 41, "TRIGGER PENDING", trigger=1160.0)], state=st)
        self.assertIn("stop moved away", bad["problems"][0]["what"])
        bad = self.run_(-41, [self.entry(side="SELL"),
                              self.order("2", "BUY", "SL-M", 82, "TRIGGER PENDING", trigger=1150.2)], state=st)
        self.assertIn("over-protected: stops for 82 shares on a 41-share position; the extra would open a long",
                      bad["problems"][0]["what"])

    def test_past_the_flat_by(self):
        r = self.run_(41, [self.entry(), self.order("2", "SELL", "SL-M", 41, "TRIGGER PENDING", trigger=1149.8)],
                      now="15:12")
        self.assertIn("past the flat-by 15:10 with long 41 still open", r["problems"][0]["what"])

    def test_the_cli_live_and_paper(self):
        d = tempfile.mkdtemp()
        try:
            posf, ordf, stf = (os.path.join(d, x) for x in ("pos.json", "ord.json", "state.json"))
            with open(posf, "w") as fh:
                json.dump({"data": {"net": [{"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 41,
                                             "product": "MIS"}]}}, fh)
            with open(ordf, "w") as fh:
                json.dump({"data": [self.entry()]}, fh)
            with open(stf, "w") as fh:
                json.dump(self.state(), fh)
            code, text, err = run(["reconcile", "--positions", posf, "--broker-orders", ordf, "--state", stf,
                                   "--config", os.path.join(d, "none"), "--now", "2026-11-05 10:30"])
            self.assertEqual(code, 1, err)
            self.assertIn("PROBLEM  INFY.NS: unprotected: long 41 INFY.NS with no open stop-loss", text)
            self.assertIn('Fix: place_order {"variety": "regular"', text)
            code, _, err = run(["reconcile", "--positions", posf, "--state", stf, "--config", os.path.join(d, "none")])
            self.assertEqual(code, 1)
            self.assertIn("get_positions and get_orders", err)
            # paper: an open paper position with its legs is OK; no portfolio at all is nothing open
            code, text, _ = run(["reconcile", "--paper", "--state", stf, "--portfolio", os.path.join(d, "none"),
                                 "--config", os.path.join(d, "none"), "--now", "2026-11-05 10:30"])
            self.assertEqual(code, 0)
            self.assertIn("nothing open", text)
        finally:
            shutil.rmtree(d)


class Forward(unittest.TestCase):
    """A frozen rule judged on the trades after it. Each made-up trade: 10 shares bought at 100 with the stop at
    99 (1 R = 10 rupees), sold at 102 (a win, about +1.9 R after charges) or at 99 (a loss, about -1.09 R)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f, self.st = os.path.join(self.d, "forward.json"), os.path.join(self.d, "broker.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def trade(self, day, sym, sell, stop=99.0):
        return [{"status": "approved", "date": day, "symbol": sym, "side": "buy", "kind": "entry", "qty": "10",
                 "stop": stop},
                {"status": "filled", "date": day, "symbol": sym, "side": "buy", "qty": "10", "price": 100.0,
                 "paper": True},
                {"status": "filled", "date": day, "symbol": sym, "side": "sell", "qty": "10", "price": sell,
                 "paper": True}]

    def days(self, n):
        d = datetime.date(2026, 11, 10)
        return [(d + datetime.timedelta(days=i)).isoformat() for i in range(n)]

    def state(self, sells):
        orders = []
        for day, sell in zip(self.days(len(sells)), sells):
            orders += self.trade(day, "ABC.NS", sell)
        with open(self.st, "w") as fh:
            json.dump({"orders": orders}, fh)

    def start(self, *extra):
        return run_fwd(["start", "--rule", "orb30 short tight 2R low_cost+with_nifty", "--file", self.f,
                        "--today", "2026-11-10"] + list(extra))

    def status(self):
        return run_fwd(["status", "--file", self.f, "--state", self.st, "--config", os.path.join(self.d, "none"),
                        "--today", "2026-12-31"])

    def test_start_from_the_edge_report(self):
        edge_doc = {"sessions": 58, "rows": [{"rule": {"setup": "orb30", "side": "short", "stop": "tight",
                                                       "target_r": 2.0}, "filter": "low_cost+with_nifty",
                                              "all": {"avg_R": 0.076, "trades": 309}, "in_sample": {"avg_R": 0.064},
                                              "out_of_sample": {"avg_R": 0.096},
                                              "bootstrap": {"ci95": [-0.145, 0.246], "p": 0.219}}]}
        ep = os.path.join(self.d, "edge.json")
        with open(ep, "w") as fh:
            json.dump(edge_doc, fh)
        code, text, err = self.start("--edge", ep)
        self.assertEqual(code, 0, err)
        self.assertIn("Expected: +0.08 R a trade after charges, low end -0.14 R", text)
        self.assertIn("no verdict before 40 trades", text)
        code, _, err = self.start("--edge", ep)
        self.assertEqual(code, 1)
        self.assertIn("already running", err)
        code, _, err = run_fwd(["start", "--rule", "orb30 sideways tight 2R", "--file", os.path.join(self.d, "x.json")])
        self.assertIn("side is long or short", err)

    def test_too_early_then_running(self):
        self.start("--expect-r", "0.1", "--expect-low", "-0.1")
        self.state([102.0, 99.0, 102.0])
        code, text, _ = self.status()
        self.assertEqual(code, 0)
        self.assertIn("Too early: 3 trades of 40", text)
        self.state([102.0, 99.0, 99.0] * 5)
        self.assertIn("Running: 15 of 40 trades", self.status()[1])

    def test_passed_and_stopped_at_40(self):
        self.start("--expect-r", "0.1", "--expect-low", "-0.1", "--max-drawdown-r", "100")
        self.state([102.0, 99.0] * 20)                                    # 20 wins, 20 losses: about +0.4 R
        res = forward.evaluate(json.load(open(self.f)), ticket.round_trips(json.load(open(self.st)), "2026-11-10",
                                                                          paper=True)[0])
        self.assertEqual(res["trades"], 40)
        self.assertGreater(res["avg_R"], 0)
        self.assertEqual(res["state"], "passed")
        self.assertIn("PASSED: after 40 trades", self.status()[1])
        self.state([102.0, 99.0, 99.0, 99.0] * 10)                        # 10 wins, 30 losses: about -0.34 R
        code, text, _ = self.status()
        self.assertEqual(code, 1)
        self.assertIn("STOP: after 40 trades the average is", text)
        self.assertIn("do not re-tune it on these same trades", text)

    def test_the_drawdown_stop_comes_early(self):
        self.start("--expect-r", "0.1", "--expect-low", "-0.1", "--max-drawdown-r", "5")
        self.state([99.0] * 5)                                            # 5 x -1.09 R
        code, text, _ = self.status()
        self.assertEqual(code, 1)
        self.assertIn("past the 5 R limit set at the start", text)

    def test_off_plan_trades_and_skipped_signals(self):
        self.start("--expect-r", "0.1", "--expect-low", "-0.1")
        orders = self.trade("2026-11-10", "ABC.NS", 98.0) + self.trade("2026-11-11", "XYZ.NS", 102.0)
        with open(self.st, "w") as fh:
            json.dump({"orders": orders}, fh)
        run_fwd(["signal", "ABC", "--taken", "--date", "2026-11-10", "--file", self.f])
        for sym in ("P", "Q"):
            run_fwd(["signal", sym, "--skipped", "--reason", "felt wrong", "--date", "2026-11-11", "--file", self.f])
        code, _, err = run_fwd(["signal", "R", "--skipped", "--file", self.f])
        self.assertIn("needs --reason", err)
        text = self.status()[1]
        self.assertIn("2026-11-10 ABC.NS: lost 2.", text)                 # sold at 98: about -2.1 R
        self.assertIn("2026-11-11 XYZ.NS: no logged signal of this rule that day", text)
        self.assertIn("Signals: 1 taken, 2 skipped (felt wrong)", text)
        self.assertIn("this tests your picks, not the rule", text)


class Shadow(unittest.TestCase):
    """forward.py shadow on the edge report's real fixtures: five Nifty 50 stocks, Nifty and India VIX."""
    EDGE = os.path.join(FIX, "trader-timemachine", "edge")
    FIVE = "INFY.NS,TCS.NS,HDFCBANK.NS,RELIANCE.NS,SBIN.NS"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "forward.json")
        run_fwd(["start", "--rule", "orb15 long tight 2R none", "--expect-r", "0.05", "--expect-low", "-0.1",
                 "--file", self.f, "--today", "2026-10-09"])

    def tearDown(self):
        shutil.rmtree(self.d)

    def shadow(self, *extra):
        return run_fwd(["shadow", "--symbols", self.FIVE, "--offline", self.EDGE, "--file", self.f,
                        "--config", os.path.join(self.d, "none"), "--today", "2026-11-06"] + list(extra))

    def test_it_takes_the_first_three_signals_of_each_day(self):
        code, text, err = self.shadow("--from", "2026-10-01")             # never before the 9 October start
        self.assertIn("Shadow so far: 20 sessions", text, err)
        with open(self.f) as fh:
            sess = json.load(fh)["shadow"]["sessions"]
        self.assertEqual(min(sess), "2026-10-09")
        # the same trades edge.py takes, the earliest three a day
        charts = {s: edge.read_offline(self.EDGE, s) for s in self.FIVE.split(",")}
        cfg = {"slippage_ticks": 1, "first_slot": edge.hhmm_slot("09:30", "x"),
               "last_entry_slot": edge.hhmm_slot("14:45", "x"), "flat_slot": edge.hhmm_slot("15:10", "x"),
               "max_loss": 500.0, "max_order": 75000.0, "max_cost_pct": 15, "dates": set(sess)}
        mine = {}
        for sym, text_ in charts.items():
            for t in edge.trades_for(sym, edge.prepare(edge.sessions_of(text_, sym), {}, {}, cfg),
                                     "orb15", "long", "tight", 2.0, cfg):
                mine.setdefault(t["date"], []).append(t)
        for day, ts in sess.items():
            want = sorted(mine.get(day, []), key=lambda t: (t["slot"], t["symbol"]))[:3]
            self.assertEqual([(t["symbol"], t["entry"], t["net"]) for t in ts],
                             [(t["symbol"], t["entry"], t["pnl"]) for t in want], day)
        self.assertTrue(any(len(ts) == 3 for ts in sess.values()))

    def test_a_session_replayed_again_is_not_doubled(self):
        self.shadow("--date", "2026-11-05")
        self.shadow("--date", "2026-11-05")
        with open(self.f) as fh:
            sh = json.load(fh)["shadow"]["sessions"]
        self.assertEqual(list(sh), ["2026-11-05"])
        self.assertLessEqual(len(sh["2026-11-05"]), 3)

    def test_status_shows_the_shadow(self):
        self.shadow("--from", "2026-10-09", "--per-day", "1")
        code, text, _ = run_fwd(["status", "--file", self.f, "--state", os.path.join(self.d, "none"),
                                 "--config", os.path.join(self.d, "none"), "--today", "2026-11-06"])
        self.assertIn("Shadow (every signal, the first 1 a day, replayed after the close): 20 sessions", text)

    def test_before_the_start_is_refused(self):
        code, _, err = self.shadow("--date", "2026-10-01")
        self.assertEqual(code, 1)
        self.assertIn("no sessions from 2026-10-09", err)


def run_fwd(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = forward.main(argv)
    return code, out.getvalue(), err.getvalue()


class JournalAndTax(unittest.TestCase):
    """Closed trades from trader/state/broker.json. The log below is made up, so each number can be
    worked by hand: three INFY.NS round trips, one of them in FY26."""

    def fill(self, day, side, qty, price, paper=False):
        return {"at": day + "T10:00:00+05:30", "date": day, "status": "filled", "symbol": "INFY.NS", "side": side,
                "qty": str(qty), "price": price, "paper": paper}

    def state(self):
        yes = {"date": "2026-11-05", "status": "placed", "symbol": "INFY.NS", "side": "buy", "stop": 1149.8,
               "at": "2026-11-05T09:41:00+05:30"}
        return {"orders": [
            self.fill("2026-03-30", "buy", 10, 1000.0), self.fill("2026-03-30", "sell", 10, 990.0),     # FY26
            yes, self.fill("2026-11-05", "buy", 41, 1160.5), self.fill("2026-11-05", "sell", 41, 1164.9),
            self.fill("2026-11-06", "buy", 20, 1180.0), self.fill("2026-11-06", "sell", 10, 1170.0),
            self.fill("2026-11-06", "sell", 10, 1171.0),
            self.fill("2026-11-09", "buy", 5, 1175.0)]}                                               # still open

    def test_round_trips(self):
        trades, still_open = ticket.round_trips(self.state(), "2026-04-01", "2027-03-31")
        self.assertEqual([(t["date"], t["qty"], t["gross"]) for t in trades],
                         [("2026-11-05", 41.0, 180.4), ("2026-11-06", 20.0, -190.0)])
        first = trades[0]
        self.assertEqual(first["charges"]["total"], 50.69)               # 20.05 on the buy + 30.64 on the sell
        self.assertEqual(first["net"], 129.71)
        self.assertEqual(first["risk"], 438.7)                           # 41 x (1160.50 - 1149.80)
        self.assertEqual(first["R"], 0.296)
        self.assertIsNone(trades[1]["R"])                                # no stop was recorded for it
        self.assertEqual(still_open, [{"date": "2026-11-09", "symbol": "INFY.NS", "qty": 5.0}])

    def test_journal_and_tax_from_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "broker.json")
            with open(st, "w") as fh:
                json.dump(self.state(), fh)
            code, text, _ = run(["journal", "--state", st, "--config", os.path.join(d, "none")])
            self.assertEqual(code, 0)
            self.assertIn("3 trades, 1 won (33.3 %)", text)
            self.assertIn("longest run of losing trades 1", text)
            self.assertIn("Still open: 5 INFY.NS on 2026-11-09", text)
            self.assertIn("Under 30 trades says little", text)
            code, text, _ = run(["journal", "--state", st, "--from", "2026-11-01", "--json"])
            j = json.loads(text)["summary"]
            self.assertEqual((j["trades"], j["wins"], j["longest_losing_streak"]), (2, 1, 1))
            self.assertEqual(j["by_day"]["2026-11-05"], 129.71)
            csvp = os.path.join(d, "fy27.csv")
            code, text, _ = run(["tax", "--fy", "FY27", "--state", st, "--csv", csvp])
            self.assertEqual(code, 0)
            self.assertIn("FY27 (2026-04-01 to 2027-03-31), intraday equity: speculative business income.", text)
            self.assertIn("2 round trips. Gross P&L -₹9.60.", text)               # 180.40 - 190.00
            self.assertIn("Turnover for the tax-audit limits", text)
            self.assertIn("₹370.40", text)                                         # 180.40 + 190.00, without signs
            self.assertIn("not tax advice", text)
            with open(csvp) as fh:
                rows = fh.read().splitlines()
            self.assertEqual(len(rows), 3)
            self.assertTrue(rows[1].startswith("2026-11-05,INFY.NS,41,47580.50,47760.90,180.40,"))
            code, text, _ = run(["tax", "--fy", "FY26", "--state", st, "--json"])
            self.assertEqual((json.loads(text)["trades"], json.loads(text)["gross"]), (1, -100.0))
            self.assertEqual(run(["tax", "--fy", "2027", "--state", st])[0], 1)

    def test_paper_is_kept_apart(self):
        st = {"orders": [self.fill("2026-11-05", "buy", 41, 1160.5, True), self.fill("2026-11-05", "sell", 41, 1164.9, True)]}
        self.assertEqual(ticket.round_trips(st, paper=False)[0], [])
        self.assertEqual(len(ticket.round_trips(st, paper=True)[0]), 1)
        self.assertEqual(ticket.fy_bounds("FY27"), ("2026-04-01", "2027-03-31"))
        self.assertEqual(ticket.fy_bounds("fy 2026"), ("2025-04-01", "2026-03-31"))


if __name__ == "__main__":
    unittest.main()
