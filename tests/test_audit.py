"""trader-broker's audit.py (the trade audit page) and day.sh (the day runner). Real 5-minute and daily bars of five
Nifty 50 stocks (the edge and swing fixtures, to 6 November 2026)."""

import datetime
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

audit = load("trader-broker", "audit")
forward = load("trader-broker", "forward")
gap = load("trader-broker", "gapshadow")
archive = load("trader-timemachine", "archive")
edge = load("trader-timemachine", "edge")
FIVE = os.path.join(FIX, "trader-timemachine", "edge")
DAILY = os.path.join(FIX, "trader-timemachine", "swing")
SYMS = ("INFY.NS", "TCS.NS", "HDFCBANK.NS", "RELIANCE.NS", "SBIN.NS")


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class AuditPage(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.chdir(self.d)
        os.makedirs("trader")
        with open("trader/nifty50.txt", "w") as fh:
            fh.write("\n".join(SYMS) + "\n")
        charts = {s: edge.read_offline(FIVE, s) for s in SYMS + (edge.INDEX, edge.VIX)}
        archive.save(charts, "5m", "trader/bars", datetime.datetime(2026, 12, 1, 9, 0))
        # the ORB rule frozen on 1 October and replayed on the sessions since, as daily.py does
        quiet(forward.main, ["start", "--rule", "orb30 short tight 2R none", "--expect-r", "0.05", "--file",
                             "trader/forward.json", "--today", "2026-10-01"])
        test = forward.read("trader/forward.json", "test")
        days = sorted(d for d in edge.sessions_of(charts["INFY.NS"], "INFY.NS") if d >= "2026-10-01")
        stocks = {s: charts[s] for s in SYMS}
        test["shadow"] = {"sessions": forward.shadow_trades(test, stocks, charts[edge.INDEX], charts[edge.VIX], days)}
        forward.write("trader/forward.json", test)
        quiet(gap.main, ["start", "--file", "trader/forward-gap.json", "--today", "2026-10-01"])
        quiet(gap.main, ["shadow", "--file", "trader/forward-gap.json", "--today", "2026-11-06", "--watchlist",
                         "trader/nifty50.txt", "--offline", DAILY, "--offline-5m", FIVE])
        self.orb = [t for d in test["shadow"]["sessions"].values() for t in d]

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def test_every_trade_on_its_chart_with_its_costs(self):
        code, out, err = quiet(audit.main, ["--today", "2026-11-06"])
        self.assertEqual(code, 0, err)
        self.assertIn("trader/audit/index.html", out)
        with open("trader/audit/index.html", encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("<title>Paper Run Audit</title>", page)
        self.assertNotIn("/*AUDIT_DATA*/null", page)
        doc = json.loads(re.search(r"const DATA = (\{.*?\});\nconst KINDS", page, re.S).group(1).replace("<\\/", "</"))
        kinds = {}
        for t in doc["trades"]:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
        self.assertEqual(kinds.get("orb-shadow"), len(self.orb))
        self.assertEqual(kinds.get("gap-shadow"), 5)                # the five gap-ups since 1 October
        for t in doc["trades"]:
            times = [b[0] for b in t["bars"]]
            self.assertIn(t["entry_time"], times)
            self.assertIn(t["exit_time"], times)
            self.assertLessEqual(t["entry_time"], t["exit_time"])
            # gross less every charge is the net, to the rupee (the charges are rounded leg by leg)
            self.assertAlmostEqual(t["gross"] - t["costs"]["total"], t["net"], delta=1.0)
        s = doc["summary"]["gap-shadow"]
        self.assertEqual((s["trades"], s["avg_R"]), (5, -0.067))

    def test_no_trades_still_builds(self):
        os.remove("trader/forward-gap.json")
        test = forward.read("trader/forward.json", "test")
        test["shadow"] = {"sessions": {}}
        forward.write("trader/forward.json", test)
        code, out, err = quiet(audit.main, ["--out", "trader/audit/empty.html"])
        self.assertEqual(code, 0, err)
        self.assertIn("Audit page: 0 trades (none)", out)


class DayRunner(unittest.TestCase):
    def test_the_script_parses_and_stops_at_its_time(self):
        sh = os.path.join(ROOT, "trader-broker", "day.sh")
        self.assertEqual(subprocess.run(["bash", "-n", sh]).returncode, 0)
        d = tempfile.mkdtemp()
        try:
            # an end time already past: it takes the lock, skips the passes and says where it stopped
            r = subprocess.run([sh, "00:00"], cwd=d, capture_output=True, text=True,
                               env=dict(os.environ, CODE=os.path.join(d, "nowhere")))
            self.assertIn("day.sh: stopped at", r.stdout)
            self.assertTrue(os.path.exists(os.path.join(d, "trader", "state", "day.lock")))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
