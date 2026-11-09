#!/usr/bin/env python3
"""Factor rotation report for trader-timemachine: do monthly momentum or low-volatility portfolios beat simply
holding the market, after delivery charges and tax? Standard library only.

Short-term price patterns did not beat the index (edge.py, swing.py). Two factors have decades of evidence
across many markets, India included: momentum (stocks that rose most over the last 6 to 12 months keep
rising for a while) and low volatility (the steadiest stocks give up less in falls). Both trade once a month,
so charges stay small. NSE runs index funds on both (Nifty 200 Momentum 30, Nifty 100 Low Volatility 30).

    python3 rotation.py run --watchlist trader/nifty100.txt              # Yahoo, 10 years of daily bars
    python3 rotation.py run --watchlist trader/nifty100.txt --capital 500000 --write
    python3 rotation.py run --offline DIR --watchlist trader/nifty100.txt --json

The portfolios (12):
  mom12, mom6   rank by the return from 12 (or 6) months ago to 1 month ago (the last month is skipped: it
                tends to reverse) and hold the top 10 or 20
  lowvol        rank by the standard deviation of the last 252 daily returns, lowest first, hold 10 or 20
  each with and without a trend filter: when Nifty 50 closes the month under its 200-day average, hold cash
The benchmarks:
  equal         every stock in the list, equal weight, bought and held the same way. Today's Nifty 100 are the
                companies that grew into it (survivorship): every portfolio of them looks good, and this
                benchmark carries the same bias. Beating it is what counts.
  nifty         Nifty 50 bought and held (the price index; a fund adds the dividends, about 1 to 1.5 % a year,
                and so would the stocks here, whose prices are not adjusted for dividends either)

How it trades: the ranking uses the close of each month's last session; the trades go in at the next
session's open. Stocks that drop out of the top are sold; new ones are bought with whole shares, each at
an equal share of the portfolio (a stock dearer than its share is skipped for the next one); stocks that stay
are not resized, so turnover stays low. Zerodha's delivery charges (swing.py's) on every order.

Tax, at today's rates as if they applied throughout: gains on shares held under a year are short-term
capital gains, 20 %; over a year, long-term, 12.5 % on the year's gains above 1,25,000. Paid out of the
portfolio each April. Nifty held throughout pays long-term tax once, at the end.

The reality check: real funds. A backtest on today's index members cannot escape survivorship; a real index
fund can, because its index picked the stocks at the time, with the information of the time. --funds
(MOMENTUM.NS, Aditya Birla Sun Life's Nifty 200 Momentum 30 ETF, and LOWVOLIETF.NS, ICICI Prudential's Nifty
100 Low Volatility 30 ETF) are set against NIFTYBEES.NS (Nifty 50) over the same dates. When the backtest and
the funds disagree, believe the funds.

The verdict. A portfolio PASSES when its monthly return beats the equal-weight benchmark's on average in
both the first two thirds and the last third of the months, in at least 60 % of the years, by more than luck
explains across the 12 tried (a bootstrap of months, p under 0.05 / 12), and its after-tax yearly return
beats holding Nifty 50. A past edge is not a promise. Not financial advice.
"""

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import swing  # noqa: E402  (daily bars, delivery charges, the Yahoo reader)

INDEX = "^NSEI"
LOOKBACK = {"mom12": 252, "mom6": 126}
SKIP = 21
VOL_DAYS = 252
SIZES = (10, 20)
MIN_STOCKS = 25
FUNDS = ("MOMENTUM.NS", "LOWVOLIETF.NS")
FUND_BENCH = "NIFTYBEES.NS"
STCG_PCT, LTCG_PCT, LTCG_FREE = 20.0, 12.5, 125000.0
GOOD_YEARS = 0.6
ALPHA = 0.05
BOOTSTRAP = 2000


class RotationError(Exception):
    pass


def fy_of(day):
    d = datetime.date.fromisoformat(day)
    return d.year + 1 if d.month >= 4 else d.year         # FY27 = April 2026 to March 2027


def month_ends(days):
    out = []
    for i, d in enumerate(days):
        if i + 1 == len(days) or days[i + 1][:7] != d[:7]:
            out.append(i)
    return out


def ranking(kind, stocks, day, lookback=None):
    """[symbols] best first, among the stocks with enough history up to `day` (a closed session)."""
    scored = []
    for sym, s in stocks.items():
        i = s["idx"].get(day)
        if i is None:
            continue
        c = s["close"]
        if kind in LOOKBACK:
            n = LOOKBACK[kind]
            if i < n + SKIP:
                continue
            scored.append((-(c[i - SKIP] / c[i - SKIP - n] - 1), sym))
        elif kind == "lowvol":
            if i < VOL_DAYS:
                continue
            rets = [c[j] / c[j - 1] - 1 for j in range(i - VOL_DAYS + 1, i + 1)]
            scored.append((statistics.pstdev(rets), sym))
        elif kind == "equal":
            if i < VOL_DAYS:
                continue
            scored.append((0, sym))
        else:
            raise RotationError("unknown ranking %r" % kind)
    return [s for _, s in sorted(scored)]


def simulate(stocks, calendar, kind, size, trend, cfg, regime, tax=True):
    """Run one portfolio over the calendar. Returns its equity by day and its trade record."""
    cash = float(cfg["capital"])
    held = {}             # sym -> [qty, [(date, qty, cost)]]
    last = {}
    eq, charges, taxes, sold_value = {}, 0.0, 0.0, 0.0
    gains = {}            # FY -> {"st": x, "lt": y}
    log = []              # every order: (date, symbol, side, qty, price, charges)
    ends = set(month_ends(calendar))
    pending = None
    start = None
    for t, day in enumerate(calendar):
        for sym, s in stocks.items():
            i = s["idx"].get(day)
            if i is not None:
                last[sym] = s["bars"][i]
        if pending is not None:
            target = pending
            pending = None
            # sell what left the list, at today's open
            for sym in [x for x in held if x not in target]:
                if sym not in last or last[sym][0] != day:
                    continue                                # no trade today: try again next month
                qty, lots = held.pop(sym)
                px = last[sym][1]
                ch = swing.delivery_charges("sell", qty * px, cfg.get("charges"))["total"]
                cash += qty * px - ch
                charges += ch
                sold_value += qty * px
                log.append((day, sym, "sell", qty, px, ch))
                per = ch / qty
                for d0, q, cost in lots:
                    g = q * (px - per) - cost
                    held_days = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(d0)).days
                    bucket = gains.setdefault(fy_of(day), {"st": 0.0, "lt": 0.0})
                    bucket["lt" if held_days > 365 else "st"] += g
            # buy the new ones with whole shares, an equal share each
            value = cash + sum(h[0] * last[s_][4] for s_, h in held.items() if s_ in last)
            slot = value / float(size or max(len(target), 1))
            for sym in target:
                if sym in held or sym not in last or last[sym][0] != day:
                    continue
                px = last[sym][1]
                qty = int(min(slot, cash) // px)
                while qty > 0 and qty * px + swing.delivery_charges("buy", qty * px, cfg.get("charges"))["total"] > cash:
                    qty -= 1
                if qty <= 0:
                    continue
                ch = swing.delivery_charges("buy", qty * px, cfg.get("charges"))["total"]
                cash -= qty * px + ch
                charges += ch
                held[sym] = [qty, [(day, qty, qty * px + ch)]]
                log.append((day, sym, "buy", qty, px, ch))
        # tax for the last financial year, paid in its first April session
        if tax and t and day[5:7] == "04" and calendar[t - 1][5:7] == "03":
            g = gains.get(fy_of(calendar[t - 1]), {"st": 0.0, "lt": 0.0})
            due = max(g["st"], 0) * STCG_PCT / 100.0 + max(g["lt"] - LTCG_FREE, 0) * LTCG_PCT / 100.0
            cash -= due
            taxes += due
            if due:
                log.append((day, "", "tax", 0, 0.0, round(due, 2)))
        val = cash + sum(h[0] * last[s_][4] for s_, h in held.items() if s_ in last)
        if start is not None:
            eq[day] = val
        if t in ends:
            ranked = ranking(kind, stocks, day)
            if ranked and start is None and len(ranked) >= (size or 1):
                start = day
                eq[day] = val
            if start is not None:
                on = (not trend) or regime.get(day, True)
                pending = (ranked[:size] if size else ranked) if on else []
    unpaid = 0.0 if not tax else sum(max(g["st"], 0) * STCG_PCT / 100.0 + max(g["lt"] - LTCG_FREE, 0) * LTCG_PCT / 100.0
                 for fy, g in gains.items() if fy >= fy_of(calendar[-1]))
    return {"equity": eq, "charges": round(charges, 2), "taxes": round(taxes + unpaid, 2),
            "sold_value": round(sold_value, 2), "held_at_end": sorted(held), "start": start, "log": log,
            "cash": round(cash, 2), "held": {k: v[0] for k, v in held.items()}}


def curve_stats(eq, capital, taxes=0.0):
    days = sorted(eq)
    if len(days) < 2:
        return None
    v0, v1 = eq[days[0]], eq[days[-1]]
    yrs = (datetime.date.fromisoformat(days[-1]) - datetime.date.fromisoformat(days[0])).days / 365.25
    peak, dd = v0, 0.0
    for d in days:
        peak = max(peak, eq[d])
        dd = max(dd, 1 - eq[d] / peak)
    rets = [eq[days[i]] / eq[days[i - 1]] - 1 for i in range(1, len(days))]
    years = {}
    for d in days:
        years.setdefault(d[:4], []).append(eq[d])
    by_year, prev = {}, v0
    for y in sorted(years):
        by_year[y] = round(100.0 * (years[y][-1] / prev - 1), 2)
        prev = years[y][-1]
    after = v1 - taxes
    return {"from": days[0], "to": days[-1], "years": round(yrs, 2), "start_value": round(v0, 2),
            "end_value": round(v1, 2), "cagr": round(100.0 * ((v1 / v0) ** (1 / yrs) - 1), 2),
            "cagr_after_tax": round(100.0 * ((max(after, 1) / v0) ** (1 / yrs) - 1), 2),
            "max_drawdown": round(100.0 * dd, 2),
            "volatility": round(100.0 * statistics.pstdev(rets) * math.sqrt(248), 2), "by_year": by_year}


def monthly(eq):
    days = sorted(eq)
    out, prev = {}, None
    for i in month_ends(days):
        d = days[i]
        if prev is not None:
            out[d[:7]] = eq[d] / eq[prev] - 1
        prev = d
    return out


def bootstrap_mean(xs, resamples=BOOTSTRAP, seed=7):
    """One-sided p that the mean is 0 or less: months resampled (the larger of the share at or under 0 and
    the normal tail)."""
    if len(xs) < 12:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    mean = sum(xs) / n
    sd = statistics.pstdev(means)
    normal = 0.5 * math.erfc(mean / sd / math.sqrt(2)) if sd > 0 else (0.0 if mean > 0 else 1.0)
    return float("%.3g" % max(normal, sum(1 for m in means if m <= 0) / float(resamples)))


def rotation_report(charts, index_text, cfg=None, today=None):
    c = {"capital": 100000.0, "min_stocks": MIN_STOCKS, "sizes": SIZES}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    stocks, failed = {}, {}
    for sym, text in sorted(charts.items()):
        try:
            b = swing.daily_bars(text, sym, today)
            if len(b) < VOL_DAYS + 30:
                raise swing.SwingError("only %d days of data" % len(b))
            stocks[sym] = {"bars": b, "close": [x[4] for x in b], "idx": {x[0]: i for i, x in enumerate(b)}}
        except swing.SwingError as e:
            failed[sym] = str(e)
    if len(stocks) < c["min_stocks"]:
        raise RotationError("only %d stocks with enough history; a rotation needs a list like the Nifty 100"
                            % len(stocks))
    if not index_text:
        raise RotationError("no Nifty 50 (^NSEI) data: the benchmark and the trend filter need it")
    nb = swing.daily_bars(index_text, INDEX, today)
    calendar = [b[0] for b in nb]
    nclose = [b[4] for b in nb]
    n200 = swing.sma(nclose, 200)
    regime = {calendar[i]: (n200[i] is None or nclose[i] > n200[i]) for i in range(len(calendar))}
    runs = {}
    plans = [("equal", None, False)] + [(k, n, tr) for k in ("mom12", "mom6", "lowvol") for n in c["sizes"]
                                        for tr in (False, True)]
    for kind, size, tr in plans:
        key = kind if kind == "equal" else "%s_top%d%s" % (kind, size, "_trend" if tr else "")
        runs[key] = simulate(stocks, calendar, kind, size, tr, c, regime)
        runs[key]["pre_tax"] = simulate(stocks, calendar, kind, size, tr, c, regime, tax=False)["equity"]
    start = max(r["start"] for r in runs.values() if r["start"])
    for r in runs.values():
        for k in ("equity", "pre_tax"):
            r[k] = {d: v for d, v in r[k].items() if d >= start}
            base = r[k][min(r[k])]
            r[k] = {d: v * c["capital"] / base for d, v in r[k].items()}
    i0 = calendar.index(start)
    nifty_eq = {calendar[i]: c["capital"] * nclose[i] / nclose[i0] for i in range(i0, len(calendar))}
    n_end = nifty_eq[calendar[-1]]
    nifty_tax = max(n_end - c["capital"] - LTCG_FREE, 0) * LTCG_PCT / 100.0
    eq_month = monthly(runs["equal"]["equity"])
    months = sorted(eq_month)
    cut = months[int(len(months) * 2 / 3)]
    tried = len(runs) - 1
    bar = ALPHA / tried
    nifty_stats = curve_stats(nifty_eq, c["capital"], nifty_tax)
    rows = []
    for key, r in runs.items():
        st = curve_stats(r["equity"], c["capital"], 0.0)
        st["cagr_after_tax"] = st["cagr"]                 # taxes already came out of the portfolio each April
        st["cagr"] = curve_stats(r["pre_tax"], c["capital"], 0.0)["cagr"]
        m = monthly(r["equity"])
        ex = [m[k] - eq_month[k] for k in months if k in m]
        first = [m[k] - eq_month[k] for k in months if k in m and k < cut]
        later = [m[k] - eq_month[k] for k in months if k in m and k >= cut]
        yrs = sorted(set(st["by_year"]) & set(runs["equal"] and curve_stats(runs["equal"]["equity"], 1)["by_year"]))
        ew_years = curve_stats(runs["equal"]["equity"], 1)["by_year"]
        beat_years = sum(1 for y in yrs if st["by_year"][y] > ew_years[y])
        p = bootstrap_mean(ex) if key != "equal" else None
        avg_val = statistics.mean(r["equity"].values())
        row = {"name": key, "stats": st, "charges": r["charges"], "taxes": r["taxes"],
               "turnover_pct_year": round(100.0 * r["sold_value"] / avg_val / max(st["years"], 0.1), 1),
               "excess_month_pct": round(100.0 * statistics.mean(ex), 3) if ex else None,
               "excess_first_pct": round(100.0 * statistics.mean(first), 3) if first else None,
               "excess_later_pct": round(100.0 * statistics.mean(later), 3) if later else None,
               "years_beating_equal": beat_years, "years": len(yrs), "p": p,
               "beats_nifty_after_tax": st["cagr_after_tax"] > nifty_stats["cagr_after_tax"],
               "held_at_end": r["held_at_end"]}
        row["passed"] = bool(key != "equal" and first and later and statistics.mean(first) > 0
                             and statistics.mean(later) > 0 and beat_years >= GOOD_YEARS * len(yrs)
                             and p is not None and p < bar and row["beats_nifty_after_tax"])
        rows.append(row)
    rows.sort(key=lambda r: -r["stats"]["cagr_after_tax"])
    doc = {"created_by": "trader-timemachine rotation.py", "stocks": len(stocks), "failed": failed,
           "from": start, "to": calendar[-1], "capital": c["capital"], "tried": tried,
           "significance": float("%.3g" % bar), "split": cut, "nifty": nifty_stats, "rows": rows,
           "passed": [r["name"] for r in rows if r["passed"]]}
    doc["verdict"] = verdict(doc)
    return doc


def fund_check(fund_texts, bench_text, today=None):
    """Each real fund's yearly return and worst fall against the benchmark ETF over the same dates."""
    if not bench_text:
        return []
    bench = {b[0]: b[4] for b in swing.daily_bars(bench_text, FUND_BENCH, today)}
    out = []
    for sym, text in fund_texts.items():
        try:
            fb = [b for b in swing.daily_bars(text, sym, today) if b[0] in bench]
        except swing.SwingError:
            continue
        if len(fb) < 250:
            continue
        d0, d1 = fb[0][0], fb[-1][0]
        yrs = (datetime.date.fromisoformat(d1) - datetime.date.fromisoformat(d0)).days / 365.25

        def stats_(series):
            peak, dd = series[0], 0.0
            for v in series:
                peak = max(peak, v)
                dd = max(dd, 1 - v / peak)
            return round(100.0 * ((series[-1] / series[0]) ** (1 / yrs) - 1), 2), round(100.0 * dd, 2)
        f_cagr, f_dd = stats_([b[4] for b in fb])
        b_cagr, b_dd = stats_([bench[b[0]] for b in fb])
        out.append({"fund": sym, "from": d0, "to": d1, "years": round(yrs, 1), "cagr": f_cagr,
                    "max_drawdown": f_dd, "bench": FUND_BENCH, "bench_cagr": b_cagr, "bench_max_drawdown": b_dd})
    return out


def verdict(doc):
    n = doc["nifty"]["cagr_after_tax"]
    eq = [r for r in doc["rows"] if r["name"] == "equal"][0]
    if doc["passed"]:
        best = [r for r in doc["rows"] if r["name"] in doc["passed"]][0]
        return ("%d of %d portfolios passed: %s made %.1f %% a year after tax against %.1f %% for holding Nifty 50 "
                "and %.1f %% for the equal-weight list, beating it in both halves and %d of %d years (p %s). "
                "Evidence, not proof: the factor can go quiet for years." % (
                    len(doc["passed"]), doc["tried"], best["name"], best["stats"]["cagr_after_tax"], n,
                    eq["stats"]["cagr_after_tax"], best["years_beating_equal"], best["years"], best["p"]))
    best = [r for r in doc["rows"] if r["name"] != "equal"][0]
    return ("No portfolio passed every test. The best after tax, %s, made %.1f %% a year against %.1f %% for "
            "holding Nifty 50 and %.1f %% for the same list held equal-weight; it beat that list in %d of %d years "
            "(p %s)." % (best["name"], best["stats"]["cagr_after_tax"], n, eq["stats"]["cagr_after_tax"],
                         best["years_beating_equal"], best["years"], best["p"]))


def report(doc):
    n = doc["nifty"]
    L = ["Factor rotation: %d stocks, %s to %s (%.1f years), %s to start, monthly rebalance, delivery charges, "
         "tax paid each April." % (doc["stocks"], doc["from"], doc["to"], n["years"], swing.money(doc["capital"])),
         "", "%-20s %8s %8s %8s %8s %9s %9s  %s" % ("portfolio", "a year", "after tax", "worst", "turnover",
                                                   "vs equal", "earlier", "later, years beating equal, p")]
    for r in doc["rows"]:
        s = r["stats"]
        L.append("%-20s %+7.1f%% %+8.1f%% %7.1f%% %7.0f%% %+8.2f%% %+8.2f%%  %+.2f%%, %d of %d%s%s"
                 % (r["name"], s["cagr"], s["cagr_after_tax"], -s["max_drawdown"], r["turnover_pct_year"],
                    r["excess_month_pct"] or 0, r["excess_first_pct"] or 0, r["excess_later_pct"] or 0,
                    r["years_beating_equal"], r["years"], ("  p=%s" % r["p"]) if r["p"] is not None else "",
                    "  PASSED" if r["passed"] else ""))
    L.append("%-20s %+7.1f%% %+8.1f%% %7.1f%%  (bought once, long-term tax at the end)"
             % ("nifty (hold)", n["cagr"], n["cagr_after_tax"], -n["max_drawdown"]))
    if doc.get("funds"):
        L += ["", "Reality check, real funds (no survivorship: their index picked the stocks at the time):"]
        for f in doc["funds"]:
            L.append("  %-14s %s to %s (%.1f years): %+.1f %% a year, worst fall %.1f %%; %s %+.1f %% a year, worst %.1f %%"
                     % (f["fund"], f["from"], f["to"], f["years"], f["cagr"], -f["max_drawdown"], f["bench"],
                        f["bench_cagr"], -f["bench_max_drawdown"]))
        L.append("  When the backtest and the funds disagree, believe the funds.")
    L += ["", "\"vs equal\" is the average monthly return over the same stocks held equal-weight; \"worst\" the "
          "deepest fall from a peak.", doc["verdict"],
          "Today's Nifty 100 are the survivors of these years, so every portfolio of them flatters; the equal-weight "
          "line carries the same bias. Prices exclude dividends, so does the Nifty line. Tax at today's rates. "
          "A past edge is not a promise. Not financial advice."]
    if doc["failed"]:
        L.append("Left out: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Factor rotation: monthly momentum and low-volatility portfolios.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--watchlist")
    p.add_argument("--symbols")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--funds", default=",".join(FUNDS), help="real factor ETFs to check against %s" % FUND_BENCH)
    p.add_argument("--today", help="YYYY-MM-DD: leave out bars from this date on (default: today in India)")
    p.add_argument("--write", nargs="?", const="trader/rotation.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        syms = swing.symbols_from(a)
        charts, fails = {}, {}
        for i, s in enumerate(syms + [INDEX]):
            try:
                if a.offline:
                    charts[s] = swing.read_offline(a.offline, s)
                else:
                    if i:
                        time.sleep(swing.PAUSE)
                    charts[s] = swing.fetch(s)
            except swing.SwingError as e:
                fails[s] = str(e)
        idx = charts.pop(INDEX, None)
        today = a.today or (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
        doc = rotation_report(charts, idx, {"capital": a.capital}, today)
        doc["failed"].update({k: v for k, v in fails.items() if k != INDEX})
        funds = {}
        for f in [x.strip().upper() for x in a.funds.split(",") if x.strip()] + [FUND_BENCH]:
            try:
                funds[f] = swing.read_offline(a.offline, f) if a.offline else swing.fetch(f)
            except swing.SwingError:
                pass
        doc["funds"] = fund_check({k: v for k, v in funds.items() if k != FUND_BENCH}, funds.get(FUND_BENCH), today)
    except (RotationError, swing.SwingError) as e:
        print("rotation.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
