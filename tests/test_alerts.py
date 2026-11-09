import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(FIX, "trader-budget", "plan-infy.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"

# NSE equity segment trading holidays for 2026 (NSE's holiday circular). Every weekday missing from
# six months of real daily bars (May to November 2026) is on it, and nothing on it traded.
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
            "2026-11-24", "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.NSE_EARLY_CLOSE, {})
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_india_has_no_daylight_saving(self):
        for when in (DT(2026, 3, 8, 7, 0), DT(2026, 7, 1), DT(2026, 11, 1, 6, 0), DT(2027, 1, 1)):
            self.assertEqual(alerts.ist_offset(when), 5.5)

    def test_parse_now(self):
        self.assertEqual(ist("2026-11-05T09:40"), DT(2026, 11, 5, 4, 10))
        self.assertEqual(ist("2026-11-05 15:10"), DT(2026, 11, 5, 9, 40))
        self.assertEqual(ist("2026-07-01T09:15"), DT(2026, 7, 1, 3, 45))
        self.assertEqual(ist("2026-11-05T04:10:00Z"), DT(2026, 11, 5, 4, 10))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        self.assertTrue(alerts.market_status(ist("2026-11-05T10:00"))["open"])
        pre = alerts.market_status(ist("2026-11-05T09:05"))             # the pre-open auction is not the session
        self.assertFalse(pre["open"])
        self.assertEqual(pre["next_open"], "2026-11-05 09:15")
        self.assertTrue(alerts.market_status(ist("2026-11-05T15:29"))["open"])
        after = alerts.market_status(ist("2026-11-05T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["next_open"], "2026-11-06 09:15")
        diwali = alerts.market_status(ist("2026-11-10T11:00"))
        self.assertFalse(diwali["open"])
        self.assertIn("Diwali", diwali["reason"])
        self.assertEqual(diwali["next_open"], "2026-11-11 09:15")
        xmas = alerts.market_status(ist("2026-12-25T10:00"))
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        self.assertEqual(alerts.market_status(ist("2026-11-07T10:00"))["reason"], "weekend")
        self.assertFalse(alerts.market_status(ist("2027-03-01T10:00"))["calendar_known"])


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-INFY.NS.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "INFY.NS")
        self.assertEqual((q["price"], q["prev_close"], q["open"]), (1164.0, 1128.2, 1151.4))
        self.assertEqual(q["time"], "2026-11-05T04:14:58Z")
        with open(os.path.join(QDIR, "yahoo-TCS.NS.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "TCS.NS")
        self.assertEqual((q["price"], q["prev_close"]), (2498.5, 2495.7))
        self.assertEqual(q["time"], "2026-11-06T09:59:59Z")

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("infy.ns"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/INFY.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("m&m.ns"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("INFY.NS", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))                  # Stooq has no NSE quotes

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["TCS.NS", "ITC.NS", "ZZZZ.NS", "NOPE.NS"], "yahoo", QDIR)
        self.assertEqual(sorted(q), ["ITC.NS", "TCS.NS"])
        self.assertEqual(q["ITC.NS"]["price"], 283.15)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-INFY.NS.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-INFY.NS.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["INFY.NS"], "yahoo", d)
            self.assertEqual((q["INFY.NS"]["price"], errors), (1164.0, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-INFY.NS.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["INFY.NS"])
        self.assertEqual((q["INFY.NS"]["price"], errors), (1164.0, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["INFY.NS"])[0]["INFY.NS"]["price"], 1164.0)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("INFY.NS", alerts.get_quotes(["INFY.NS"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("INFY.NS", alerts.get_quotes(["INFY.NS"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["INFY.NS"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/INFY.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE.NS"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["INFY.NS"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_tickers(self):
        f = self.net([200, 200])
        alerts.get_quotes(["INFY.NS", "TCS.NS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["TCS.NS", "HDFCBANK.NS", "ITC.NS", "INFY.NS"], "yahoo", QDIR)
        self.now = ist("2026-11-06T10:00")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "tcs-2490", "symbol": "TCS.NS", "type": "above", "price": 2490}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("TCS.NS is ₹2498.50: at or above your alert level ₹2490", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_a_shorts_plan_alerts_point_the_other_way(self):
        plan = {"symbol": "INFY.NS", "side": "sell", "date": "2026-11-06", "stop": 1150.2, "target": 1118.1,
                "flat_by": "15:10"}
        rules = alerts.rules_from_plan(plan)
        self.assertEqual([r.get("side") for r in rules[:2]], ["short", "short"])
        q = {"INFY.NS": {"price": 1151.0, "prev_close": 1140.0}}
        rings, st = alerts.evaluate(rules, q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("INFY.NS is ₹1151.00: at or above your stop ₹1150.2", rings[0]["message"])
        rings, _ = alerts.evaluate(rules, {"INFY.NS": {"price": 1118.0}}, self.open, {}, self.now)
        self.assertIn("at or below your target ₹1118.1", rings[0]["message"])
        rings, _ = alerts.evaluate(rules, {"INFY.NS": {"price": 1130.0}}, self.open, {}, self.now)
        self.assertEqual(rings, [])
        # a long plan's rules carry no side and keep the stop underneath
        self.assertNotIn("side", plan_rules()[0])

    def test_stop_and_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK.NS", "type": "stop", "price": 790, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop ₹790", rings[0]["message"])
        self.q["HDFCBANK.NS"]["price"] = 791.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", st["rung"])
        self.q["HDFCBANK.NS"]["price"] = 787.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "itc-3", "symbol": "ITC.NS", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "itc-1", "symbol": "ITC.NS", "type": "pct_move", "pct": 1, "direction": "up"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("+1.54%", rings[0]["message"])                    # 283.15 / 278.85
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        down = [{"id": "itc-down", "symbol": "ITC.NS", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        hdfc = [{"id": "hdfc-3", "symbol": "HDFCBANK.NS", "type": "pct_move", "pct": 3}]
        rings, _ = alerts.evaluate(hdfc, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK.NS moved +3.72% to ₹788.25 (from ₹760.00)", rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-11-06T09:05"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-11-06T09:05"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-11-06T11:00"))[0], [])
        nxt = alerts.market_status(ist("2026-11-09T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-11-09T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("infy.ns-plan-stop", "stop"), ("infy.ns-plan-target", "target"), ("infy.ns-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1149.8, 1181.9))
        self.assertEqual((rules[2]["at"], rules[2]["before_close"]), ("15:10", True))
        self.assertTrue(all(r["date"] == "2026-11-05" for r in rules))
        rings, st = self.at("2026-11-05 09:45", rules)                  # the 9:45 quote, 1164.00
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["INFY.NS"]["price"] = 1149.00
        rings, st = self.at("2026-11-05 10:40", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["infy.ns-plan-stop"])
        self.assertIn("INFY.NS is ₹1149.00: at or below your stop ₹1149.80 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["INFY.NS"]["price"] = 1181.90
        self.assertEqual([r["id"] for r in self.at("2026-11-05 12:00", rules, st, q)[0]], ["infy.ns-plan-target"])

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-05 15:09", rules)[0], [])
        rings, st = self.at("2026-11-05 15:10", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("it is 15:10 IST (set for 15:10), the market closes at 15:30", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol INFY.NS", rings[0]["message"])
        self.assertEqual(self.at("2026-11-05 15:20", rules, st)[0], [])
        rings, _ = self.at("2026-11-05 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-06 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_a_time_alert_waits_for_a_trading_day(self):
        rule = [{"id": "flat", "type": "time", "at": "15:10", "label": "flat by close", "before_close": True}]
        self.assertEqual(self.at("2026-11-07 15:11", rule)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-11-10 15:11", rule)[0], [])         # Diwali Balipratipada
        rings, st = self.at("2026-11-11 15:10", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["flat"], "2026-11-11T09:40:00Z")

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("infy.ns-plan") or r["id"] == "infy.ns-flat-by"],
                         plan_rules())

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "TCS.NS"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "3pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "INFY.NS", "stop": 1149.8})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_reel_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added infy.ns-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-05 09:45")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-11-05 09:46")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, INFY.NS ₹1164.00.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"INFY.NS": 1164.0})
        self.assertEqual(doc["detail"]["INFY.NS"]["time"], "2026-11-05T04:14:58Z")
        self.assertEqual(doc["detail"]["INFY.NS"]["fetched"], "2026-11-05T04:16:00Z")
        self.assertEqual(doc["detail"]["INFY.NS"]["prev_close"], 1128.2)   # for trader-risk's band check

        code, text, _ = self.check("2026-11-05 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-11-05 15:12")[0], 1)
        code, text, _ = self.check("2026-11-06 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-06 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["infy.ns-flat-by", "infy.ns-plan-stop", "infy.ns-plan-target", "opening-bell"])
        with open(self.state) as fh:
            self.assertNotIn("infy.ns-flat-by", json.load(fh)["rung"])

    def test_holdings_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "TCS.NS", "--above", "2600"],
                     ["--symbol", "ITC.NS", "--stop", "270"]):
            self.assertEqual(run(["add", "--rules", self.rules] + args)[0], 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TCS.NS", "--above", "2600"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-11-06T15:45")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            self.assertEqual(json.load(fh)["prices"], {"ITC.NS": 283.15, "TCS.NS": 2498.5})

        code, text, _ = self.check("2026-11-09T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        self.assertEqual(self.check("2026-11-09T09:30")[0], 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by close",
                             "--before-close", "--symbol", "infy.ns"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "INFY.NS", "--target", "1181.90",
                             "--date", "2026-11-05"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by close",
                                    "symbol": "INFY.NS", "before_close": True})
        self.assertEqual(rules[1], {"id": "infy.ns-target-1181.90", "symbol": "INFY.NS", "type": "target",
                                    "price": 1181.9, "date": "2026-11-05"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "TCS.NS", "ITC.NS", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-06T10:00:01Z"])
        self.assertEqual(code, 0)
        self.assertIn("TCS.NS", text)
        self.assertIn("₹2498.50", text)
        self.assertIn("delayed", text)
        run(["quote", "HDFCBANK.NS", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["HDFCBANK.NS", "ITC.NS", "TCS.NS"])
        self.assertEqual(doc["detail"]["ITC.NS"]["prev_close"], 278.85)
        self.assertEqual(doc["detail"]["ITC.NS"]["fetched"], "2026-11-06T10:00:01Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "ITC.NS", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["INFY.NS", "ITC.NS"])

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-11-05 10:00"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-11-10T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Diwali", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
