#!/usr/bin/env python3
"""Quarterly results calculator for trader-analyst. Standard library only, no network.

Indian companies file their quarterly results with the NSE and BSE as a PDF
(Regulation 33 of SEBI's listing rules), within 45 days of the quarter's end
(60 for the fourth quarter). No free, script-friendly feed carries those
numbers reliably, so the figures are read from the filing itself and this tool
does the arithmetic, exactly, so nothing is rounded twice or compared wrongly.

    python3 results.py show trader/results/INFY.json        # the table: growth, margins, TTM
    python3 results.py show trader/results/INFY.json --json
    python3 results.py show trader/results/INFY.json --metrics trader/metrics.json
    python3 results.py template INFY.NS > trader/results/INFY.json   # an empty file to fill in

The results file (one company, one basis, one unit):

    {"symbol": "INFY.NS", "name": "Infosys", "basis": "consolidated", "unit": "crore",
     "source": "NSE filing, Financial Results, 2026-10-16",
     "quarters": [
       {"end": "2026-09-30", "revenue": 12345.6, "ebitda": 3086.4, "operating_profit": 2716.0,
        "pat": 2100.0, "eps": 5.06, "exceptional": 0, "other_income": 410.0},
       ...]}

  end               the quarter's last day (labels it: 2026-09-30 is Q2 FY27,
                    India's financial year runs April to March)
  revenue           revenue from operations (not total income)
  ebitda            optional; banks and insurers leave it out
  operating_profit  optional: profit before other income, finance cost and tax
  pat               profit after tax attributable to the owners (the line EPS uses)
  eps               diluted EPS in rupees
  exceptional, other_income   optional, to flag a profit the business did not earn

Growth is against the same quarter a year earlier (YoY), quarter-on-quarter
(QoQ) is shown too. TTM = the last four quarters, only when all four are there
and back to back. Margins are in percent of revenue. Exit code 0 = a table.
"""

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

FIELDS = ("revenue", "ebitda", "operating_profit", "pat", "eps", "exceptional", "other_income")
BASES = ("consolidated", "standalone")
UNITS = {"crore": "crore", "cr": "crore", "lakh": "lakh", "lakhs": "lakh", "million": "million",
         "mn": "million", "rupees": "rupees"}


class ResultsError(Exception):
    pass


def D(x, what):
    if x is None:
        return None
    try:
        return Decimal(str(x).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ResultsError("%s is not a number: %r" % (what, x))


def q(x, places="0.1"):
    return x.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def fy_label(end):
    """2026-09-30 -> 'Q2 FY27'. India's financial year ends on 31 March."""
    m, y = end.month, end.year
    if m in (4, 5, 6):
        return "Q1 FY%02d" % ((y + 1) % 100)
    if m in (7, 8, 9):
        return "Q2 FY%02d" % ((y + 1) % 100)
    if m in (10, 11, 12):
        return "Q3 FY%02d" % ((y + 1) % 100)
    return "Q4 FY%02d" % (y % 100)


def load(doc):
    """Check and normalise a results document. Raises ResultsError with the reason."""
    if not isinstance(doc, dict):
        raise ResultsError("the results file is not a JSON object")
    basis = str(doc.get("basis") or "").lower()
    if basis not in BASES:
        raise ResultsError("basis must be \"consolidated\" or \"standalone\" (never mix the two)")
    unit = UNITS.get(str(doc.get("unit") or "").lower())
    if not unit:
        raise ResultsError("unit must be crore, lakh, million or rupees: the filing says which, at the top "
                           "of the table")
    rows = []
    seen = set()
    for i, r in enumerate(doc.get("quarters") or []):
        if not isinstance(r, dict) or not r.get("end"):
            raise ResultsError("quarters[%d] needs an \"end\" date" % i)
        try:
            end = datetime.date.fromisoformat(str(r["end"]))
        except ValueError:
            raise ResultsError("quarters[%d] end %r is not a date like 2026-09-30" % (i, r["end"]))
        if end.month not in (3, 6, 9, 12):
            raise ResultsError("quarters[%d] end %s is not a quarter end (March, June, September, December)"
                               % (i, end))
        if end in seen:
            raise ResultsError("the quarter ending %s is in the file twice" % end)
        seen.add(end)
        row = {"end": end, "label": fy_label(end)}
        for f in FIELDS:
            row[f] = D(r.get(f), "%s for %s" % (f, end))
        if row["revenue"] is None or row["revenue"] <= 0:
            raise ResultsError("the quarter ending %s needs a positive revenue (revenue from operations)" % end)
        if row["pat"] is None:
            raise ResultsError("the quarter ending %s needs pat (profit after tax)" % end)
        rows.append(row)
    if not rows:
        raise ResultsError("no quarters in the file")
    rows.sort(key=lambda r: r["end"])
    return {"symbol": str(doc.get("symbol") or "").upper(), "name": doc.get("name") or "", "basis": basis,
            "unit": unit, "source": doc.get("source") or "", "quarters": rows}


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


def growth(new, old):
    """Percent change, or None. A swing across zero has no meaningful percent."""
    if new is None or old is None or old == 0 or (old < 0 < new) or (new < 0 < old):
        return None
    return q((new - old) / abs(old) * 100)


def margin(part, whole):
    if part is None or whole in (None, 0):
        return None
    return q(part / whole * 100)


def analyse(res):
    rows = res["quarters"]
    by_end = {r["end"]: r for r in rows}
    out = []
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i and months_between(rows[i - 1]["end"], r["end"]) == 3 else None
        ya = next((x for e, x in by_end.items() if months_between(e, r["end"]) == 12), None)
        row = {"label": r["label"], "end": r["end"].isoformat()}
        for f in ("revenue", "ebitda", "pat", "eps"):
            row[f] = r[f]
            row[f + "_yoy"] = growth(r[f], ya[f]) if ya else None
            row[f + "_qoq"] = growth(r[f], prev[f]) if prev else None
        row["ebitda_margin"] = margin(r["ebitda"], r["revenue"])
        row["pat_margin"] = margin(r["pat"], r["revenue"])
        row["yoy_vs"] = ya["label"] if ya else None
        notes = []
        if r["exceptional"]:
            notes.append("exceptional items of %s %s are inside PAT" % (num(r["exceptional"]), res["unit"]))
        if r["operating_profit"] is not None and r["pat"] > r["operating_profit"]:
            notes.append("PAT is above operating profit: other income or one-offs are inside it")
        elif r["other_income"] is not None and r["pat"] > 0 and r["other_income"] > r["pat"] * Decimal("0.25"):
            notes.append("other income is %s%% of PAT" % q(r["other_income"] / r["pat"] * 100, "1"))
        row["notes"] = notes
        out.append(row)
    ttm = None
    last4 = rows[-4:]
    if len(last4) == 4 and all(months_between(a["end"], b["end"]) == 3 for a, b in zip(last4, last4[1:])):
        ttm = {"to": last4[-1]["label"], "revenue": sum(r["revenue"] for r in last4),
               "pat": sum(r["pat"] for r in last4)}
        eps = [r["eps"] for r in last4]
        ttm["eps"] = sum(eps) if None not in eps else None
        ttm["pat_margin"] = margin(ttm["pat"], ttm["revenue"])
    return out, ttm


def num(x):
    if x is None:
        return "-"
    s = format(q(x, "0.01"), "f")
    whole, _, frac = s.lstrip("-").partition(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(groups)
    frac = frac.rstrip("0")
    return ("-" if s.startswith("-") else "") + whole + ("." + frac if frac else "")


def pct(x):
    return "-" if x is None else "%+.1f%%" % x


def render(res, rows, ttm):
    lines = ["%s%s, %s results, in rupees %s (EPS in rupees)%s"
             % (res["name"] or res["symbol"], (" (%s)" % res["symbol"]) if res["name"] and res["symbol"] else "",
                res["basis"], res["unit"], ("; " + res["source"]) if res["source"] else "")]
    lines.append("")
    lines.append("%-9s %14s %8s %8s %12s %8s %12s %8s %8s %8s"
                 % ("quarter", "revenue", "YoY", "QoQ", "PAT", "YoY", "EBITDA", "margin", "EPS", "YoY"))
    for r in rows:
        lines.append("%-9s %14s %8s %8s %12s %8s %12s %8s %8s %8s"
                     % (r["label"], num(r["revenue"]), pct(r["revenue_yoy"]), pct(r["revenue_qoq"]),
                        num(r["pat"]), pct(r["pat_yoy"]), num(r["ebitda"]),
                        ("%.1f%%" % r["ebitda_margin"]) if r["ebitda_margin"] is not None else "-",
                        num(r["eps"]), pct(r["eps_yoy"])))
    lines.append("")
    last = rows[-1]
    lines.append("Latest, %s: revenue %s %s (%s on %s), PAT %s %s (%s), PAT margin %s."
                 % (last["label"], num(last["revenue"]), res["unit"], pct(last["revenue_yoy"]),
                    last["yoy_vs"] or "no year-ago quarter in the file", num(last["pat"]), res["unit"],
                    pct(last["pat_yoy"]), ("%.1f%%" % last["pat_margin"]) if last["pat_margin"] is not None else "-"))
    for r in rows:
        for n in r["notes"]:
            lines.append("NOTE %s: %s." % (r["label"], n))
    if ttm:
        lines.append("Last four quarters to %s: revenue %s %s, PAT %s %s (%s margin)%s."
                     % (ttm["to"], num(ttm["revenue"]), res["unit"], num(ttm["pat"]), res["unit"],
                        ("%.1f%%" % ttm["pat_margin"]) if ttm["pat_margin"] is not None else "-",
                        (", EPS %s" % num(ttm["eps"])) if ttm["eps"] is not None else ""))
    else:
        lines.append("No TTM: it needs the last four quarters, back to back.")
    lines.append("Figures as filed, %s basis. Not financial advice." % res["basis"])
    return lines


def metrics_row(res, rows, ttm):
    last = rows[-1]

    def f(x):
        return float(x) if x is not None else None
    return {"ticker": res["symbol"], "name": res["name"], "basis": res["basis"], "unit": res["unit"],
            "quarter": last["label"], "period_end": last["end"], "revenue": f(last["revenue"]),
            "revenue_growth_yoy": f(last["revenue_yoy"]), "pat": f(last["pat"]), "pat_growth_yoy": f(last["pat_yoy"]),
            "eps": f(last["eps"]), "eps_growth_yoy": f(last["eps_yoy"]), "ebitda_margin": f(last["ebitda_margin"]),
            "pat_margin": f(last["pat_margin"]), "revenue_ttm": f(ttm["revenue"]) if ttm else None,
            "pat_ttm": f(ttm["pat"]) if ttm else None, "eps_ttm": f(ttm["eps"]) if ttm else None,
            "source": res["source"] or "the company's results filing"}


def _jsonable(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, (datetime.date,)):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_jsonable(v) for v in x]
    return x


def cmd_show(a):
    try:
        with open(a.file, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise ResultsError("no results file at %s (results.py template SYMBOL makes one)" % a.file)
    except ValueError as e:
        raise ResultsError("%s is not valid JSON: %s" % (a.file, e))
    res = load(doc)
    rows, ttm = analyse(res)
    if a.metrics:
        old = {}
        if os.path.exists(a.metrics):
            with open(a.metrics, encoding="utf-8") as fh:
                old = json.load(fh) or {}
        comp = [c for c in old.get("companies", []) if c.get("ticker") != res["symbol"]]
        comp.append(metrics_row(res, rows, ttm))
        d = os.path.dirname(a.metrics)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.metrics, "w", encoding="utf-8") as fh:
            json.dump(_jsonable({"as_of": datetime.date.today().isoformat(), "companies": comp}), fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(_jsonable({"company": {k: v for k, v in res.items() if k != "quarters"},
                                    "quarters": rows, "ttm": ttm}), indent=2))
    else:
        print("\n".join(render(res, rows, ttm)))
        if a.metrics:
            print("Wrote %s." % a.metrics)
    return 0


def cmd_template(a):
    sym = a.symbol.upper()
    if not sym.endswith(".NS"):
        sym += ".NS"
    doc = {"symbol": sym, "name": "", "basis": "consolidated", "unit": "crore",
           "source": "NSE filing, Financial Results, <date filed>",
           "quarters": [{"end": "YYYY-MM-DD", "revenue": None, "ebitda": None, "operating_profit": None,
                         "pat": None, "eps": None, "exceptional": None, "other_income": None}]}
    print(json.dumps(doc, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quarterly results calculator (trader-analyst).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("show", help="growth, margins and TTM from a results file")
    p.add_argument("file")
    p.add_argument("--metrics", metavar="PATH", help="also write one row to this metrics file")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("template", help="print an empty results file")
    p.add_argument("symbol")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "template": cmd_template}[a.cmd](a)
    except ResultsError as e:
        print("results.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
