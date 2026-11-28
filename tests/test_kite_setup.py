"""kite_setup.py: the folder, the login and a read-only check, with the secret never written down."""

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

ks = load("trader-broker", "kite_setup")
NOW = ks.check.parse_now("2026-11-28 10:00")
SECRET = "s3cretValue123"


class FakeKite:
    calls = []

    def __init__(self, api_key, token):
        self.key, self.token = api_key, token

    def session(self, request_token, secret):
        FakeKite.calls.append(("session", request_token, secret == SECRET))
        return {"access_token": "acc355T0ken", "user_id": "AB1234"}

    def profile(self):
        return {"user_id": "AB1234", "user_name": "Someone", "email": "x@example.com", "exchanges": ["NSE", "BSE"],
                "products": ["CNC", "MIS", "NRML"], "order_types": ["MARKET", "LIMIT", "SL", "SL-M"]}

    def positions(self):
        return {"data": {"net": [{"tradingsymbol": "INFY", "exchange": "NSE", "product": "MIS", "quantity": 1,
                                  "average_price": 1600.0, "last_price": 1601.0}], "day": []}}

    def orders(self):
        return {"data": [{"order_id": "1", "tradingsymbol": "INFY", "exchange": "NSE", "transaction_type": "BUY",
                          "order_type": "MARKET", "product": "MIS", "quantity": 1, "pending_quantity": 0,
                          "status": "COMPLETE", "placed_by": "AB1234", "tag": ""}]}

    def trades(self):
        return {"data": [{"trade_id": "9", "order_id": "1", "tradingsymbol": "INFY", "exchange": "NSE",
                          "transaction_type": "BUY", "quantity": 1, "average_price": 1600.0}]}


class Token(unittest.TestCase):
    def test_what_can_be_pasted(self):
        url = "http://127.0.0.1/?action=login&type=login&status=success&request_token=AbC123xyz9Q"
        self.assertEqual(ks.request_token_of(url), "AbC123xyz9Q")
        self.assertEqual(ks.request_token_of("  AbC123xyz9Q "), "AbC123xyz9Q")
        with self.assertRaises(ks.SetupError):
            ks.request_token_of("http://127.0.0.1/?status=error&request_token=AbC123xyz9Q")
        with self.assertRaises(ks.SetupError):
            ks.request_token_of("http://127.0.0.1/?status=success")
        with self.assertRaises(ks.SetupError):
            ks.request_token_of("not a token!")

    def test_personal_fields_are_taken_out(self):
        r = ks.redact({"profile": FakeKite("k", "t").profile(), "orders": FakeKite("k", "t").orders()})
        self.assertEqual((r["profile"]["user_id"], r["profile"]["email"], r["profile"]["user_name"]),
                         ("REDACTED",) * 3)
        self.assertEqual(r["orders"]["data"][0]["placed_by"], "REDACTED")
        self.assertEqual(r["orders"]["data"][0]["tradingsymbol"], "INFY")


class Run(unittest.TestCase):
    def setUp(self):
        self.old, self.d = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.d)
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            os.environ.pop(k, None)
        FakeKite.calls = []

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def run_it(self, argv=(), answers=("myapikey", "http://127.0.0.1/?status=success&request_token=Req0001tok")):
        asked = list(answers)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = ks.main(list(argv), ask=lambda prompt: asked.pop(0), ask_secret=lambda prompt: SECRET,
                           browser=lambda url: True, kite_cls=FakeKite, now=NOW)
        return code, out.getvalue()

    def test_set_up_log_in_and_check(self):
        code, out = self.run_it(["--save", "kite-sample.json"])
        self.assertEqual(code, 0, out)
        self.assertEqual(FakeKite.calls, [("session", "Req0001tok", True)])
        with open(os.path.join("trader", "config.json")) as fh:
            self.assertTrue(json.load(fh)["paper"])                          # set up on paper
        sess = os.path.join("trader", "state", "kite_session.json")
        self.assertEqual(stat.S_IMODE(os.stat(sess).st_mode), 0o600)
        with open(sess) as fh:
            self.assertEqual(json.load(fh), {"api_key": "myapikey", "access_token": "acc355T0ken",
                                             "date": "2026-11-28"})
        for base, _, files in os.walk("."):                                 # the secret is nowhere on disk
            for f in files:
                with open(os.path.join(base, f), "rb") as fh:
                    self.assertNotIn(SECRET.encode(), fh.read(), f)
        self.assertIn("SL-M stop-loss orders  yes", out)
        self.assertIn("open MIS positions     1 INFY.NS", out)
        self.assertNotIn("AB1234", out)
        with open("kite-sample.json") as fh:
            saved = fh.read()
        self.assertNotIn("AB1234", saved)
        self.assertNotIn("x@example.com", saved)
        self.assertNotIn("acc355T0ken", saved)

    def test_todays_session_needs_no_login_and_a_config_is_left_alone(self):
        os.makedirs(os.path.join("trader", "state"))
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"paper": False, "budget": 50000}, fh)
        ks.guardian.save_session(ks.guardian.SESSION, "myapikey", "acc355T0ken", NOW)
        code, out = self.run_it(answers=())
        self.assertEqual(code, 0, out)
        self.assertEqual(FakeKite.calls, [])
        self.assertIn("no login needed", out)
        self.assertIn("config: LIVE account", out)
        with open(os.path.join("trader", "config.json")) as fh:
            self.assertEqual(json.load(fh), {"paper": False, "budget": 50000})

    def test_an_account_without_sl_m_says_so(self):
        class NoSLM(FakeKite):
            def profile(self):
                return dict(FakeKite.profile(self), order_types=["MARKET", "LIMIT"])
        asked = ["myapikey", "Req0001tok"]
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = ks.main([], ask=lambda p: asked.pop(0), ask_secret=lambda p: SECRET, browser=lambda u: False,
                           kite_cls=NoSLM, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("SL-M stop-loss orders  NO", out.getvalue())
        self.assertIn("https://kite.zerodha.com/connect/login?v=3&api_key=myapikey", out.getvalue())


if __name__ == "__main__":
    unittest.main()
