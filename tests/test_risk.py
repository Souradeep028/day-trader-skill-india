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
      "max_trades_per_day": 0, "max_daily_loss_inr": 0, "max_loss_per_trade_inr": 0, "min_reward_risk": 0,
      "max_losing_streak": 0, "max_entries_per_sector": 0}
SECTORS = {"INFY.NS": "Information Technology", "TCS.NS": "Information Technology", "HCLTECH.NS": "Information Technology",
           "SBIN.NS": "Financial Services"}


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

    def test_charges_too_big_for_the_risk(self):
        o = infy(stop_loss_stop_price="1155.00", take_profit_limit_price="1171.50", qty="64")
        o["plan"] = dict(o.get("plan") or {}, stop=1155.0, target=1171.5)
        res = self.one(o)
        self.assertEqual(res["results"][0]["reasons"],
                         ["charges: ₹73.44 at the stop is 20.9% of the ₹352 you risk on price, over your "
                          "15% limit (max_charges_pct_of_risk): the stop 1155.00 is too close for the "
                          "charges; widen it to about 7.67 a share or more"])
        self.assertEqual(verdicts(self.one(o, max_charges_pct_of_risk=25)), ["PASS"])
        self.assertEqual(verdicts(self.one(infy())), ["PASS"])            # the reel's trade: 11.5%

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
        res = check.check(orders, cfg(max_same_side_risk_inr=0), now=at(NOW))
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

    def fills(self, sym, buy, sell, qty=10, day=TODAY):
        return [{"symbol": sym, "side": "buy", "status": "filled", "date": day, "qty": str(qty), "price": buy},
                {"symbol": sym, "side": "sell", "status": "filled", "date": day, "qty": str(qty), "price": sell}]

    def test_cool_off_after_two_losing_trades(self):
        two = {"orders": self.fills("TCS.NS", 2500, 2490) + self.fills("SBIN.NS", 1018, 1010)}
        self.assertEqual(check.losing_streak(two, TODAY)[0], 2)
        res = check.check([infy()], cfg(), state=two, now=at(NOW))
        self.assertIn("cool-off: 2 losing trades in a row today (-₹126.50, -₹90.76), your limit is 2", reasons(res))
        # TCS: 10 x -10.00 = -100.00 less 26.50 of charges; SBIN: 10 x -8.00 = -80.00 less 10.76
        won_last = {"orders": two["orders"] + self.fills("ITC.NS", 280, 281.5, qty=500)}
        self.assertEqual(check.losing_streak(won_last, TODAY)[0], 0)
        self.assertEqual(verdicts(check.check([infy()], cfg(), state=won_last, now=at(NOW))), ["PASS"])
        # a trade that broke even on price lost its charges: it counts as a loss
        flat = {"orders": self.fills("TCS.NS", 2500, 2500)}
        self.assertEqual(check.losing_streak(flat, TODAY)[0], 1)
        yesterday = {"orders": self.fills("TCS.NS", 2500, 2490, day="2026-11-04") * 1 +
                     self.fills("SBIN.NS", 1018, 1010, day="2026-11-04")}
        self.assertEqual(verdicts(check.check([infy()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])
        self.assertEqual(verdicts(check.check([infy()], cfg(max_losing_streak=3), state=two, now=at(NOW))), ["PASS"])

    def test_sector_cap(self):
        two_it = {"orders": [placed("TCS.NS"), placed("HCLTECH.NS")]}
        res = check.check([infy()], cfg(), state=two_it, now=at(NOW), sectors=SECTORS)
        self.assertIn("sector: 2 Information Technology entries today already, your limit is 2", reasons(res))
        res = check.check([infy()], cfg(), state={"orders": [placed("TCS.NS")]}, now=at(NOW), sectors=SECTORS)
        self.assertEqual(verdicts(res), ["PASS"])
        batch = [bracket(s, 2, 400.0, 390.0, 430.0) for s in ("TCS.NS", "HCLTECH.NS", "INFY.NS")]
        res = check.check(batch, cfg(), now=at(NOW), sectors=SECTORS)
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("sector:", reasons(res, 2))
        res = check.check([infy()], cfg(), now=at(NOW), sectors={"SBIN.NS": "Financial Services"})
        self.assertIn("INFY.NS is not in trader/sectors.json", warnings(res))
        res = check.check([infy()], cfg(), now=at(NOW))
        self.assertIn("the sector cap (max_entries_per_sector) is off", " ".join(res["warnings"]))

    def test_the_shipped_sector_map(self):
        with open(os.path.join(ROOT, "trader", "sectors.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["sectors"]), 501)                          # NSE's Nifty 500 list
        self.assertEqual(doc["sectors"]["INFY.NS"], "Information Technology")
        self.assertEqual(doc["sectors"]["M&M.NS"], "Automobile and Auto Components")
        with open(os.path.join(ROOT, "trader", "nifty50.txt")) as fh:
            self.assertTrue(all(l.strip() in doc["sectors"] for l in fh if l.strip()))

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


class SkipDays(unittest.TestCase):
    def one(self, order=None, now=NOW, events=None, surveillance={"symbols": {}, "updated": TODAY}, **over):
        return check.check([order or infy()], cfg(**over), now=at(now), events=events, surveillance=surveillance)

    def test_expiry_days(self):
        d = datetime.date
        self.assertTrue(check.expiry_day(d(2026, 11, 3)))                  # a Tuesday
        self.assertFalse(check.expiry_day(d(2026, 11, 2)))
        self.assertTrue(check.expiry_day(d(2026, 10, 19)))                 # Monday: Tuesday is Dussehra
        self.assertFalse(check.expiry_day(d(2026, 10, 20)))                # the holiday itself
        self.assertTrue(check.expiry_day(d(2026, 11, 9)))                  # Monday: Tuesday the 10th is a holiday
        self.assertFalse(check.expiry_day(d(2026, 11, 5)))
        self.assertEqual(check.previous_trading_day(d(2026, 10, 21)), d(2026, 10, 19))
        self.assertEqual(check.previous_trading_day(d(2026, 11, 9)), d(2026, 11, 6))

    def test_expiry_warns_by_default_and_blocks_when_asked(self):
        res = self.one(infy(), now="2026-11-03 09:40")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertTrue(any("weekly F&O expiry" in w for w in res["warnings"]))
        res = self.one(infy(), now="2026-11-03 09:40", skip_expiry_days=True)
        self.assertEqual(res["results"][0]["reasons"],
                         ["expiry: today is Nifty's weekly F&O expiry and \"skip_expiry_days\" is on"])
        self.assertFalse(any("expiry" in w for w in self.one(infy())["warnings"]))

    def test_results_day_and_the_day_after(self):
        ev = check.load_events({"events": [{"symbol": "INFY.NS", "date": "2026-11-05", "what": "Q2 FY27 results"}]})
        self.assertEqual(ev, {"INFY.NS": [("2026-11-05", "Q2 FY27 results", "results")]})
        self.assertEqual(self.one(events=ev)["results"][0]["reasons"],
                         ["results: INFY.NS has Q2 FY27 results today (trader/events.json): the price can gap or "
                          "swing on the numbers, so no new entries in it today"])
        ev = check.load_events({"INFY.NS": ["2026-11-04"], "TCS.NS": "2026-11-05"})
        self.assertIn("results: INFY.NS had results on 2026-11-04, the last trading day", reasons(self.one(events=ev)))
        # two days on, or results in another stock: no block
        self.assertEqual(verdicts(self.one(events=check.load_events({"INFY.NS": ["2026-11-03"]}))), ["PASS"])
        self.assertEqual(verdicts(self.one(events=check.load_events({"TCS.NS": ["2026-11-05"]}))), ["PASS"])
        self.assertEqual(verdicts(self.one(events=ev, skip_results_days=False)), ["PASS"])
        # the trading day after a Friday's results is the Monday
        ev = check.load_events({"INFY.NS": ["2026-11-06"]})
        self.assertIn("had results on 2026-11-06", reasons(self.one(events=ev, now="2026-11-09 09:40")))

    def test_surveillance(self):
        sv = {"symbols": {"INFY.NS": "ASM Stage I"}, "updated": TODAY}
        self.assertEqual(self.one(surveillance=sv)["results"][0]["reasons"],
                         ["surveillance: INFY.NS is on NSE's ASM Stage I list (trader/surveillance.json): extra "
                          "margins, often no intraday leverage, and sharp moves. No new entries"])
        self.assertEqual(verdicts(self.one(surveillance=sv, block_surveillance=False)), ["PASS"])
        res = self.one(surveillance=None)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertTrue(any("no trader/surveillance.json" in w for w in res["warnings"]))
        res = self.one(surveillance={"symbols": {}, "updated": "2026-10-20"})
        self.assertTrue(any("16 days old" in w for w in res["warnings"]))

    def test_cli_reads_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            ev, sv, orders = (os.path.join(d, x) for x in ("events.json", "surveillance.json", "orders.json"))
            with open(ev, "w") as fh:
                json.dump({"events": [{"symbol": "INFY.NS", "date": "2026-11-05", "what": "results"}]}, fh)
            with open(sv, "w") as fh:
                json.dump({"symbols": {}, "updated": TODAY}, fh)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = check.main(["--orders", INFY_PATH, "--config", CFG_PATH, "--portfolio", os.path.join(d, "x"),
                                   "--prices", os.path.join(d, "x"), "--state", os.path.join(d, "x"),
                                   "--bands", os.path.join(d, "x"), "--sectors", os.path.join(d, "x"),
                                   "--events", ev, "--surveillance", sv, "--now", NOW, "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("results: INFY.NS has results today", out.getvalue())


class Shorts(unittest.TestCase):
    """trader-budget's mirrored-reel short: sell 41 INFY.NS at 1139.50, stop 1150.20 overhead, target 1118.10."""

    def order(self, **over):
        o = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "41", "type": "market",
             "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "1118.10",
             "stop_loss_stop_price": "1150.20", "plan": {"entry": 1139.5, "stop": 1150.2, "target": 1118.1}}
        o.update(over)
        return o

    def one(self, o=None, **over):
        return check.check([o or self.order()], cfg(**over), now=at(NOW), surveillance={"symbols": {}})

    def test_off_by_default(self):
        self.assertIn("short: selling more INFY.NS than you hold (0 shares); short selling is off", reasons(self.one()))

    def test_the_short_passes_with_allow_short(self):
        res = self.one(allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "entry"))
        # 41 x 10.70 = 438.70 + 49.85 charges (STT on the sale at 1139.50, stamp on the buy back at 1150.20)
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["charges"], r["reward_risk"]), (10.7, 488.55, 49.85, 1.7))

    def test_a_short_must_be_intraday(self):
        self.assertIn("must be intraday (MIS)", reasons(self.one(self.order(product="CNC"), allow_short=True)))

    def test_the_stop_goes_overhead(self):
        o = self.order(stop_loss_stop_price="1130.00")
        o["plan"] = dict(o["plan"], stop=1130.0)
        self.assertIn("stop: the stop 1130.00 must be above the entry 1139.50 for a sell", reasons(self.one(o, allow_short=True)))

    def test_a_lower_fresh_price_is_the_entry(self):
        res = check.check([self.order()], cfg(allow_short=True), prices={"INFY.NS": 1135.0}, now=at("2026-11-05 09:50"),
                          price_times={"INFY.NS": QUOTE_TIME}, surveillance={"symbols": {}})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1135.0, "price", 15.2))
        self.assertIn("loss: 41 shares x 15.20 risk a share", reasons(res))

    def test_the_lower_band(self):
        # previous close 1150, a 5 % band: lower band 1092.50; a short at 1100 is within 1 % of it
        o = self.order(stop_loss_stop_price="1110.00", take_profit_limit_price="1080.00")
        o["plan"] = {"entry": 1100.0, "stop": 1110.0, "target": 1080.0}
        res = check.check([o], cfg(allow_short=True, max_charges_pct_of_risk=0), now=at(NOW), bands={"INFY.NS": 5},
                          prev_close={"INFY.NS": 1150.0}, surveillance={"symbols": {}})
        self.assertIn("band: INFY.NS at 1100.00 is within 1% of its lower price band 1092.50", reasons(res))
        o = self.order(stop_loss_stop_price="1210.00", take_profit_limit_price="1100.00")
        o["plan"] = {"entry": 1180.0, "stop": 1210.0, "target": 1100.0}
        res = check.check([o], cfg(allow_short=True, max_loss_per_trade_inr=2000), now=at(NOW),
                          bands={"INFY.NS": 5}, prev_close={"INFY.NS": 1150.0}, surveillance={"symbols": {}})
        self.assertIn("the stop 1210.00 is over the upper price band 1207.50", warnings(res))

    def test_buying_back_a_short_is_an_exit(self):
        pf = {"cash": 100000, "positions": [{"symbol": "INFY.NS", "qty": -41, "avg_price": 1139.5}]}
        o = {"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "41", "type": "market",
             "time_in_force": "day"}
        res = check.check([o], cfg(allow_short=True), portfolio=pf, prices={"INFY.NS": 1130.0}, now=at("2026-11-05 15:10"),
                          surveillance={"symbols": {}})
        self.assertEqual((res["results"][0]["kind"], res["results"][0]["verdict"]), ("exit", "PASS"))


class LossLimits(unittest.TestCase):
    """Weekly, monthly and same-direction limits. 5 November 2026 is a Thursday: the week began on Monday
    2 November, the month on 1 November."""

    def fills(self, day, buy=100.0, sell=74.0, sym="ABC.NS", qty="100"):
        return [{"status": "filled", "date": day, "symbol": sym, "side": "buy", "qty": qty, "price": buy},
                {"status": "filled", "date": day, "symbol": sym, "side": "sell", "qty": qty, "price": sell}]

    def one(self, state, **over):
        return check.check([infy()], cfg(**over), state=state, now=at(NOW), surveillance={"symbols": {}})

    def test_a_losing_week(self):
        # Monday: bought 100 at 100 (4.22 of charges), sold at 74 (2.22 + 1.85 STT + 0.23 + 0.01 + 0.44 GST = 4.75):
        # -2,600 - 8.97 = -2,608.97. Plus the reel's 489 at risk = 3,097.97, over 3,000
        st = {"orders": self.fills("2026-11-02")}
        self.assertEqual(check.closed_trades(st, "2026-11-02", TODAY), [("2026-11-02", "ABC.NS", -2608.97)])
        res = self.one(st)
        self.assertIn("weekly loss: -₹2,608.97 realised since 2026-11-02, plus ₹489 at risk on this one, is over "
                      "your ₹3,000 weekly limit: no new entries until next Monday", reasons(res))
        self.assertNotIn("monthly loss", reasons(res))
        self.assertEqual(verdicts(self.one(st, max_weekly_loss_inr=4000)), ["PASS"])

    def test_last_week_does_not_count_but_the_month_does(self):
        st = {"orders": self.fills("2026-10-30")}              # last Friday, last month: neither
        self.assertEqual(verdicts(self.one(st)), ["PASS"])
        st = {"orders": self.fills("2026-11-02") + self.fills("2026-11-03", 100.0, 101.0, "XYZ.NS")}
        res = self.one(st, max_weekly_loss_inr=0, max_monthly_loss_inr=2500)
        # Tuesday's +100 less 4.22 and 6.47 of charges (the sale's STT 2.525 rounds to 2.52) = +89.31
        self.assertIn("monthly loss: -₹2,519.66 realised since 2026-11-01", reasons(res))   # -2,608.97 + 89.31
        self.assertIn("no new entries until next month", reasons(res))

    def test_a_winning_week_leaves_room(self):
        st = {"orders": self.fills("2026-11-02", 100.0, 130.0)}
        self.assertEqual(verdicts(self.one(st)), ["PASS"])

    def test_same_direction(self):
        orders = [bracket(s, 2, 400.0, 200.0, 900.0) for s in ("AAA.NS", "BBB.NS", "CCC.NS")]
        res = check.check(orders, cfg(max_entries_per_sector=0), now=at(NOW), surveillance={"symbols": {}})
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("direction: ", reasons(res, 2))
        self.assertIn("already at risk in open longs today", reasons(res, 2))
        self.assertIn("over your ₹1,000 same-direction limit", reasons(res, 2))

    def test_a_drawdown_halves_the_max_loss(self):
        st = {"orders": self.fills("2026-10-20", 100.0, 40.0)}         # last month: 6.01 % under the peak
        res = self.one(st)
        self.assertIn("loss: 41 shares x 10.70 risk a share = ₹438.70 at the stop, plus ₹50.30 charges = ₹489, over "
                      "your ₹250 max loss per trade (cut for the drawdown)", reasons(res))
        self.assertTrue(any(w.startswith("drawdown: the account is 6.0% under its peak") for w in res["warnings"]))

    def test_only_open_entries_count(self):
        placed = {"status": "placed", "date": TODAY, "symbol": "ABC.NS", "side": "buy", "kind": "entry",
                  "max_loss": 600.0}
        buy = {"status": "filled", "date": TODAY, "symbol": "ABC.NS", "side": "buy", "qty": "10", "price": 100.0}
        sell = dict(buy, side="sell", price=101.0)
        res = self.one({"orders": [placed, buy]})                      # 600 open + 489 = 1,089
        self.assertIn("direction: ₹600 already at risk in open longs today, plus ₹489 = ₹1,089", reasons(res))
        self.assertNotIn("direction", reasons(self.one({"orders": [placed, buy, sell]})))   # closed: off the hook


class Calendar(unittest.TestCase):
    def one(self, now=NOW, events=None, prices=None, prev=None, times=None, **over):
        return check.check([infy()], cfg(**over), now=at(now), events=events, prices=prices, prev_close=prev,
                           price_times=times, surveillance={"symbols": {}})

    def test_a_market_event_stops_every_entry(self):
        ev = check.load_events({"events": [{"symbol": "MARKET", "date": TODAY, "what": "RBI policy"}]})
        self.assertEqual(ev, {"MARKET": [(TODAY, "RBI policy", "market")]})
        self.assertEqual(self.one(events=ev)["results"][0]["reasons"],
                         ["market event: RBI policy today (trader/events.json): the whole market can swing on it, so "
                          "no new entries today"])
        self.assertEqual(verdicts(self.one(events=ev, skip_market_event_days=False)), ["PASS"])

    def test_an_ex_date_is_that_day_only(self):
        ev = check.load_events({"INFY.NS": [TODAY]})
        ev = check.load_events({"events": [{"symbol": "INFY.NS", "date": TODAY, "what": "ex-dividend 21.50"}]})
        self.assertEqual(ev["INFY.NS"][0][2], "ex-date")
        self.assertIn("ex-date: INFY.NS goes ex-dividend 21.50 today", reasons(self.one(events=ev)))
        self.assertNotIn("results:", reasons(self.one(events=ev)))
        ev = check.load_events({"events": [{"symbol": "INFY.NS", "date": "2026-11-04", "what": "split 1:2"}]})
        self.assertEqual(verdicts(self.one(events=ev)), ["PASS"])          # the day after an ex-date is fine

    def test_a_special_session(self):
        res = self.one(now="2026-11-08 15:00")
        self.assertIn("window: today is a special session (Diwali Muhurat trading, a short Sunday session)",
                      reasons(res))

    def test_a_big_nifty_day(self):
        res = self.one(prices={"^NSEI": 24200.0, "INFY.NS": 1160.5}, prev={"^NSEI": 25000.0})
        self.assertIn("index: Nifty 50 is -3.20% from yesterday's close (25000.00 to 24200.00), past your 3% limit",
                      reasons(res))
        self.assertIn("at 10% the market-wide circuit breaker halts all trading", reasons(res))
        res = self.one(prices={"^NSEI": 22400.0}, prev={"^NSEI": 25000.0})
        self.assertIn("the market-wide circuit breaker level is reached", reasons(res))
        self.assertEqual(verdicts(self.one(prices={"^NSEI": 25300.0}, prev={"^NSEI": 25000.0})), ["PASS"])
        self.assertEqual(verdicts(self.one(prices={"^NSEI": 24200.0}, prev={"^NSEI": 25000.0},
                                           max_index_move_pct=0)), ["PASS"])

    def test_a_stale_or_missing_nifty_price_is_a_warning(self):
        old = datetime.datetime(2026, 11, 5, 3, 0)                          # 8:30 IST, 70 minutes before
        res = self.one(prices={"^NSEI": 24200.0}, prev={"^NSEI": 25000.0}, times={"^NSEI": old})
        self.assertNotIn("index:", reasons(res))
        self.assertTrue(any("Nifty 50 price in trader/prices.json is older" in w for w in res["warnings"]))
        res = self.one(prices={"INFY.NS": 1160.5}, times={"INFY.NS": QUOTE_TIME})
        self.assertTrue(any("no Nifty 50 price in trader/prices.json" in w for w in res["warnings"]))


class Upkeep(unittest.TestCase):
    def test_december_and_old_charges(self):
        d = datetime.date
        self.assertEqual(check.staleness(d(2026, 11, 9)), [])
        self.assertIn("NSE publishes 2027's trading holidays in December", check.staleness(d(2026, 12, 2))[0])
        old = check.staleness(d(2027, 11, 10))
        self.assertTrue(any("read from zerodha.com/charges on 2026-11-08, 367 days ago" in w for w in old))
        res = check.check([infy()], cfg(), now=at("2026-12-02 10:00"), surveillance={"symbols": {}})
        self.assertTrue(any("2027's trading holidays" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
