"""ticket.py sync: Kite's executed trades into the broker log, once each, so the risk limits see live fills."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
guardian = load("trader-broker", "guardian")

TODAY = "2026-11-05"
NOW = ticket.parse_now(TODAY + " 10:30")
CFG = {"paper": False, "budget": 200000, "max_daily_loss_inr": 1500, "max_loss_per_trade_inr": 500}


def trade(tid, oid, sym, side, qty, price, at="09:45:10", exchange="NSE"):
    return {"trade_id": tid, "order_id": oid, "exchange": exchange, "tradingsymbol": sym, "product": "MIS",
            "average_price": price, "quantity": qty, "transaction_type": side.upper(),
            "fill_timestamp": "%s %s" % (TODAY, at), "exchange_timestamp": "%s %s" % (TODAY, at)}


class Sync(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.cfg_path = os.path.join(self.d, "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump(CFG, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def sync(self, trades):
        return ticket.sync({"status": "success", "data": trades}, self.state, self.pf, CFG, NOW)

    def fills(self):
        return [r for r in ticket.load_state(self.state)["orders"] if r["status"] == "filled"]

    def test_partial_fills_of_one_order_are_one_record_and_never_twice(self):
        ts = [trade("1", "A", "INFY", "buy", 20, 1160.0), trade("2", "A", "INFY", "buy", 21, 1161.0, "09:45:11")]
        added, warns = self.sync(ts)
        self.assertEqual(warns, [])
        self.assertEqual(len(added), 1)
        r = added[0]
        self.assertEqual((r["symbol"], r["side"], r["qty"], r["price"], r["trade_ids"], r["broker_order_id"]),
                         ("INFY.NS", "buy", "41", round((20 * 1160 + 21 * 1161) / 41.0, 4), ["1", "2"], "A"))
        self.assertEqual(r["at"], TODAY + "T09:45:11+05:30")
        self.assertEqual(r["charges"], ticket.order_charges("buy", 41 * r["price"])["total"])   # one order, one cap
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["positions"][0]["shares"], 41)
        self.assertEqual(self.sync(ts), ([], []))
        ts.append(trade("3", "B", "INFY", "sell", 41, 1149.5, "10:02:00"))       # the guardian's stop filled
        added, _ = self.sync(ts)
        self.assertEqual([(r["side"], r["qty"], r["trade_ids"]) for r in added], [("sell", "41", ["3"])])
        self.assertEqual(len(self.fills()), 2)
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["positions"], [])

    def test_fills_logged_by_hand_are_not_counted_twice(self):
        ticket.log_fill(self.state, NOW, TODAY, "INFY.NS", "buy", 41, 1160.5, CFG)        # ticket.py result / fill
        added, warns = self.sync([trade("1", "A", "INFY", "buy", 41, 1160.5)])
        self.assertEqual((added, warns), ([], []))
        self.assertEqual(len(self.fills()), 1)

    def test_a_partial_hand_log_leaves_the_rest(self):
        ticket.log_fill(self.state, NOW, TODAY, "INFY.NS", "buy", 20, 1160.0, CFG)
        added, _ = self.sync([trade("1", "A", "INFY", "buy", 20, 1160.0), trade("2", "A", "INFY", "buy", 21, 1161.0)])
        self.assertEqual([(r["qty"], r["trade_ids"]) for r in added], [("21", ["1", "2"])])
        self.assertAlmostEqual(added[0]["price"], (20 * 1160 + 21 * 1161) / 41.0, places=3)

    def test_more_by_hand_than_kite_shows_is_a_warning(self):
        ticket.log_fill(self.state, NOW, TODAY, "INFY.NS", "buy", 50, 1160.0, CFG)
        added, warns = self.sync([trade("1", "A", "INFY", "buy", 41, 1160.0)])
        self.assertEqual(added, [])
        self.assertEqual(warns, ["INFY.NS: 9 buy shares logged by hand on %s that Kite's trades do not show: check "
                                 "them" % TODAY])

    def test_paper_fills_and_other_exchanges_are_left_out(self):
        ticket.log_fill(self.state, NOW, TODAY, "INFY.NS", "buy", 41, 1160.5, CFG, paper=True)
        added, _ = self.sync([trade("1", "A", "INFY", "buy", 41, 1160.5), trade("2", "B", "TCS", "buy", 5, 3000, exchange="BSE"),
                              dict(trade("", "C", "SBIN", "buy", 5, 1000))])
        self.assertEqual([(r["symbol"], r["qty"]) for r in added], [("INFY.NS", "41")])     # paper is not a live fill

    def test_a_portfolio_that_disagrees_still_logs_the_fill(self):
        added, warns = self.sync([trade("1", "A", "INFY", "sell", 41, 1150.0)])             # nothing held in it
        self.assertEqual(len(added), 1)
        self.assertTrue(warns and "the fill is logged but" in warns[0])

    def test_the_daily_loss_sees_a_slipped_stop_once_synced(self):
        # why sync exists: the guardian's stop slipped at Kite; until it is logged, the risk check cannot see it
        state = {"orders": [{"status": "placed", "kind": "entry", "date": TODAY, "symbol": "INFY.NS", "side": "buy",
                             "max_loss": 500}]}
        ticket.write_json(self.state, state)
        order = {"symbol": "TCS.NS", "side": "buy", "qty": "10", "type": "limit", "limit_price": "3000",
                 "order_class": "bracket", "stop_loss_stop_price": "2960", "take_profit_limit_price": "3100",
                 "product": "MIS"}
        cfg = dict(CFG, paper=True, max_entries_per_sector=0, block_surveillance=False, max_losing_streak=0)

        def verdict():
            return check.check([order], cfg, state=ticket.load_state(self.state), now=NOW, stop=False,
                               surveillance={"symbols": {}})["results"][0]["verdict"]
        self.assertEqual(verdict(), "PASS")
        self.sync([trade("1", "A", "INFY", "buy", 100, 1000.0), trade("2", "B", "INFY", "sell", 100, 989.5, "10:05:00")])
        self.assertEqual(verdict(), "BLOCK")

    def test_a_late_synced_entry_still_pairs_as_a_long(self):
        # the exit was typed in by hand at 10:05; the entry (09:45) only reaches the log when sync runs later
        ticket.log_fill(self.state, ticket.parse_now(TODAY + " 10:05"), TODAY, "INFY.NS", "sell", 41, 1170.0, CFG)
        self.sync([trade("1", "A", "INFY", "buy", 41, 1160.0, "09:45:00")])
        state = ticket.load_state(self.state)
        self.assertEqual([r["side"] for r in state["orders"]], ["sell", "buy"])          # file order: exit first
        trades, still_open = ticket.round_trips(state)
        self.assertEqual((len(trades), trades[0]["side"], still_open), (1, "long", []))
        net = check.closed_trades(state, TODAY, TODAY)[0][2]
        self.assertAlmostEqual(net, 41 * 10.0 - ticket.order_charges("buy", 41 * 1160.0)["total"]
                               - ticket.order_charges("sell", 41 * 1170.0)["total"], places=2)
        self.assertEqual(check.losing_streak(state, TODAY)[0], 0)

    def test_the_cli_refuses_paper_and_logs_live(self):
        tpath = os.path.join(self.d, "trades.json")
        with open(tpath, "w") as fh:
            json.dump({"data": [trade("1", "A", "INFY", "buy", 41, 1160.5)]}, fh)
        args = ["sync", "--trades", tpath, "--state", self.state, "--portfolio", self.pf, "--config", self.cfg_path,
                "--now", TODAY + " 10:30"]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ticket.main(args), 0)
        self.assertIn("Logged BUY 41 INFY.NS at 1160.50 (Kite order A, 1 trade)", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            ticket.main(args)
        self.assertIn("Nothing new", out.getvalue())
        with open(self.cfg_path, "w") as fh:
            json.dump(dict(CFG, paper=True), fh)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(ticket.main(args), 1)
        self.assertIn("paper account", err.getvalue())

    def test_the_guardian_syncs_after_its_pass(self):
        class K:
            def trades(self):
                return {"data": [trade("1", "A", "INFY", "buy", 41, 1160.5)]}
        said = []
        added = guardian.sync_fills(K(), self.state, self.pf, CFG, NOW, say=said.append)
        self.assertEqual(len(added), 1)
        self.assertIn("FILL  logged BUY 41 INFY.NS at 1160.50 (Kite order A)", said)
        self.assertEqual(guardian.sync_fills(K(), self.state, self.pf, CFG, NOW, say=said.append), [])


if __name__ == "__main__":
    unittest.main()
