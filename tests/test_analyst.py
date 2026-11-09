"""trader-analyst tests. fixtures/trader-analyst/example-results.json is MADE UP (Example
Industries, round numbers) so every figure below can be checked by hand."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

results = load("trader-analyst", "results")
EXAMPLE = os.path.join(FIX, "trader-analyst", "example-results.json")


def doc(**over):
    with open(EXAMPLE) as fh:
        d = json.load(fh)
    d.update(over)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = results.main(argv)
    return code, out.getvalue(), err.getvalue()


class Labels(unittest.TestCase):
    def test_indian_fiscal_quarters(self):
        d = results.datetime.date
        self.assertEqual([results.fy_label(d(2026, m, 28 if m == 2 else 30 if m in (6, 9) else 31))
                          for m in (3, 6, 9, 12)], ["Q4 FY26", "Q1 FY27", "Q2 FY27", "Q3 FY27"])


class Analyse(unittest.TestCase):
    def setUp(self):
        self.res = results.load(doc())
        self.rows, self.ttm = results.analyse(self.res)

    def test_year_on_year_and_quarter_on_quarter(self):
        last = self.rows[-1]
        self.assertEqual((last["label"], last["yoy_vs"]), ("Q2 FY27", "Q2 FY26"))
        self.assertEqual(last["revenue_yoy"], Decimal("15.0"))       # 1150 / 1000
        self.assertEqual(last["revenue_qoq"], Decimal("6.5"))        # 1150 / 1080 = 1.0648
        self.assertEqual(last["pat_yoy"], Decimal("20.0"))           # 144 / 120
        self.assertEqual(last["eps_yoy"], Decimal("20.0"))           # 7.20 / 6.00
        self.assertEqual(last["ebitda_margin"], Decimal("21.0"))     # 241.5 / 1150
        self.assertEqual(last["pat_margin"], Decimal("12.5"))        # 144 / 1150 = 12.52
        self.assertIsNone(self.rows[0]["revenue_yoy"])              # no year-ago quarter in the file

    def test_ttm(self):
        self.assertEqual(self.ttm["to"], "Q2 FY27")
        self.assertEqual(self.ttm["revenue"], Decimal("4380"))      # 1050 + 1100 + 1080 + 1150
        self.assertEqual(self.ttm["pat"], Decimal("615"))           # 126 + 210 + 135 + 144
        self.assertEqual(self.ttm["eps"], Decimal("30.75"))

    def test_one_offs_are_flagged(self):
        q4 = self.rows[2]
        self.assertEqual(q4["label"], "Q4 FY26")
        self.assertIn("exceptional items of 75 crore are inside PAT", q4["notes"])
        self.assertIn("PAT is above operating profit: other income or one-offs are inside it", q4["notes"])
        self.assertEqual(self.rows[-1]["notes"], [])

    def test_gaps_and_losses(self):
        d = doc()
        del d["quarters"][1]                                         # Q3 FY26 missing
        rows, ttm = results.analyse(results.load(d))
        self.assertIsNone(ttm)
        self.assertIsNone(rows[1]["revenue_qoq"])                  # Q4 FY26 does not follow Q2 FY26
        self.assertIsNone(results.growth(Decimal("10"), Decimal("-5")))   # a swing across zero has no percent
        self.assertEqual(results.growth(Decimal("-5"), Decimal("-10")), Decimal("50.0"))

    def test_bad_files(self):
        for change, words in ((dict(basis="both"), "consolidated"), (dict(unit="dollars"), "unit"),
                              (dict(quarters=[]), "no quarters"),
                              (dict(quarters=[{"end": "2026-08-31", "revenue": 1, "pat": 1}]), "not a quarter end"),
                              (dict(quarters=[{"end": "2026-09-30", "pat": 1}]), "positive revenue"),
                              (dict(quarters=[{"end": "2026-09-30", "revenue": 1}]), "needs pat"),
                              (dict(quarters=[{"end": "2026-09-30", "revenue": "lots", "pat": 1}]), "not a number")):
            with self.assertRaises(results.ResultsError) as cm:
                results.load(doc(**change))
            self.assertIn(words, str(cm.exception), change)
        twice = doc()
        twice["quarters"].append(dict(twice["quarters"][0]))
        with self.assertRaises(results.ResultsError):
            results.load(twice)


class Cli(unittest.TestCase):
    def test_show_metrics_json_and_template(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "metrics.json")
            code, text, _ = run(["show", EXAMPLE, "--metrics", m])
            self.assertEqual(code, 0)
            self.assertIn("Example Industries (EXAMPLE.NS), consolidated results, in rupees crore", text)
            self.assertIn("Latest, Q2 FY27: revenue 1,150 crore (+15.0% on Q2 FY26), PAT 144 crore (+20.0%)", text)
            self.assertIn("Last four quarters to Q2 FY27: revenue 4,380 crore, PAT 615 crore (14.0% margin)", text)
            self.assertIn("Not financial advice", text)
            with open(m) as fh:
                row = json.load(fh)["companies"][0]
            self.assertEqual((row["ticker"], row["quarter"], row["revenue_growth_yoy"], row["pat_ttm"]),
                             ("EXAMPLE.NS", "Q2 FY27", 15.0, 615.0))
            run(["show", EXAMPLE, "--metrics", m])                      # the same company replaces its row
            with open(m) as fh:
                self.assertEqual(len(json.load(fh)["companies"]), 1)
        code, text, _ = run(["show", EXAMPLE, "--json"])
        self.assertEqual(json.loads(text)["ttm"]["revenue"], 4380.0)
        code, text, _ = run(["template", "infy"])
        self.assertEqual(json.loads(text)["symbol"], "INFY.NS")
        code, _, err = run(["show", "/nonexistent.json"])
        self.assertEqual(code, 1)
        self.assertIn("template SYMBOL", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(results.num(Decimal("1234567.5")), "12,34,567.5")


class Events(unittest.TestCase):
    def test_record_and_list(self):
        with tempfile.TemporaryDirectory() as d:
            ev = os.path.join(d, "trader", "events.json")
            code, text, err = run(["event", "infy", "2026-11-12", "--what", "Q2 FY27 results", "--events", ev])
            self.assertEqual(code, 0, err)
            self.assertIn("INFY.NS: Q2 FY27 results on 2026-11-12", text)
            run(["event", "TCS.NS", "2026-10-09", "--events", ev])
            run(["event", "INFY.NS", "2026-11-12", "--what", "Q2 FY27 results (moved)", "--events", ev])
            with open(ev) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["events"], [{"symbol": "TCS.NS", "date": "2026-10-09", "what": "results",
                                              "kind": "results"},
                                             {"symbol": "INFY.NS", "date": "2026-11-12",
                                              "what": "Q2 FY27 results (moved)", "kind": "results"}])
            code, text, _ = run(["events", "--events", ev, "--today", "2026-11-09"])
            self.assertEqual(text, "2026-11-12  INFY.NS        Q2 FY27 results (moved)\n")
            self.assertIn("TCS.NS", run(["events", "--events", ev, "--all"])[1])
            check = load("trader-risk", "check")
            self.assertEqual(check.load_events(doc)["INFY.NS"], [("2026-11-12", "Q2 FY27 results (moved)", "results")])
            code, text, _ = run(["event", "infy", "2026-11-12", "--what", "ex-dividend 21.50", "--events", ev])
            self.assertIn("INFY.NS: ex-dividend 21.50 on 2026-11-12. trader-risk blocks new entries in it that day.", text)
            code, text, _ = run(["event", "market", "2026-12-04", "--what", "RBI policy", "--events", ev])
            self.assertIn("MARKET: RBI policy on 2026-12-04. trader-risk blocks every new entry that day.", text)
            with open(ev) as fh:
                kinds = {(e["symbol"], e["what"]): e["kind"] for e in json.load(fh)["events"]}
            self.assertEqual((kinds[("INFY.NS", "ex-dividend 21.50")], kinds[("MARKET", "RBI policy")]),
                             ("ex-date", "market"))
            code, _, err = run(["event", "INFY.NS", "12-11-2026", "--events", ev])
            self.assertEqual(code, 1)
            self.assertIn("YYYY-MM-DD", err)


if __name__ == "__main__":
    unittest.main()
