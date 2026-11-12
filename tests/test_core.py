"""core/: the shared tables (tradercore.py), the one command (trader.py), the health check and the config migration
(doctor.py), the stop switch, the MCP server (mcp_server.py) and the standalone skill zips (package.py)."""

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from decimal import Decimal

from _load import load, ROOT

CORE = os.path.join(ROOT, "core")
sys.path.insert(0, CORE)
import tradercore  # noqa: E402
import doctor  # noqa: E402
import mcp_server  # noqa: E402

check = load("trader-risk", "check")
preflight = load("trader-broker", "preflight")
forward = load("trader-broker", "forward")
TRADER = [sys.executable, os.path.join(CORE, "trader.py")]


def run(*args, cwd=None, env=None):
    return subprocess.run(TRADER + list(args), cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))


class Tables(unittest.TestCase):
    def test_every_skill_reads_the_one_copy(self):
        self.assertEqual(check.NSE_HOLIDAYS["2026-11-24"], "Prakash Gurpurb Sri Guru Nanak Dev")
        self.assertEqual(check.CALENDAR_YEARS, (2026,))
        self.assertEqual(check.CHARGES, tradercore.charges("zerodha"))
        self.assertEqual(check.CHARGES_READ, tradercore.broker("zerodha")["read"])
        exit_ = load("trader-exit", "exit")
        edge = load("trader-timemachine", "edge")
        self.assertEqual(exit_.tick_for(Decimal("999.95")), Decimal("0.05"))      # Decimal where exit.py needs it
        self.assertEqual((exit_.tick_for(Decimal("25000")), edge.tick_for(25000.0)), (Decimal("5.00"), 5.0))
        self.assertEqual(edge.tick_for(240.0), 0.01)
        self.assertEqual(tradercore.brokers(), ["zerodha"])

    def test_a_missing_table_says_where_it_looked(self):
        d = tempfile.mkdtemp()
        try:
            env = dict(os.environ, TRADER_CORE=d)
            r = subprocess.run([sys.executable, "-c", "import tradercore; tradercore.broker('nobroker')"], cwd=CORE,
                               capture_output=True, text=True, env=env)
            self.assertIn("brokers/nobroker.json is missing", r.stderr)
        finally:
            shutil.rmtree(d)

    def test_the_tables_are_in_the_code_fingerprint(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(CORE, os.path.join(d, "core"), ignore=shutil.ignore_patterns("__pycache__"))
            v1 = forward.code_version(d)
            path = os.path.join(d, "core", "brokers", "zerodha.json")
            with open(path) as fh:
                z = json.load(fh)
            z["charges"]["brokerage_max"] = 25.0
            with open(path, "w") as fh:
                json.dump(z, fh)
            self.assertNotEqual(v1, forward.code_version(d))           # new charges are new results
        finally:
            shutil.rmtree(d)


class Folder(unittest.TestCase):
    def setUp(self):
        self.old, self.d = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def config(self, **kw):
        os.makedirs("trader", exist_ok=True)
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        cfg.update(kw)
        for k in [k for k, v in kw.items() if v is None]:
            del cfg[k]
        with open("trader/config.json", "w") as fh:
            json.dump(cfg, fh)
        return cfg


class Doctor(Folder):
    def levels(self, today=datetime.date(2026, 11, 12)):
        return {n: (lvl, det) for lvl, n, det in doctor.run(today)}

    def test_a_fresh_folder_is_healthy(self):
        self.config()
        got = self.levels()
        self.assertEqual({n for n, (lvl, _) in got.items() if lvl != "OK"}, set(), got)
        self.assertIn("paper, budget ₹1,00,000", got["account"][1])
        self.assertIn("Zerodha, read 2026-11-08", got["charges"][1])

    def test_what_it_catches(self):
        self.config(config_version=None, max_los_per_trade_inr=500, paper=False)
        with open("trader/STOP", "w") as fh:
            fh.write("bad week\n")
        os.makedirs("trader/state")
        with open("trader/state/scheduler.json", "w") as fh:
            json.dump({"alive": "2026-11-12 09:00:00", "next": "x"}, fh)
        got = doctor.run(datetime.date(2026, 12, 2), now_utc=datetime.datetime(2026, 12, 2, 6, 0))
        got = {n: (lvl, det) for lvl, n, det in got}
        self.assertEqual(got["config format"][0], "WARN")                       # an old config: migrate
        self.assertIn("max_los_per_trade_inr", got["config keys"][1])            # a typo is not silently ignored
        self.assertIn("LIVE", got["account"][1])
        self.assertIn("bad week", got["stop switch"][1])
        self.assertEqual(got["holidays"][0], "WARN")                             # December: next year's list
        self.assertEqual(got["scheduler"][0], "WARN")                            # not alive for 20 days
        self.assertIn("is the container running", got["scheduler"][1])

    def test_no_folder_and_a_new_year_fail(self):
        got = {n: lvl for lvl, n, _ in doctor.run(datetime.date(2027, 1, 4))}
        self.assertEqual((got["holidays"], got["trading folder"]), ("FAIL", "FAIL"))
        r = run("doctor")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_the_bundle_holds_no_trades(self):
        self.config()
        os.makedirs("trader/state")
        os.makedirs("trader/logs")
        with open("trader/state/broker.json", "w") as fh:
            fh.write('{"orders": ["a trade"]}')
        with open("trader/logs/autopilot.log", "w") as fh:
            fh.write("".join("line %d\n" % i for i in range(500)))
        r = run("doctor", "--bundle", "doc.zip", "--today", "2026-11-12")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with zipfile.ZipFile("doc.zip") as z:
            names = sorted(z.namelist())
            log = z.read("trader/logs/autopilot.log").decode()
        self.assertEqual(names, ["doctor.txt", "trader/config.json", "trader/logs/autopilot.log"])
        self.assertEqual(log.splitlines()[0], "line 300")                        # the last 200 lines only


class Migrate(Folder):
    def test_an_old_config_moves_up_and_keeps_every_limit(self):
        old = self.config(config_version=None, broker=None, max_loss_per_trade_inr=350)
        new, notes = doctor.migrate(old)
        self.assertEqual((new["config_version"], new["broker"], new["max_loss_per_trade_inr"]),
                         (doctor.CONFIG_VERSION, "zerodha", 350))
        self.assertEqual({k: v for k, v in new.items() if k in old}, old)        # nothing you set is changed
        self.assertEqual(len(notes), 2)
        r = run("migrate")
        self.assertIn("No limit was changed", r.stdout)
        with open("trader/config.json") as fh:
            self.assertEqual(json.load(fh)["config_version"], doctor.CONFIG_VERSION)
        self.assertTrue(os.path.exists("trader/config.json.v2.bak"))
        self.assertIn("nothing to do", run("migrate").stdout)


class StopSwitch(Folder):
    ORDER = {"symbol": "INFY.NS", "side": "buy", "qty": 10, "type": "limit", "limit_price": 1500.0,
             "exchange": "NSE", "product": "MIS", "order_class": "bracket", "stop_loss_stop_price": 1480.0,
             "take_profit_limit_price": 1540.0, "plan": {"entry": 1500.0, "stop": 1480.0, "target": 1540.0}}

    def verdict(self, order):
        cfg = self.config(allow_short=True)
        res = check.check([order], cfg, prices={"INFY.NS": 1500.0}, today="2026-11-12",
                          now=datetime.datetime(2026, 11, 12, 5, 0))
        return res["results"][0]

    def test_it_blocks_entries_and_never_exits(self):
        self.assertEqual(self.verdict(self.ORDER)["verdict"], "PASS")
        self.assertEqual(run("stop", "a", "bad", "week").returncode, 0)
        r = self.verdict(self.ORDER)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("stop switch" in x and "a bad week" in x for x in r["reasons"]), r["reasons"])
        # the way out of a position is never blocked
        state = {"orders": [{"date": "2026-11-12", "symbol": "INFY.NS", "side": "buy", "qty": 10, "status": "filled",
                             "max_loss": 120.0, "price": 1500.0}]}
        cfg = self.config()
        sell = {"symbol": "INFY.NS", "side": "sell", "qty": 10, "type": "market", "exchange": "NSE", "product": "MIS"}
        out = check.check([sell], cfg, portfolio={"cash": 85000, "positions": [{"symbol": "INFY.NS", "qty": 10}]},
                          prices={"INFY.NS": 1500.0}, state=state, today="2026-11-12",
                          now=datetime.datetime(2026, 11, 12, 5, 0))["results"][0]
        self.assertEqual((out["kind"], out["verdict"]), ("exit", "PASS"), out)
        doc = preflight.run_checks(datetime.datetime(2026, 11, 12, 3, 0), self.config(), None, None, None, None, None,
                                   None, "trader/state")
        self.assertIn("stop switch: stop switch: trader/STOP is there (a bad week)", " ".join(doc["no_go"]))
        self.assertEqual(run("resume").returncode, 0)
        self.assertEqual(self.verdict(self.ORDER)["verdict"], "PASS")


class Mcp(Folder):
    def talk(self, *msgs):
        env = dict(os.environ, TRADER_HOME=self.d)
        p = subprocess.run([sys.executable, os.path.join(CORE, "mcp_server.py")], capture_output=True, text=True,
                           input="".join(json.dumps(m) + "\n" for m in msgs), env=env, timeout=120)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def test_the_handshake_the_tools_and_no_order_tool(self):
        self.config()
        out = self.talk({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "status", "arguments": {}}},
                        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "place_order", "arguments": {}}},
                        {"jsonrpc": "2.0", "id": 5, "method": "nothing"})
        self.assertEqual([m["id"] for m in out], [1, 2, 3, 4, 5])                 # no reply to the notification
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in out[1]["result"]["tools"]]
        self.assertEqual(names, ["doctor", "status", "forward_tests", "journal", "daily_report", "schedule", "stop"])
        for t in out[1]["result"]["tools"]:
            self.assertNotRegex(t["name"], "order|approve|place|resume")       # nothing that trades or unlocks
            self.assertEqual(t["annotations"]["readOnlyHint"], t["name"] != "stop")
        self.assertIn("Stop switch: off", out[2]["result"]["content"][0]["text"])
        self.assertEqual(out[3]["error"]["code"], -32602)
        self.assertEqual(out[4]["error"]["code"], -32601)

    def test_stop_from_claude_and_the_doctor(self):
        self.config()
        out = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "stop", "arguments": {"reason": "user said stop"}}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "doctor"}})
        self.assertIn("Stop switch ON", out[0]["result"]["content"][0]["text"])
        with open("trader/STOP") as fh:
            self.assertEqual(fh.read().strip(), "user said stop")
        self.assertIn("stop switch", out[1]["result"]["content"][0]["text"])

    def test_an_old_protocol_is_answered_in_kind(self):
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(reply["result"]["protocolVersion"], mcp_server.PROTOCOLS[0])


class Command(Folder):
    def test_one_front_door(self):
        r = run("help")
        for cmd in ("doctor", "migrate", "stop", "resume", "mcp", "check", "autopilot", "daily"):
            self.assertIn("  " + cmd, r.stdout)
        self.assertEqual(run("nothing-here").returncode, 2)
        self.assertRegex(run("version").stdout, r"day-trader-skill-india \d+\.\d+\.\d+, code [0-9a-f]{10}")
        # a tool runs in TRADER_HOME with its own options
        r = run("schedule", "plan", "--now", "2026-11-14 10:00", env={"TRADER_HOME": self.d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-11-16 08:40", r.stdout)                             # a Saturday: Monday's brief next

    def test_setup_then_doctor(self):
        self.assertEqual(run("setup").returncode, 0)
        with open("trader/config.json") as fh:
            self.assertEqual(json.load(fh)["config_version"], doctor.CONFIG_VERSION)
        r = run("doctor", "--today", "2026-11-12", "--json")
        got = {x["check"]: x["level"] for x in json.loads(r.stdout)}
        self.assertEqual(got["config format"], "OK")

    def test_standalone_skill_zips(self):
        r = run("package", "trader-risk", "--out", self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(os.path.join(self.d, "trader-risk.zip")) as z:
            z.extractall(self.d)
        names = sorted(os.listdir(os.path.join(self.d, "trader-risk")))
        self.assertTrue({"SKILL.md", "check.py", "tradercore.py", "nse.json", "brokers"} <= set(names), names)
        self.assertNotIn("__pycache__", names)
        # the zip runs on its own, away from the repo
        r = subprocess.run([sys.executable, "-c", "import check; print(check.CHARGES['brokerage_max'])"],
                           cwd=os.path.join(self.d, "trader-risk"), capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "20.0", r.stderr)


if __name__ == "__main__":
    unittest.main()
