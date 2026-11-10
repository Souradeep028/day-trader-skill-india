#!/usr/bin/env python3
"""Turn-of-the-month report for trader-timemachine: hold the Nifty 50 ETF only around each month's turn?

Studies of the Nifty since the 1990s find most of its return comes on the last days of one month and the
first days of the next. This tests it as a replacement for holding the index: NIFTYBEES held over the turn of
each month and cash the rest of the time, against simply holding NIFTYBEES, with Zerodha's ETF delivery
charges, whole units, and India's capital gains tax, year by year. Standard library only.

    python3 tom.py run                                # Yahoo: NIFTYBEES.NS, 10 years of daily bars
    python3 tom.py run --capital 500000 --cash-rate 5.75 --write
    python3 tom.py run --offline DIR                  # DIR/NIFTYBEES.NS.json(.gz)

The rule, fixed before any test from the literature (Lakonishok and Smidt): in for the LAST session of each
month and the FIRST 3 sessions of the next. Bought at the close of the session before (the second-last of the
month), sold at the close of the 3rd session of the new month, a slippage of --slippage-pct (0.05) each way.
A grid of 1 to 5 last sessions x 1 to 5 first sessions is shown next to it, to see whether the answer depends
on the exact window. The window needs the coming sessions, so it needs the NSE holiday list (trader-risk).

Money, the way a person would hold it:
  charges    Zerodha, equity ETF in delivery: no brokerage, STT 0.001 % on the sale only, exchange 0.00307 %,
             SEBI 10 rupees a crore, stamp duty 0.015 % on the buy, 18 % GST on exchange and SEBI, and a
             15.34-rupee DP charge on each day with a sale (zerodha.com/charges, read 2026-11-10)
  cash       out of the ETF the money sits as cash. --cash-rate (0) puts it in a liquid fund ETF instead,
             earning that rate a year, taxed at --slab-pct (30), with its own sale's DP charge each month
  tax        each round trip is a short-term gain (held under a year): 20 % on the financial year's net
             gains (April to March); a net loss is carried forward and set against later gains. Holding is
             a long-term gain, 12.5 % on the part over 1.25 lakh, paid once when it is sold at the end.
             Today's rates for every year (they were lower before July 2024): the comparison, not the
             history of the tax code.
The verdict: the turn-of-the-month rule replaces holding only if it ends with MORE money after charges and
tax, from the start AND from the middle of the data. Less risk (a smaller worst fall, a third of the time in
the market) is worth saying, not a reason on its own. A past edge is not a promise.
"""

import argparse
import datetime
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing  # noqa: E402  (Yahoo daily bars, the bad-print filter, offline files)

ETF = "NIFTYBEES.NS"
PRIMARY = (1, 3)
GRID = range(1, 6)
STCG_PCT = 20.0
LTCG_PCT = 12.5
LTCG_FREE = 125000.0
SESSIONS_A_YEAR = 248
ETF_CHARGES = {  # Zerodha, equity ETF in delivery: zerodha.com/charges, read 2026-11-10
    "stt_sell_pct": 0.001, "exchange_pct": 0.00307, "sebi_per_crore": 10.0, "stamp_buy_pct": 0.015,
    "gst_pct": 18.0, "dp_per_sale": 15.34,
}


class TomError(Exception):
    pass


def etf_charges(side, value, rates=None):
    r = dict(ETF_CHARGES)
    r.update(rates or {})
    v = float(value)
    out = {"stt": round(v * r["stt_sell_pct"] / 100.0, 2) if side == "sell" else 0.0,
           "exchange": round(v * r["exchange_pct"] / 100.0, 2), "sebi": round(v * r["sebi_per_crore"] / 1e7, 2),
           "stamp": round(v * r["stamp_buy_pct"] / 100.0, 2) if side == "buy" else 0.0,
           "dp": r["dp_per_sale"] if side == "sell" else 0.0}
    out["gst"] = round((out["exchange"] + out["sebi"]) * r["gst_pct"] / 100.0, 2)
    out["total"] = round(sum(out.values()), 2)
    return out


def fy_of(date):
    """'2026-03-31' -> 2026 (FY26: April 2025 to March 2026); '2026-04-01' -> 2027."""
    return int(date[:4]) + (1 if int(date[5:7]) >= 4 else 0)


def windows(dates, last_n, first_m):
    """[(buy_index, sell_index)]: in at the close of the session before the month's last `last_n`, out at the
    close of the next month's `first_m`-th session. Months without enough sessions on both sides are skipped."""
    months = []
    for i, d in enumerate(dates):
        if not months or months[-1][0] != d[:7]:
            months.append((d[:7], []))
        months[-1][1].append(i)
    out = []
    for (m, idx), (_, nxt) in zip(months, months[1:]):
        if len(idx) <= last_n or len(nxt) < first_m:
            continue
        out.append((idx[-last_n - 1], nxt[first_m - 1]))
    return out


def max_drawdown(values):
    peak, worst = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1) if peak else worst
    return round(100.0 * worst, 2)


def simulate(bars, rule, cfg):
    """The turn-of-the-month rule from bars[0]'s close to bars[-1]'s. -> dict with the after-tax end value."""
    dates = [b[0] for b in bars]
    closes = [b[4] for b in bars]
    slip = cfg["slippage_pct"] / 100.0
    rate = cfg["cash_rate"] / 100.0 / SESSIONS_A_YEAR
    wins = windows(dates, *rule)
    buy_at = {b: s for b, s in wins}
    cash, units, basis = cfg["capital"], 0, 0.0
    fy_gain, fy_interest, carry, tax_paid, charges_paid = {}, {}, 0.0, 0.0, 0.0
    curve, sell_on, days_in, log = [], None, 0, []

    def settle(fy):
        nonlocal carry, tax_paid, cash
        g = fy_gain.pop(fy, 0.0) + carry
        tax = max(g, 0.0) * STCG_PCT / 100.0
        carry = min(g, 0.0)
        tax += max(fy_interest.pop(fy, 0.0), 0.0) * cfg["slab_pct"] / 100.0
        cash -= tax
        tax_paid += tax

    for i, d in enumerate(dates):
        if i and fy_of(d) != fy_of(dates[i - 1]):
            settle(fy_of(dates[i - 1]))
        if units == 0 and i:
            interest = cash * rate if cash > 0 else 0.0
            cash += interest
            fy_interest[fy_of(d)] = fy_interest.get(fy_of(d), 0.0) + interest
        if units and i == sell_on:
            px = closes[i] * (1 - slip)
            ch = etf_charges("sell", units * px, cfg.get("charges"))["total"]
            proceeds = units * px - ch
            fy_gain[fy_of(d)] = fy_gain.get(fy_of(d), 0.0) + proceeds - basis
            log.append({"sold": d, "R": round(proceeds / basis - 1, 5)})
            cash += proceeds
            charges_paid += ch
            units, basis, sell_on = 0, 0.0, None
            if cfg["cash_rate"]:                          # the liquid fund is bought back: its own DP on the switch
                cash -= ETF_CHARGES["dp_per_sale"]
                charges_paid += ETF_CHARGES["dp_per_sale"]
        if units == 0 and i in buy_at and i < len(dates) - 1:
            px = closes[i] * (1 + slip)
            n = int(cash // (px * (1 + 0.0002)))
            while n > 0 and n * px + etf_charges("buy", n * px, cfg.get("charges"))["total"] > cash:
                n -= 1
            if n > 0:
                ch = etf_charges("buy", n * px, cfg.get("charges"))["total"]
                cash -= n * px + ch
                charges_paid += ch
                units, basis, sell_on = n, n * px + ch, buy_at[i]
        if units:
            days_in += 1
        curve.append(cash + units * closes[i])
    if units:                                             # the last window is still open: sold at the end
        px = closes[-1] * (1 - slip)
        ch = etf_charges("sell", units * px, cfg.get("charges"))["total"]
        fy_gain[fy_of(dates[-1])] = fy_gain.get(fy_of(dates[-1]), 0.0) + units * px - ch - basis
        cash += units * px - ch
        charges_paid += ch
    for fy in sorted(set(fy_gain) | set(fy_interest)):
        settle(fy)
    years = (datetime.date.fromisoformat(dates[-1]) - datetime.date.fromisoformat(dates[0])).days / 365.25
    return {"rule": list(rule), "end": round(cash, 2), "cagr_pct": cagr(cfg["capital"], cash, years),
            "tax": round(tax_paid, 2), "charges": round(charges_paid, 2), "trips": len(log),
            "in_market_pct": round(100.0 * days_in / len(dates), 1), "max_drawdown_pct": max_drawdown(curve)}


def hold(bars, cfg):
    """Bought at bars[0]'s close, sold at bars[-1]'s, long-term gains tax once at the end."""
    slip = cfg["slippage_pct"] / 100.0
    px = bars[0][4] * (1 + slip)
    n = int(cfg["capital"] // (px * 1.0002))
    while n > 0 and n * px + etf_charges("buy", n * px)["total"] > cfg["capital"]:
        n -= 1
    buy_ch = etf_charges("buy", n * px)["total"]
    cash = cfg["capital"] - n * px - buy_ch
    curve = [cash + n * b[4] for b in bars]
    sx = bars[-1][4] * (1 - slip)
    sell_ch = etf_charges("sell", n * sx)["total"]
    gain = n * sx - sell_ch - (n * px + buy_ch)
    tax = max(gain - LTCG_FREE, 0.0) * LTCG_PCT / 100.0
    end = cash + n * sx - sell_ch - tax
    years = (datetime.date.fromisoformat(bars[-1][0]) - datetime.date.fromisoformat(bars[0][0])).days / 365.25
    return {"end": round(end, 2), "pre_tax_end": round(end + tax, 2), "cagr_pct": cagr(cfg["capital"], end, years),
            "tax": round(tax, 2), "charges": round(buy_ch + sell_ch, 2), "max_drawdown_pct": max_drawdown(curve)}


def cagr(start, end, years):
    if years <= 0 or start <= 0 or end <= 0:
        return None
    return round(100.0 * ((end / start) ** (1 / years) - 1), 2)


def day_returns(bars, rule):
    """Close-to-close returns (%) on the sessions inside the windows, and on the rest."""
    dates, closes = [b[0] for b in bars], [b[4] for b in bars]
    inside = set()
    for b, s in windows(dates, *rule):
        inside.update(range(b + 1, s + 1))
    tin, tout = [], []
    for i in range(1, len(bars)):
        (tin if i in inside else tout).append(100.0 * (closes[i] / closes[i - 1] - 1))
    return tin, tout


def welch_t(a, b):
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    return round((statistics.mean(a) - statistics.mean(b)) / math.sqrt(va + vb), 2)


def by_year(bars, rule):
    """Calendar year -> (the rule's return before tax, holding's), with the rule's money compounding
    only on its window days and charges left out: does the effect show up year after year?"""
    tin_idx = set()
    dates, closes = [b[0] for b in bars], [b[4] for b in bars]
    for b, s in windows(dates, *rule):
        tin_idx.update(range(b + 1, s + 1))
    out = {}
    for i in range(1, len(bars)):
        y = dates[i][:4]
        r = closes[i] / closes[i - 1]
        t, h = out.get(y, (1.0, 1.0))
        out[y] = (t * (r if i in tin_idx else 1.0), h * r)
    return {y: (round(100.0 * (t - 1), 2), round(100.0 * (h - 1), 2)) for y, (t, h) in sorted(out.items())}


def tom_report(text, cfg=None, today=None):
    c = {"capital": 100000.0, "cash_rate": 0.0, "slab_pct": 30.0, "slippage_pct": 0.05}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    bars = swing.daily_bars(text, ETF, today)
    if len(bars) < 500:
        raise TomError("only %d days of %s" % (len(bars), ETF))
    first = next(i for i in range(1, len(bars)) if bars[i][0][:7] != bars[i - 1][0][:7])
    bars = bars[first:]
    mid = next(i for i in range(len(bars) // 2, len(bars)) if bars[i][0][:7] != bars[i - 1][0][:7])
    runs = {}
    for label, part in (("all", bars), ("later", bars[mid:])):
        runs[label] = {"from": part[0][0], "to": part[-1][0], "rule": simulate(part, PRIMARY, c), "hold": hold(part, c)}
    grid = {"%d+%d" % (n, m): simulate(bars, (n, m), c)["cagr_pct"] for n in GRID for m in GRID}
    tin, tout = day_returns(bars, PRIMARY)
    years = by_year(bars, PRIMARY)
    doc = {"created_by": "trader-timemachine tom.py", "etf": ETF, "from": bars[0][0], "to": bars[-1][0],
           "settings": c, "primary": "%d+%d" % PRIMARY, "runs": runs, "grid_cagr_pct": grid,
           "days": {"inside": len(tin), "outside": len(tout), "inside_avg_pct": round(statistics.mean(tin), 4),
                    "outside_avg_pct": round(statistics.mean(tout), 4), "t": welch_t(tin, tout)},
           "by_year": years, "years_rule_ahead": sum(1 for t, h in years.values() if t > h),
           "grid_beats_hold": sum(1 for v in grid.values() if v is not None and v > runs["all"]["hold"]["cagr_pct"])}
    wins = [runs[k]["rule"]["end"] > runs[k]["hold"]["end"] for k in ("all", "later")]
    doc["replaces_holding"] = all(wins)
    doc["verdict"] = verdict(doc)
    return doc


def verdict(doc):
    a, l = doc["runs"]["all"], doc["runs"]["later"]
    head = ("Turn of the month (%s) after charges and tax: %s from %s against %s holding (%+.2f %% a year against "
            "%+.2f %%); from %s: %s against %s." % (
                doc["primary"], swing.money(a["rule"]["end"]), a["from"], swing.money(a["hold"]["end"]),
                a["rule"]["cagr_pct"] or 0, a["hold"]["cagr_pct"] or 0, l["from"], swing.money(l["rule"]["end"]),
                swing.money(l["hold"]["end"])))
    if doc["replaces_holding"]:
        return head + (" It ended ahead both times, %s of the time in the market, with a worst fall of %.0f %% "
                       "against %.0f %%. Worth a forward test beside the SIP, not a switch yet." % (
                           "%.0f %%" % a["rule"]["in_market_pct"], a["rule"]["max_drawdown_pct"],
                           a["hold"]["max_drawdown_pct"]))
    return head + (" It does not replace holding: the effect is there (%+.3f %% a day inside the windows against "
                   "%+.3f %% outside, t %.1f), but out of the market the rest of the time, short-term tax and the "
                   "charges cost more than it adds. Keep the SIP." % (
                       doc["days"]["inside_avg_pct"], doc["days"]["outside_avg_pct"], doc["days"]["t"]))


def report(doc):
    s = doc["settings"]
    L = ["Turn-of-the-month report: %s, %s to %s, %s to start, cash %s." % (
        doc["etf"], doc["from"], doc["to"], swing.money(s["capital"]),
        ("in a liquid fund at %.2f %% a year (taxed at %g %%)" % (s["cash_rate"], s["slab_pct"]))
        if s["cash_rate"] else "idle"),
         "In for the last session of each month and the first 3 of the next (%s), bought and sold at the close "
         "with %.2f %% slippage, ETF delivery charges, 20 %% short-term tax a year against 12.5 %% long-term on "
         "holding." % (doc["primary"], s["slippage_pct"]), "",
         "%-26s %12s %10s %10s %10s %9s %8s" % ("", "after tax", "a year", "tax", "charges", "worst", "in mkt")]
    for label in ("all", "later"):
        run = doc["runs"][label]
        L.append("From %s:" % run["from"])
        for k, v in (("turn of the month", run["rule"]), ("holding", run["hold"])):
            L.append("  %-24s %12s %+9.2f%% %10s %10s %8.1f%% %8s" % (
                k, swing.money(v["end"]), v["cagr_pct"] or 0, swing.money(v["tax"]), swing.money(v["charges"]),
                v["max_drawdown_pct"], ("%.0f%%" % v["in_market_pct"]) if "in_market_pct" in v else "100%"))
    d = doc["days"]
    L += ["", "Days inside the windows: %d, %+.3f %% on average; outside: %d, %+.3f %% (difference t %.1f)." % (
        d["inside"], d["inside_avg_pct"], d["outside"], d["outside_avg_pct"], d["t"]),
          "Every window, %% a year after charges and tax (last sessions + first sessions; holding %+.2f %%):"
          % doc["runs"]["all"]["hold"]["cagr_pct"]]
    for n in GRID:
        L.append("  " + "  ".join("%d+%d %+6.2f" % (n, m, doc["grid_cagr_pct"]["%d+%d" % (n, m)] or 0) for m in GRID))
    L.append("  %d of %d windows beat holding." % (doc["grid_beats_hold"], len(doc["grid_cagr_pct"])))
    L.append("By year, before charges and tax (turn of the month / holding): " + ", ".join(
        "%s %+.0f/%+.0f" % (y, t, h) for y, (t, h) in doc["by_year"].items()) +
        " (%d of %d years ahead)." % (doc["years_rule_ahead"], len(doc["by_year"])))
    L += ["", doc["verdict"], "A past edge is not a promise. Not financial advice."]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Turn-of-the-month: the Nifty ETF around each month's turn vs holding.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--cash-rate", type=float, default=0.0, help="%% a year on the money out of the ETF (0: idle)")
    p.add_argument("--slab-pct", type=float, default=30.0, help="your income tax slab, for the cash's interest")
    p.add_argument("--slippage-pct", type=float, default=0.05)
    p.add_argument("--today", help="YYYY-MM-DD: leave out bars from this date on (default: today in India)")
    p.add_argument("--write", nargs="?", const="trader/tom.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    today = a.today or (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                        + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
    try:
        text = swing.read_offline(a.offline, ETF) if a.offline else swing.fetch(ETF)
        doc = tom_report(text, {"capital": a.capital, "cash_rate": a.cash_rate, "slab_pct": a.slab_pct,
                                "slippage_pct": a.slippage_pct}, today)
    except (TomError, swing.SwingError) as e:
        print("tom.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    print(json.dumps(doc, indent=2) if a.json else "\n".join(report(doc)))
    if a.write and not a.json:
        print("Wrote %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
