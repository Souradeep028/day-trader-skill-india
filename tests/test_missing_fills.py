"""Live fills that never reached the log block live entries until they are added (or marked as never filled), and
the live ramp pairs each round trip with its own approved entry."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_risk as R

check = R.check
ticket = R.ticket
preflight = load("trader-broker", "preflight")

TODAY = "2026-11-26"          # Thursday; the 24th was an NSE holiday
YDAY = "2026-11-25"


def approved(sym="INFY.NS", day=YDAY, fp="f1", paper=False, at="09:40", max_loss=489.0):
    return {"status": "approved", "date": day, "at": "%sT%s:00+05:30" % (day, at), "symbol": sym, "side": "buy",
            "kind": "entry", "fingerprint": fp, "paper": paper, "max_loss": max_loss, "qty": "41"}


def fill(side, sym="INFY.NS", day=YDAY, qty="41", price=1160.0, at="09:41", **extra):
    r = {"status": "filled", "date": day, "at": "%sT%s:00+05:30" % (day, at), "symbol": sym, "side": side,
         "qty": qty, "price": price}
    r.update(extra)
    return r


class MissingFills(unittest.TestCase):
    def gaps(self, rows):
        return check.missing_fills({"orders": rows}, TODAY)

    def test_an_approved_entry_with_no_fill(self):
        self.assertEqual(self.gaps([approved()]),
                         [(YDAY, "INFY.NS", "the buy entry was approved but no live fill is logged")])

    def test_what_accounts_for_it(self):
        self.assertEqual(self.gaps([approved(), fill("buy"), fill("sell", at="10:05")]), [])
        self.assertEqual(self.gaps([approved(), dict(approved(), status="failed")]), [])        # never placed
        self.assertEqual(self.gaps([approved(paper=True)]), [])
        self.assertEqual(self.gaps([approved(), {"status": "no-fill", "date": YDAY, "symbol": "INFY.NS",
                                                 "note": "limit expired"}]), [])
        self.assertEqual(self.gaps([approved(day="2026-11-17")]), [])                           # over 5 sessions back
        self.assertEqual(self.gaps([approved(day=TODAY)]), [])                                  # today syncs live

    def test_fills_that_do_not_net_to_flat(self):
        self.assertEqual(self.gaps([approved(), fill("buy")]),
                         [(YDAY, "INFY.NS", "the logged fills leave 41 shares long: an exit is not logged")])
        self.assertEqual(self.gaps([fill("buy", product="CNC")]), [])                           # delivery is held
        self.assertEqual(self.gaps([fill("buy", paper=True)]), [])

    def test_the_holiday_is_skipped_in_the_five_sessions(self):
        # 25, 23, 20, 19, 18 November: the 18th counts, the 17th does not
        self.assertTrue(self.gaps([approved(day="2026-11-18")]))
        self.assertFalse(self.gaps([approved(day="2026-11-17")]))


class RiskGate(unittest.TestCase):
    NOW = TODAY + " 10:00"

    def one(self, rows, paper=False):
        c = R.cfg(paper=paper, max_loss_per_trade_inr=2000)
        return check.check([R.infy()], c, now=R.at(self.NOW), state={"orders": rows}, forward=R.passed("2026-11-20"),
                           **R.live_inputs(self.NOW))

    def test_live_entries_wait_for_the_missing_fills(self):
        res = self.one([approved()])
        self.assertEqual(R.verdicts(res), ["BLOCK"])
        self.assertIn("live: fills missing from the log (%s INFY.NS: the buy entry was approved but no live fill is "
                      "logged)" % YDAY, R.reasons(res))
        self.assertIn("ticket.py nofill", R.reasons(res))
        self.assertEqual(R.verdicts(self.one([approved()], paper=True)), ["PASS"])
        self.assertEqual(R.verdicts(self.one([approved(), fill("buy"), fill("sell", at="10:05")])), ["PASS"])


class NoFillCommand(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "broker.json")
        ticket.write_json(self.state, {"orders": [approved()]})

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_it_needs_a_reason_and_clears_the_gap(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(ticket.main(["nofill", "--symbol", "INFY", "--date", YDAY, "--note", " ",
                                          "--state", self.state]), 1)
        self.assertIn("say why it never filled", err.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ticket.main(["nofill", "--symbol", "INFY", "--date", YDAY, "--note",
                                          "limit expired unfilled", "--state", self.state]), 0)
        self.assertIn("Recorded: INFY.NS did not fill on %s (limit expired unfilled)." % YDAY, out.getvalue())
        self.assertEqual(check.missing_fills(ticket.load_state(self.state), TODAY), [])


class Preflight(unittest.TestCase):
    def doc(self, cfg, rows):
        d = tempfile.mkdtemp()
        try:
            return preflight.run_checks(check.parse_now(TODAY + " 08:45"), cfg, None, {"orders": rows}, None, None,
                                        {"symbols": {}}, None, d)
        finally:
            shutil.rmtree(d)

    def row(self, doc):
        return [x for x in doc["checks"] if x["check"] == "fills"]

    def test_a_live_morning_checks_the_log(self):
        live = {"paper": False, "budget": 100000}
        doc = self.doc(live, [approved()])
        self.assertFalse(self.row(doc)[0]["ok"])
        self.assertTrue(any(x.startswith("fills: fills missing from the log") for x in doc["no_go"]))
        self.assertTrue(self.row(self.doc(live, []))[0]["ok"])
        self.assertEqual(self.row(self.doc({"paper": True, "budget": 100000}, [approved()])), [])


class RampPairing(unittest.TestCase):
    def test_each_round_trip_gets_its_own_approved_risk(self):
        # two round trips in ABC on one day: approved at 500, then at 250; each loses 250 and charges
        rows = [approved("ABC.NS", YDAY, "a", at="09:30", max_loss=500.0),
                fill("buy", "ABC.NS", qty="10", price=100.0, at="09:31"),
                fill("sell", "ABC.NS", qty="10", price=75.0, at="09:40"),
                approved("ABC.NS", YDAY, "b", at="10:00", max_loss=250.0),
                fill("buy", "ABC.NS", qty="10", price=100.0, at="10:01"),
                fill("sell", "ABC.NS", qty="10", price=75.0, at="10:10")]
        net = check.closed_trades({"orders": rows}, YDAY, YDAY)[0][2]
        ramp = check.live_ramp(R.passed("2026-11-20"), {"orders": rows}, TODAY)
        self.assertEqual(ramp["R"], [round(net / 500.0, 4), round(net / 250.0, 4)])     # was net / 250 for both


if __name__ == "__main__":
    unittest.main()
