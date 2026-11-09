import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-infy.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
TOP5 = os.path.join(FIX, "trader-budget", "berkshire-q2-2026-top5.json")   # arithmetic test data for the weights mode
NOW = "2026-11-05 09:40"
TODAY = "2026-11-05"
REEL_V1 = {"AAPL": 1250, "AXP": 1119, "GOOGL": 823, "KO": 708, "BAC": 600}
PRICES = {"AAPL": 341.27, "AXP": 309.84, "GOOGL": 353.78, "KO": 90.11, "BAC": 53.96}
# the NSE config, key for key
CONTRACT_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000, "min_order_inr": 500,
    "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_weekly_loss_inr": 3000,
    "max_monthly_loss_inr": 6000, "max_same_side_risk_inr": 1000, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "max_losing_streak": 2, "max_entries_per_sector": 2, "min_reward_risk": 1.0, "max_charges_pct_of_risk": 15, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "fractional": False, "broker": "zerodha", "exchange": "NSE", "product": "MIS", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False, "intraday_leverage": 1,
    "charges": {"brokerage_pct": 0.03, "brokerage_max": 20, "stt_sell_pct": 0.025, "exchange_pct": 0.00307,
                "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
}
# v1's rules, for the weights mode tests
V1 = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 2000, "min_order_inr": 5,
      "fractional": True, "paper": True, "blocked": []}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rows(path):
    with open(path) as fh:
        return size.parse_weights(json.load(fh))


def dollars(result):
    return {p["ticker"]: p["dollars"] for p in result["positions"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-11-05T04:15:00Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"INFY.NS": price},
            "detail": {"INFY.NS": {"prev_close": 1128.2, "time": quote_time, "source": "yahoo"}}}


class Charges(unittest.TestCase):
    """Zerodha's equity intraday rates, worked by hand for 41 INFY at 1160.50, out at the 1149.80 stop."""

    def test_the_buy(self):
        c = size.order_charges("buy", 41 * 1160.50)               # 47,580.50
        self.assertEqual(c, {"brokerage": 14.27, "stt": 0.0, "exchange": 1.46, "sebi": 0.05, "stamp": 1.43,
                             "gst": 2.84, "total": 20.05})

    def test_the_sell_at_the_stop(self):
        c = size.order_charges("sell", 41 * 1149.80)              # 47,141.80
        self.assertEqual(c, {"brokerage": 14.14, "stt": 11.79, "exchange": 1.45, "sebi": 0.05, "stamp": 0.0,
                             "gst": 2.82, "total": 30.25})
        self.assertEqual(size.round_trip(41, 1160.50, 1149.80), 50.30)

    def test_brokerage_is_capped_at_20_rupees_an_order(self):
        self.assertEqual(size.order_charges("buy", 1000000)["brokerage"], 20.0)   # 0.03 % would be 300
        self.assertEqual(size.order_charges("buy", 0)["total"], 0.0)

    def test_the_config_can_change_a_rate(self):
        self.assertEqual(size.order_charges("buy", 47580.50, {"brokerage_max": 0})["brokerage"], 0.0)


class TheReel(unittest.TestCase):
    """₹1,00,000, 10% buffer, ₹500 max loss with charges, INFY risk 10.70 -> 41 shares, ₹47,580.50, max loss ₹489."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)

    def test_infy_is_41_shares_bound_by_the_max_loss_with_charges(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 41)                           # 42 would be 449.40 + 51.51 = 500.91
        self.assertEqual(s["bound_by"], "max loss")
        self.assertEqual(s["risk_per_share"], 10.7)
        self.assertEqual(s["cost"], 47580.5)
        self.assertEqual((s["loss_at_stop"], s["charges_at_stop"], s["max_loss"]), (438.7, 50.3, 489.0))
        self.assertEqual((s["charges_at_target"], s["gain_at_target"]), (51.13, 826.27))
        self.assertEqual(s["reward_risk"], 2.0)
        self.assertEqual(s["reward_risk_net"], 1.69)
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 41), ("position cap", 64), ("max order", 64), ("cash", 77)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertIsNone(s["refusal"])

    def test_the_bracket_order(self):
        o = size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "41", "type": "market",
            "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "1181.90",
            "stop_loss_stop_price": "1149.80",
            "plan": {"entry": 1160.5, "stop": 1149.8, "target": 1181.9, "risk_per_share": 10.7,
                     "max_loss": 489.0, "charges_at_stop": 50.3, "charges_at_target": 51.13,
                     "gain_at_target": 826.27, "setup": "orb", "entry_time": "9:40 AM", "flat_by": "15:10"}})
        self.assertIn("bound by your ₹500 max loss", why)
        self.assertIn("₹438.70 at the stop + ₹50.30 charges = ₹489", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(os.path.join(FIX, "trader-risk", "orders-infy.json")) as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_a_limit_order(self):
        o = size.bracket_order(size.size_trade(plan(), cfg(), limit=1160.50, today=TODAY))
        self.assertEqual((o["type"], o["limit_price"], o["qty"]), ("limit", "1160.50", "41"))
        s = size.size_trade(plan(), cfg(), limit=1160.00, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["qty"], s["max_loss"]), (10.2, 43, 491.33))
        self.assertEqual(size.bracket_order(s)["limit_price"], "1160.00")

    def test_cli_writes_one_bracket_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 41 shares of INFY.NS = ₹47,580.50 at 1160.50.", text)
            self.assertIn("At the stop:   lose 41 x 10.70 = ₹438.70, plus ₹50.30 charges = ₹489 (charges 11.5% of the risk).", text)
            self.assertIn("At the target: win ₹826.27 after ₹51.13 charges. Reward:risk after charges 1.69.", text)
            self.assertIn("on the NSE, market, MIS (intraday)", text)
            self.assertIn("<- binds", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["created_at"], "2026-11-05T04:10:00Z")
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss"], 489.0)

    def test_cli_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 41)
        self.assertEqual(out["orders"][0]["limit_price"], "1160.50")


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=600.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹500 max loss per trade", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=600.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_charges_can_turn_a_1r_target_into_a_refusal(self):
        s = size.size_trade(plan(target=1171.2, target_r=1.0), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["reward_risk"], s["reward_risk_net"]), (41, 1.0, 0.79))
        self.assertIn("reward:risk after charges is 0.79 (win ₹387.85 at the target, lose ₹489 at the stop)",
                      s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_charges_over_15_pct_of_the_risk_is_a_refusal(self):
        # a stop 5.50 under 1160.50: 64 shares (the max order binds), ₹352 at the stop on price. Charges:
        # the buy ₹74,272 pays ₹20 brokerage (capped) + 2.28 exchange + 0.07 SEBI + 2.23 stamp + 4.02 GST
        # = 28.60; the sell at the stop ₹73,920 pays 20 + 18.48 STT + 2.27 + 0.07 + 4.02 = 44.84. 73.44 in all,
        # 20.9% of 352
        s = size.size_trade(plan(stop=1155.0, target=1171.5), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["loss_at_stop"], s["charges_at_stop"], s["charges_pct_of_risk"]),
                         (64, 352.0, 73.44, 20.9))
        self.assertIn("charges at the stop are ₹73.44, 20.9% of the ₹352 you risk on price, over your 15% limit",
                      s["refusal"])
        self.assertIn("about 7.67 a share or more", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        self.assertIsNone(size.size_trade(plan(stop=1155.0, target=1171.5), cfg(max_charges_pct_of_risk=0),
                                          today=TODAY)["refusal"])
        # the reel's trade: 50.30 of charges on 438.70 at risk, 11.5%, inside the limit
        self.assertEqual(size.size_trade(plan(), cfg(), today=TODAY)["charges_pct_of_risk"], 11.5)

    def test_a_short_mirrors_the_reel(self):
        # the reel turned upside down: sell 1139.50, stop 1150.20 overhead, target 1118.10. The sale at the
        # entry pays STT (46,719.50: 14.02 + 11.68 STT + 1.43 + 0.05 + 2.79 GST = 29.97), the buy back at the
        # stop pays stamp duty (47,158.20: 14.15 + 1.45 + 0.05 + 1.41 stamp + 2.82 GST = 19.88): 49.85
        short = plan(side="sell", entry=1139.5, stop=1150.2, target=1118.1)
        s = size.size_trade(short, cfg(allow_short=True), today=TODAY)
        self.assertEqual((s["side"], s["qty"], s["charges_at_stop"], s["max_loss"]), ("sell", 41, 49.85, 488.55))
        self.assertEqual(size.round_trip(41, 1150.2, 1139.5), 49.85)
        self.assertEqual((s["gain_at_target"], s["reward_risk_net"]), (828.1, 1.7))
        o = size.bracket_order(s)
        self.assertEqual((o["side"], o["product"], o["stop_loss_stop_price"], o["take_profit_limit_price"]),
                         ("sell", "MIS", "1150.20", "1118.10"))
        text = size.plan_report(s, o)
        self.assertIn("SHORT (sell first, buy back by the flat-by), opening-range breakdown", text)
        self.assertIn("Risk 10.70 a share (stop 1150.20 minus entry 1139.50)", text)
        self.assertIn("sell short 41 INFY.NS on the NSE, market, MIS", text)
        self.assertIn("can fill above 1150.20", text)

    def test_short_rules(self):
        short = plan(side="sell", entry=1139.5, stop=1150.2, target=1118.1)
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(short, cfg(allow_short=True, product="CNC"), today=TODAY)
        self.assertIn("must be intraday (MIS)", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(dict(short, stop=1130.0), cfg(allow_short=True), today=TODAY)
        self.assertIn("a short needs its stop overhead", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(dict(short, target=1145.0), cfg(allow_short=True), today=TODAY)
        self.assertIn("not below the entry", str(e.exception))
        # a fresh LOWER price is a bigger risk for a short: sized at it
        s = size.size_trade(short, cfg(allow_short=True), price=1135.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1135.0, "price", 15.2))
        s = size.size_trade(short, cfg(allow_short=True), price=1142.0, today=TODAY)
        self.assertEqual(s["entry_from"], "plan")

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=30000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_position_pct=20), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (17, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=15000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (4, "cash", 5000))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=1000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (64, "position cap"))

    def test_leverage_needs_allow_margin_and_stops_at_5x(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(intraday_leverage=5), today=TODAY)
        self.assertIn("allow_margin", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(intraday_leverage=6, allow_margin=True), today=TODAY)
        self.assertIn("over 5", str(e.exception))
        s = size.size_trade(plan(), cfg(intraday_leverage=5, allow_margin=True), cash=15000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (21, "cash"))   # 5,000 x 5 = 25,000 / 1160.50

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(), price=1164.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1164.0, "price", 14.2))
        self.assertEqual((s["qty"], s["max_loss"], s["reward_risk_net"]), (32, 493.7, 1.08))
        self.assertIsNone(s["refusal"])
        s = size.size_trade(plan(), cfg(min_reward_risk=1.2), price=1164.0, today=TODAY)
        self.assertIn("reward:risk after charges is 1.08", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        lower = size.size_trade(plan(), cfg(), price=1158.00, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1160.5, 41))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1164.0), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            out = json.loads(run(base + ["--now", "2026-11-05 09:50"])[1])
            self.assertEqual(out["sizing"]["qty"], 32)
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-11-05 10:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 41)
            self.assertIn("not used", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 15000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (4, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1165.0), "not below the entry"),
                        (plan(target=1150.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "short selling is off"),
                        (plan(side="sideways"), "buy or sell"),
                        (plan(symbol=""), "no symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=1185.00, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["infy.ns"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-11-06")
        self.assertIn("make a new plan", str(e.exception))

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="ABC.NS", entry=40.0, stop=39.0, target=42.0)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=1.5, min_reward_risk=0), today=TODAY)
        self.assertEqual(s["qty"], 1)
        self.assertIn("under your ₹500 minimum order", s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_old_usd_keys_still_read(self):
        s = size.size_trade(plan(), {"budget": 100000, "max_loss_per_trade_usd": 1000, "max_order_usd": 30000},
                            today=TODAY)
        self.assertEqual((s["max_loss_per_trade_inr"], s["bound_by"]), (1000, "max order"))

    def test_missing_plan_and_weights_only_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


class WeightsMode(unittest.TestCase):
    """v1's split, kept behind --weights / --targets: 5,000, 10% cash, 25% cap. The weights are
    arithmetic test data from v1 (a US 13F); targets.example.json is the NSE example."""

    def check_reel(self, r):
        self.assertEqual(dollars(r), REEL_V1)
        self.assertEqual(r["cash_buffer"], 500)
        self.assertEqual(r["invested"], 4500)
        self.assertEqual(r["idle"], 0)
        self.assertEqual(r["cap"], 1250)
        self.assertEqual([p["ticker"] for p in r["positions"] if p["capped"]], ["AAPL"])

    def test_from_the_raw_weights(self):
        self.check_reel(size.size(rows(TOP5), v1()))

    def test_the_nse_example_weights_file(self):
        r = size.size(rows(TARGETS), {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75,
                                      "min_order_inr": 500, "fractional": True})
        self.assertEqual(dollars(r), {"RELIANCE.NS": 36000, "TCS.NS": 27000, "INFY.NS": 27000})
        self.assertEqual((r["invested"], r["idle"]), (90000, 0))

    def test_from_inline_weights(self):
        w = size.parse_weights(size.weights_from_arg("AAPL=22.04,AXP=17.14,GOOGL=12.62,KO=10.86,BAC=9.20"))
        self.check_reel(size.size(w, v1()))

    def test_renormalised_weights(self):
        r = size.size(rows(TOP5), v1())
        shown = {p["ticker"]: round(p["weight"], 1) for p in r["positions"]}
        self.assertEqual(shown, {"AAPL": 30.7, "AXP": 23.9, "GOOGL": 17.6, "KO": 15.1, "BAC": 12.8})

    def test_one_decimal_weights_stay_within_a_dollar(self):
        w = size.parse_weights({"AAPL": 30.7, "AXP": 23.9, "GOOGL": 17.6, "KO": 15.1, "BAC": 12.8})
        r = size.size(w, v1())
        self.assertEqual(r["invested"], 4500)
        self.assertEqual(dollars(r)["AAPL"], 1250)
        for t, v in dollars(r).items():
            self.assertLessEqual(abs(v - REEL_V1[t]), 1.0, t)

    def test_orders_are_place_stock_order_shaped(self):
        r = size.size(rows(TOP5), v1())
        orders = size.orders_from(r, "a 13F")
        self.assertEqual(orders[0], {"symbol": "AAPL", "exchange": "NSE", "product": "CNC", "side": "buy",
                                     "notional": "1250", "type": "market", "time_in_force": "day",
                                     "why": orders[0]["why"]})
        self.assertIn("capped at 25%", orders[0]["why"])
        for o in orders:
            self.assertIsInstance(o["notional"], str)
            self.assertNotIn("qty", o)
        self.assertEqual([o["notional"] for o in orders], ["1250", "1119", "823", "708", "600"])

    def test_cents_mode_keeps_the_total(self):
        r = size.size(rows(TOP5), v1(), cents=True)
        d = dollars(r)
        self.assertEqual(d["AAPL"], 1250)
        self.assertAlmostEqual(sum(d.values()), 4500, places=2)
        self.assertAlmostEqual(d["AXP"], 1118.13, places=2)
        self.assertEqual(size.orders_from(r)[1]["notional"], "1118.13")

    def test_cap_redistributes_until_nobody_is_over(self):
        w = size.parse_weights({"A": 60, "B": 30, "C": 10})
        r = size.size(w, {"budget": 1000, "cash_buffer_pct": 0, "max_position_pct": 40, "min_order_inr": 1})
        self.assertEqual(dollars(r), {"A": 400, "B": 400, "C": 200})
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["A", "B"])

    def test_everything_capped_leaves_money_idle(self):
        w = size.parse_weights({"AAPL": 50, "KO": 50})
        r = size.size(w, v1())
        self.assertEqual(dollars(r), {"AAPL": 1250, "KO": 1250})
        self.assertEqual(r["idle"], 2000)
        self.assertIn("every stock is at its cap", size.report(r, size.orders_from(r)))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        w = size.parse_weights({"A": 90, "B": 7, "C": 3})
        r = size.size(w, {"budget": 100, "cash_buffer_pct": 10, "max_position_pct": 100, "min_order_inr": 5})
        self.assertNotIn("C", dollars(r))
        self.assertEqual(sum(dollars(r).values()), 90)
        self.assertTrue(all(v >= 5 for v in dollars(r).values()))
        self.assertEqual(r["dropped"][0]["ticker"], "C")

    def test_blocked_tickers_are_dropped_first(self):
        r = size.size(rows(TOP5), v1(blocked=["ko"]))
        self.assertNotIn("KO", dollars(r))
        self.assertEqual(r["invested"], 4500)
        self.assertIn("blocked", r["dropped"][0]["reason"])

    def test_top_n(self):
        r = size.size(rows(TOP5), v1(max_position_pct=100), top=2)
        self.assertEqual(sorted(dollars(r)), ["AAPL", "AXP"])

    def test_whole_shares_need_prices(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TOP5), v1(fractional=False))
        with self.assertRaises(size.SizeError):
            size.size(rows(TOP5), cfg())          # the v2 example config is whole shares

    def test_whole_shares_with_prices(self):
        r = size.size(rows(TOP5), v1(fractional=False), prices=PRICES)
        self.assertTrue(r["whole_shares"])
        for p in r["positions"]:
            self.assertEqual(p["shares"], int(p["shares"]))
            self.assertAlmostEqual(p["dollars"], p["shares"] * PRICES[p["ticker"]], places=2)
            self.assertLessEqual(p["dollars"], 1250)
        self.assertAlmostEqual(r["invested"] + r["idle"], 4500, places=2)
        self.assertLess(r["idle"], min(PRICES.values()) * 2)
        orders = size.orders_from(r)
        self.assertTrue(all("qty" in o and "notional" not in o for o in orders))

    def test_no_budget_and_duplicates_are_errors(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TOP5), {"cash_buffer_pct": 10})
        with self.assertRaises(size.SizeError):
            size.size([{"ticker": "KO", "name": "", "weight": 1}, {"ticker": "KO", "name": "", "weight": 2}], v1())

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(v1(), fh)
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--targets", TOP5, "--config", c, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("require_stop", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["AAPL", "AXP", "GOOGL", "KO", "BAC"])
            code, text, _ = run(["--targets", TOP5, "--config", c, "--json"])
            self.assertEqual(json.loads(text)["result"]["idle"], 0)
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)


if __name__ == "__main__":
    unittest.main()
