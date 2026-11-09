import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
INFY_PATH = os.path.join(FIX, "trader-risk", "orders-infy.json")
DELIVERY_PATH = os.path.join(FIX, "trader-risk", "orders-delivery.json")
NOW = "2026-11-05 09:40"
TODAY = "2026-11-05"
LT_TODAY = "2026-11-06"
PRICES = {"RELIANCE.NS": 1320.0, "TCS.NS": 2498.5, "HDFCBANK.NS": 788.25, "SBIN.NS": 1018.5, "ITC.NS": 283.15}
QUOTE_TIME = datetime.datetime(2026, 11, 5, 4, 15)   # Yahoo's INFY quote 1164.00, 9:45 IST
# long-term rules with every day-trade check switched off: the account checks still hold
LT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 20000, "min_order_inr": 500,
      "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None, "no_entries_after": None,
      "max_trades_per_day": 0, "max_daily_loss_inr": 0, "max_loss_per_trade_inr": 0, "min_reward_risk": 0}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def lt(**over):
    c = dict(LT)
    c.update(over)
    return c


def infy(**over):
    with open(INFY_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": "buy", "qty": str(qty), "type": "market",
            "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop, "plan": {"entry": entry, "stop": stop, "target": target}}


def delivery():
    with open(DELIVERY_PATH) as fh:
        return json.load(fh)["orders"]


def buy(sym, qty, product="CNC"):
    return {"symbol": sym, "exchange": "NSE", "product": product, "side": "buy", "qty": str(qty), "type": "market",
            "time_in_force": "day"}


def placed(sym, side="buy", day=TODAY, max_loss=489.0, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:45:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheReel(unittest.TestCase):
    def test_the_infy_bracket_passes_every_check(self):
        res = check.check([infy()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1160.5, 1149.8, 1181.9))
        self.assertEqual((r["risk_per_share"], r["charges"], r["max_loss"], r["reward_risk"]),
                         (10.7, 50.3, 489.0, 1.69))
        self.assertEqual(r["value_inr"], 47580.5)
        self.assertEqual(res["cash_after"], 52419.5)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_42_shares_is_blocked_by_the_max_loss_with_charges(self):
        res = check.check([infy(qty="42")], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 42 shares x 10.70 risk a share = ₹449.40 at the stop, plus ₹51.51 charges = "
                          "₹500.91, over your ₹500 max loss per trade"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = infy()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty="42")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss_stop_price="1149.00")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, product="CNC")), check.fingerprint(o))
        self.assertEqual(check.ORDER_FIELDS, ticket.ORDER_FIELDS)

    def test_charges_are_the_same_in_every_skill(self):
        size = load("trader-budget", "size")
        tm = load("trader-timemachine", "timemachine")
        for side, value in (("buy", 47580.5), ("sell", 47141.8), ("sell", 1000000), ("buy", 99.99)):
            want = check.order_charges(side, value)
            for mod in (size, ticket, tm):
                self.assertEqual(mod.order_charges(side, value), want, (mod.__name__, side, value))
        self.assertEqual(check.CHARGES, size.CHARGES)
        self.assertEqual(check.CHARGES, ticket.CHARGES)
        self.assertEqual(check.CHARGES, tm.CHARGES)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", INFY_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--bands", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy INFY.NS 41 shares, entry", text)
            self.assertIn("entry 1160.50 (the plan's entry), stop 1149.80, target 1181.90: risk 10.70 a share", text)
            self.assertIn("max loss ₹489 with ₹50.30 charges (limit ₹500), reward:risk after charges 1.69 (min 1)",
                          text)
            self.assertIn("1 passed, 0 blocked at 2026-11-05 09:40 IST", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(infy()))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = infy()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 1160.5}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = infy()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price"):
            o.pop(k)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 489.0)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 1160.50", reasons(self.one(infy(stop_loss_stop_price="1161.00"))))
        res = self.one(infy(take_profit_limit_price="1160.00"))
        self.assertIn("target: the target 1160.00 must be above the entry 1160.50", reasons(res))

    def test_reward_risk_is_after_charges(self):
        res = self.one(infy(take_profit_limit_price="1171.20"))          # 1R on price
        self.assertIn("reward:risk: 0.79 after charges (win ₹387.85 at the target 1171.20, lose ₹489 at the stop "
                      "1149.80) is under your minimum 1", reasons(res))
        self.assertEqual(verdicts(self.one(infy(take_profit_limit_price="1171.20"), min_reward_risk=0.75)),
                         ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = check.check([infy()], cfg(), prices={"INFY.NS": 1164.0}, now=at("2026-11-05 09:50"),
                          price_times={"INFY.NS": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1164.0, "price", 14.2))
        self.assertIn("loss: 41 shares x 14.20 risk a share = ₹582.20 at the stop, plus ₹50.37 charges = ₹632.57",
                      reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-11-05 10:30", "2026-11-05 09:40"):
            res = check.check([infy()], cfg(), prices={"INFY.NS": 1164.0}, now=at(now),
                              price_times={"INFY.NS": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 1160.5)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([infy()], cfg(), prices={"INFY.NS": 1158.0}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1160.5)

    def test_a_limit_order_uses_its_limit_price(self):
        o = infy(type="limit", limit_price="1160.00")
        res = check.check([o], cfg(), prices={"INFY.NS": 1164.0}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["verdict"]), (1160.0, 468.5, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("TCS.NS"), placed("SBIN.NS")]}
        res = check.check([infy()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, ₹978 of ₹1,500 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("TCS.NS"), placed("SBIN.NS"), placed("ITC.NS")]}
        why = reasons(check.check([infy()], cfg(), state=three, now=at(NOW)))
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: ₹1,467 already at risk today plus ₹489 for this one = ₹1,956, over your ₹1,500",
                      why)
        yesterday = {"orders": [placed(s, day="2026-11-04") for s in ("TCS.NS", "SBIN.NS", "ITC.NS")]}
        self.assertEqual(verdicts(check.check([infy()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("TCS.NS"), placed("TCS.NS", side="sell", kind="exit", max_loss=None),
                         placed("SBIN.NS", status="declined"), placed("ITC.NS", status="failed")]}
        res = check.check([infy()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [bracket(s, 2, 400.0, 200.0, 900.0) for s in ("AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: ", why)
        self.assertIn("over your ₹1,500 daily loss limit", why)

    def test_entry_window_ist(self):
        cases = [("2026-11-05 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-11-05 14:46", "no new entries after 14:45 IST (it is 14:46)"),
                 ("2026-11-05 09:00", "the market is closed (Thu 2026-11-05 09:00"),
                 ("2026-11-05 15:31", "the market is closed"),
                 ("2026-11-07 10:15", "the market is closed today (weekend"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday: Diwali Balipratipada"),
                 ("2026-11-05T03:55:00Z", "no new entries before 09:30 IST (it is 09:25)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(self.one(infy(), now=now)), now)
        for now in ("2026-11-05 09:30", "2026-11-05 14:45", "2026-11-05T04:10:00Z"):
            self.assertEqual(verdicts(self.one(infy(), now=now)), ["PASS"], now)
        res = self.one(infy(), now="2026-11-07 10:15", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("check nseindia.com", " ".join(self.one(infy(), now="2027-01-04 10:00")["warnings"]))
        with self.assertRaises(check.RiskError):
            self.one(infy(), no_entries_before="9.30am")

    def test_the_window_uses_india_time_not_the_machine(self):
        self.assertEqual(check.to_ist(at("2026-11-05 09:40")), datetime.datetime(2026, 11, 5, 9, 40))
        self.assertEqual(at("2026-11-05 09:40"), datetime.datetime(2026, 11, 5, 4, 10))
        self.assertEqual(at("2026-07-01 09:40"), datetime.datetime(2026, 7, 1, 4, 10))   # no daylight saving

    def test_the_price_band(self):
        near = check.check([infy()], cfg(), now=at(NOW), bands={"INFY.NS": 20}, prev_close={"INFY.NS": 970.0})
        self.assertIn("band: INFY.NS at 1160.50 is within 1% of its upper price band 1164.00 (20% over the previous "
                      "close 970.00)", reasons(near))
        far = check.check([infy()], cfg(), now=at(NOW), bands={"INFY.NS": 20}, prev_close={"INFY.NS": 1128.2})
        self.assertEqual(verdicts(far), ["PASS"])
        low = check.check([infy()], cfg(), now=at(NOW), bands={"INFY.NS": 2}, prev_close={"INFY.NS": 1180.0})
        self.assertEqual(verdicts(low), ["PASS"])
        self.assertIn("the stop 1149.80 is under the lower price band 1156.40", warnings(low))
        none = check.check([infy()], cfg(), now=at(NOW), bands={"INFY.NS": None}, prev_close={"INFY.NS": 970.0})
        self.assertEqual(verdicts(none), ["PASS"])                     # F&O stocks have no fixed band
        missing = check.check([infy()], cfg(), now=at(NOW), bands={"TCS.NS": 5}, prev_close={"INFY.NS": 970.0})
        self.assertIn("INFY.NS is not in trader/bands.json", warnings(missing))

    def test_leverage_only_with_allow_margin(self):
        pf = {"cash": 20000, "positions": []}
        res = check.check([infy()], cfg(intraday_leverage=5), portfolio=pf, now=at(NOW))
        self.assertIn("cash: needs ₹47,580.50 but only ₹20,000 cash", reasons(res))
        self.assertIn("ignored while \"allow_margin\" is false", " ".join(res["warnings"]))
        res = check.check([infy()], cfg(intraday_leverage=5, allow_margin=True, max_position_pct=500), portfolio=pf,
                          now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("leverage: ₹38,064.40 of this ₹47,580.50 position is borrowed (5x intraday)", warnings(res))
        res = check.check([infy()], cfg(intraday_leverage=9, allow_margin=True, max_position_pct=500), portfolio=pf,
                          now=at(NOW))
        self.assertEqual(res["leverage"], 5.0)                         # SEBI's ceiling

    def test_a_short_entry_when_allowed(self):
        short = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "41",
                 "type": "market", "time_in_force": "day", "order_class": "bracket",
                 "take_profit_limit_price": "1149.80", "stop_loss_stop_price": "1171.20", "plan": {"entry": 1160.5}}
        self.assertIn("short: selling more INFY.NS than you hold", reasons(self.one(short)))
        r = self.one(short, allow_short=True, min_reward_risk=0)["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "entry"))
        self.assertAlmostEqual(r["max_loss"], 438.7 + r["charges"], places=2)
        res = self.one(dict(short, stop_loss_stop_price="1160.00"), allow_short=True)
        self.assertIn("must be above the entry 1160.50 for a sell", reasons(res))

    def test_nse_order_format(self):
        self.assertIn("the NSE takes whole shares, not a rupee amount", reasons(self.one(dict(infy(qty=None),
                                                                                         notional="47000"))))
        self.assertIn("whole shares only", reasons(self.one(infy(qty="41.5"))))
        self.assertIn("a bracket order needs time_in_force day", reasons(self.one(infy(time_in_force="ioc"))))
        self.assertIn("unknown time_in_force", reasons(self.one(infy(time_in_force="gtc"))))
        self.assertIn("this pack trades on the NSE, not BSE", reasons(self.one(infy(exchange="BSE"))))
        self.assertIn("product must be MIS (intraday) or CNC", reasons(self.one(infy(product="NRML"))))
        self.assertIn("delivery STT", warnings(self.one(infy(product="CNC"))))
        self.assertIn("no extended-hours session", reasons(self.one(infy(extended_hours=True))))
        self.assertIn("unknown order type", reasons(self.one(infy(type="trailing_stop"))))
        o = infy()
        o.pop("take_profit_limit_price")
        self.assertIn("needs both take_profit_limit_price and stop_loss_stop_price", reasons(self.one(o)))
        self.assertIn("needs a stop_loss_stop_price", reasons(self.one(dict(infy(), stop_loss_stop_price=None,
                                                                            stop_loss_limit_price="1149.00"))))
        self.assertIn("as a string", warnings(self.one(infy(stop_loss_stop_price=1149.8))))

    def test_futures_options_and_other_symbols(self):
        for sym in ("INFY26NOVFUT", "INFY26NOV1200CE", "NIFTY26N1225000PE"):
            o = buy(sym, 1, "MIS")
            r = check.check([o], lt(), prices={sym: 10.0}, today=LT_TODAY)["results"][0]
            self.assertIn("F&O is off", " ".join(r["reasons"]), sym)
            r = check.check([o], lt(allow_options=True), prices={sym: 10.0}, today=LT_TODAY)["results"][0]
            self.assertNotIn("asset:", " ".join(r["reasons"]), sym)
            self.assertIn("F&O order", " ".join(r["warnings"]), sym)
        for sym in ("AAPL", "BTC/INR", "INFY.BO"):
            r = check.check([buy(sym, 1)], lt(), prices={sym: 10.0}, today=LT_TODAY)["results"][0]
            self.assertIn("does not look like an NSE stock or ETF symbol", " ".join(r["reasons"]), sym)
        for sym in ("M&M.NS", "BAJAJ-AUTO.NS", "NIFTYBEES.NS"):
            r = check.check([buy(sym, 1)], lt(), prices={sym: 600.0}, today=LT_TODAY)["results"][0]
            self.assertEqual(r["verdict"], "PASS", (sym, r["reasons"]))


class Exits(unittest.TestCase):
    PF = {"cash": 52419.5, "positions": [{"ticker": "INFY.NS", "shares": 41,
                                          "lots": [{"date": TODAY, "shares": 41, "cost": 47580.5}]}]}
    SELL = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "41", "type": "market",
            "time_in_force": "day"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("INFY.NS"), placed("TCS.NS"), placed("SBIN.NS")] +
                          [placed("T%d.NS" % i, max_loss=1) for i in range(6)] +
                          [placed("INFY.NS", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["INFY.NS"], max_order_inr=100, max_daily_loss_inr=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-11-05 15:10"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("price", why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-11-07 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty="20")
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"INFY.NS": 1164.9}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"INFY.NS": 1164.9}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, type="limit")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, qty="42")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more INFY.NS than you hold (41 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([infy(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class AccountChecks(unittest.TestCase):
    """The account checks on delivery (CNC) buys, with the day-trade checks off."""

    def one(self, order, **over):
        return check.check([order], lt(**over), prices=PRICES, today=LT_TODAY)["results"][0]

    def test_the_five_buys_pass_on_a_fresh_1_lakh_paper_account(self):
        res = check.check(delivery(), lt(), prices=PRICES, today=LT_TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5, [r["reasons"] for r in res["results"]])
        self.assertEqual(res["cash_after"], 13422.35)                  # 1,00,000 - 86,577.65
        self.assertEqual(res["results"][0]["value_inr"], 17160.0)
        self.assertEqual(res["results"][0]["amount"], "13 shares")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(delivery() + [buy("RELIANCE.NS", 7)], lt(), prices=PRICES, today=LT_TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_day_trade_config_blocks_buys_without_a_stop(self):
        res = check.check(delivery(), cfg(), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your ₹20,000 max order",
                      " ".join(self.one(buy("TCS.NS", 9), max_position_pct=100)["reasons"]))
        self.assertIn("under your ₹500 minimum", " ".join(self.one(buy("ITC.NS", 1))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("ITC.NS", 10), blocked=["ITC.NS"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_format_rules(self):
        nolimit = dict(buy("ITC.NS", 10), type="limit")
        self.assertIn("needs a limit_price", " ".join(self.one(nolimit)["reasons"]))
        bad = dict(buy("ITC.NS", 10), side="hold")
        self.assertIn("side must be buy or sell", " ".join(self.one(bad)["reasons"]))
        self.assertIn("no qty", " ".join(self.one(dict(buy("ITC.NS", 10), qty=None))["reasons"]))

    def test_numbers_should_be_strings(self):
        r = self.one(dict(buy("ITC.NS", 10), qty=10))
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("as a string", r["warnings"][0])

    def test_share_orders_need_a_price(self):
        self.assertIn("no price for ZZZZ.NS", " ".join(self.one(buy("ZZZZ.NS", 2))["reasons"]))
        r = self.one(buy("ITC.NS", 10))
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_inr"], 2831.5)

    def test_margin(self):
        r = check.check([buy("TCS.NS", 6)], lt(cash_buffer_pct=0, max_position_pct=100),
                        portfolio={"cash": 10000, "positions": []}, prices=PRICES, today=LT_TODAY)["results"][0]
        self.assertIn("margin", " ".join(r["reasons"]))

    PF = {"cash": 5000.0, "positions": [{"ticker": "ITC.NS", "shares": 61,
                                         "lots": [{"date": "2026-11-06", "shares": 61, "cost": 17272.15}]}]}

    def test_short_sales_are_blocked(self):
        sell = dict(buy("ITC.NS", 70), side="sell")
        r = check.check([sell], lt(), portfolio=self.PF, prices=PRICES, today=LT_TODAY)["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = "30"
        r = check.check([sell], lt(), portfolio=self.PF, prices=PRICES, today=LT_TODAY)["results"][0]
        self.assertEqual(r["verdict"], "PASS")

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [dict(buy("ITC.NS", 40), side="sell"), buy("HDFCBANK.NS", 15)]
        res = check.check(orders, lt(max_position_pct=100), portfolio=self.PF, prices=PRICES, today=LT_TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("HDFCBANK.NS", 15)], lt(max_position_pct=100), portfolio=self.PF, prices=PRICES,
                          today=LT_TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d.NS" % i, "side": "buy", "status": "placed", "at": LT_TODAY + "T10:00:00"}
                         for i in range(10)]}
        res = check.check([buy("ITC.NS", 10)], lt(), state=st, prices=PRICES, today=LT_TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("ITC.NS", 10)], lt(), state=st, prices=PRICES, today="2026-11-09")
        self.assertEqual(verdicts(res), ["PASS"])
        itc = {"orders": [{"symbol": "ITC.NS", "side": "buy", "status": "placed", "at": LT_TODAY + "T10:00:00"}]}
        res = check.check([buy("ITC.NS", 10)], lt(), state=itc, prices=PRICES, today=LT_TODAY)
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "ITC.NS", "side": "buy", "status": "declined",
                                "at": LT_TODAY + "T10:00:00"}]}
        self.assertEqual(verdicts(check.check([buy("ITC.NS", 10)], lt(), state=declined, prices=PRICES,
                                              today=LT_TODAY)), ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("ITC.NS", 10)], lt(paper=False), prices=PRICES, today=LT_TODAY)
        self.assertIn("LIVE", res["warnings"][0])


class Cli(unittest.TestCase):
    def test_prices_bands_and_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-11-05T04:16:00Z", "prices": {"INFY.NS": 1164.0},
                           "detail": {"INFY.NS": {"prev_close": 1128.2, "time": "2026-11-05T04:15:00Z",
                                                  "fetched": "2026-11-05T04:16:00Z"}}}, fh)
            bp = os.path.join(d, "bands.json")
            with open(bp, "w") as fh:
                json.dump({"bands": {"INFY.NS": None, "TCS.NS": None}}, fh)
            none = os.path.join(d, "none")
            base = ["--orders", INFY_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--bands", bp, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-11-05 09:50"]), 1)
            self.assertIn("41 shares x 14.20 risk a share = ₹582.20", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-11-05 10:30"]), 0)
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-11-05T04:16:00Z", "prices": {"INFY.NS": 1160.5},
                           "detail": {"INFY.NS": {"prev_close": 970.0, "time": "2026-11-05T04:10:00Z"}}}, fh)
            with open(bp, "w") as fh:
                json.dump({"bands": {"INFY.NS": 20}}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-11-05 09:40"]), 1)
            self.assertIn("band: INFY.NS", buf.getvalue())

    def test_delivery_orders_with_a_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(lt(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"prices": PRICES}, fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", DELIVERY_PATH, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--today", LT_TODAY, "--out", out])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], LT_TODAY)
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))

    def test_old_usd_config_keys_are_read(self):
        res = check.check([infy()], {"budget": 100000, "max_loss_per_trade_usd": 400}, now=at(NOW))
        self.assertIn("over your ₹400 max loss per trade", reasons(res))

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", INFY_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", INFY_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())


if __name__ == "__main__":
    unittest.main()
