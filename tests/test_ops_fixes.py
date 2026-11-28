"""One copy of the charges, the stop-slippage buffer, the measured stop slippage, the doctor's live-fills row, the
guardian's --forever mode and its opt-in Docker service."""

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
tradercore = check.tradercore
size = load("trader-budget", "size")
ticket = load("trader-broker", "ticket")
g = load("trader-broker", "guardian")
doctor = load("core", "doctor")

TODAY = "2026-11-05"


def plan():
    with open(os.path.join(FIX, "trader-budget", "plan-infy.json")) as fh:
        return json.load(fh)


def cfg(**over):
    with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
        c = json.load(fh)
    c.update(over)
    return c


class OneCopy(unittest.TestCase):
    def test_every_skill_calls_the_core_function(self):
        for mod in (check, ticket, size, load("trader-timemachine", "edge"), load("trader-timemachine", "timemachine")):
            with self.subTest(mod.__name__):
                self.assertIn("tradercore.order_charges", open(mod.__file__).read())
                self.assertEqual(mod.order_charges("sell", 75000), tradercore.order_charges("sell", 75000))
        # 75,000 on each side: 0.03 % is 22.50, capped at 20 a side
        self.assertEqual(tradercore.order_charges("buy", 75000)["brokerage"], 20.0)
        self.assertEqual(tradercore.order_charges("sell", 75000)["stt"], 18.75)


class StopSlippage(unittest.TestCase):
    def test_the_buffer_is_where_the_stop_fills(self):
        self.assertEqual(tradercore.slipped_stop(1149.8, True, 0), 1149.8)
        self.assertEqual(tradercore.slipped_stop(1149.8, True, 10), 1148.65)     # 1149.8 x 0.999, down to the paisa
        self.assertEqual(tradercore.slipped_stop(1150.2, False, 10), 1151.36)    # 1150.2 x 1.001, up to the paisa

    def test_zero_changes_nothing_and_a_buffer_sizes_smaller(self):
        base = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((base["qty"], base["max_loss"]), (41, 489.0))           # the reel, unchanged at 0 bp
        s = size.size_trade(plan(), cfg(stop_slippage_bp=10), today=TODAY)
        self.assertLess(s["qty"], 41)
        self.assertLessEqual(s["max_loss"], 500.0)
        self.assertEqual(s["stop_fill"], 1148.65)
        self.assertAlmostEqual(s["loss_per_share"], 11.85)                         # 1160.50 - 1148.65
        self.assertEqual(s["loss_at_stop"], round(s["qty"] * 11.85, 2))
        self.assertIn("x 11.85 = ", size.bracket_order(s)["why"])

    def test_the_sizer_and_the_risk_check_agree(self):
        for bp in (0, 5, 10, 25):
            with self.subTest(bp=bp):
                c = cfg(stop_slippage_bp=bp, max_entries_per_sector=0, block_surveillance=False)
                s = size.size_trade(plan(), c, today=TODAY)
                o = size.bracket_order(s)
                res = check.check([o], c, now=check.parse_now(TODAY + " 09:40"), stop=False,
                                  surveillance={"symbols": {}})["results"][0]
                self.assertEqual(res["verdict"], "PASS", res["reasons"])
                self.assertEqual(res["max_loss"], s["max_loss"])
                bigger = dict(o, qty=str(s["qty"] + 1))
                res = check.check([bigger], c, now=check.parse_now(TODAY + " 09:40"), stop=False,
                                  surveillance={"symbols": {}})["results"][0]
                self.assertTrue(any(r.startswith("loss: ") for r in res["reasons"]), (bp, res["reasons"]))

    def test_a_buffer_lowered_today_holds_the_morning_value(self):
        eff, warns = check.hold_morning_limits({"stop_slippage_bp": 0}, {"stop_slippage_bp": 10})
        self.assertEqual(eff, {"stop_slippage_bp": 10})

    def test_the_approval_records_what_trader_risk_passed(self):
        d = tempfile.mkdtemp()
        try:
            o = dict(size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)), type="limit",
                     limit_price="1160.50")
            paths = {k: os.path.join(d, k + ".json") for k in ("orders", "config", "risk", "state")}
            ticket.write_json(paths["orders"], {"orders": [o]})
            ticket.write_json(paths["config"], cfg())
            ticket.write_json(paths["risk"], {"date": TODAY, "results": [
                {"fingerprint": ticket.fingerprint(o), "verdict": "PASS", "kind": "entry", "max_loss": 497.12}]})
            with redirect_stdout(io.StringIO()):
                code = ticket.main(["approve", "--index", "1", "--reply", "yes", "--orders", paths["orders"],
                                    "--config", paths["config"], "--risk", paths["risk"], "--state", paths["state"],
                                    "--now", TODAY + " 09:41"])
            self.assertEqual(code, 0)
            self.assertEqual(ticket.load_state(paths["state"])["orders"][0]["max_loss"], 497.12)
        finally:
            shutil.rmtree(d)


class MeasuredSlippage(unittest.TestCase):
    def test_the_journal_measures_fills_past_the_stop(self):
        def trade(day, sell, paper=False):
            return [{"status": "approved", "date": day, "symbol": "ABC.NS", "side": "buy", "kind": "entry",
                     "stop": 100.0, "paper": paper},
                    {"status": "filled", "date": day, "at": day + "T09:40", "symbol": "ABC.NS", "side": "buy",
                     "qty": "10", "price": 105.0, "paper": paper},
                    {"status": "filled", "date": day, "at": day + "T10:00", "symbol": "ABC.NS", "side": "sell",
                     "qty": "10", "price": sell, "paper": paper}]
        rows = trade("2026-11-02", 99.9) + trade("2026-11-03", 99.5) + trade("2026-11-04", 110.0) \
            + trade("2026-11-05", 99.8, paper=True)
        trades, _ = ticket.round_trips({"orders": rows})
        ss = ticket.stop_slippage(trades)
        self.assertEqual(ss["live"], {"trades": 2, "avg_bp": 30.0, "p80_bp": 50.0, "worst_bp": 50.0})   # 10 and 50 bp
        self.assertEqual(ss["paper"]["trades"], 1)


class InFolder(unittest.TestCase):
    def setUp(self):
        self.old, self.d = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.d)


class DoctorFills(InFolder):
    def test_a_live_fills_row(self):
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"paper": False, "budget": 100000}, fh)
        ticket.write_json(os.path.join("trader", "state", "broker.json"), {"orders": [
            {"status": "approved", "date": "2026-11-25", "symbol": "INFY.NS", "side": "buy", "kind": "entry",
             "fingerprint": "f", "paper": False}]})
        rows = {n: (lvl, d) for lvl, n, d in doctor.run(now_utc=check.parse_now("2026-11-26 08:30"))}
        self.assertEqual(rows["live fills"][0], "FAIL")
        self.assertIn("2026-11-25 INFY.NS", rows["live fills"][1])


class Forever(InFolder):
    def test_it_waits_out_the_night_and_retries_the_login(self):
        a = argparse.Namespace(session=os.path.join("trader", "state", "kite_session.json"), now=None)
        times = iter(["2026-11-05 08:00", "2026-11-05 09:20", "2026-11-05 09:21", "2026-11-05 09:22",
                      "2026-11-05 09:23", "2026-11-05 16:00"])
        sent, ran, slept = [], [], []
        real = g.notify
        g.notify = lambda text: sent.append(text) or True

        def runner(a_, loop):
            ran.append(loop)
            return 1 if len(ran) == 1 else 0         # the first session expires; the second runs to the close
        try:
            with redirect_stderr(io.StringIO()):
                calls = [0]

                def clock():
                    calls[0] += 1
                    t = next(times)
                    if calls[0] == 4:                  # the user logs in at 09:22
                        ticket.write_json(a.session, {"api_key": "k", "access_token": "t", "date": "2026-11-05"})
                    return check.parse_now(t)
                g.forever(a, clock=clock, sleep=slept.append, runner=runner, loops=6)
        finally:
            g.notify = real
        self.assertEqual(sent, ["no Kite session for 2026-11-05: run guardian.py login. Positions are NOT being "
                                "guarded"])               # once, though two passes had no session
        self.assertEqual(ran, [True, True])
        self.assertEqual(len(slept), 5)                 # night, two waits for the login, the retry, the evening


class Compose(unittest.TestCase):
    def test_the_guardian_service_is_opt_in(self):
        with open(os.path.join(ROOT, "docker", "compose.yaml")) as fh:
            dc = fh.read()
        trader, guardian = dc.split("\n  guardian:\n")
        self.assertNotIn("profiles:", trader)                                     # the scheduler always runs
        self.assertIn('profiles: ["live"]', guardian)                           # the guardian only on request
        self.assertIn('command: ["run", "--arm", "--forever"]', guardian)
        self.assertIn('entrypoint: ["python3", "/app/trader-broker/guardian.py"]', guardian)
        self.assertNotIn("KITE_API_SECRET:", guardian)                          # no secret kept in the service
        self.assertIn("GUARDIAN_PING_URL", guardian)


if __name__ == "__main__":
    unittest.main()
