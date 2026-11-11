#!/usr/bin/env python3
"""Open-to-close report for trader-timemachine: intraday rules tested on TEN YEARS of daily bars.

The edge report (edge.py) has only the 60 days of 5-minute bars Yahoo keeps. But a day trade that goes in
at the open, has a fixed stop and nothing else, and is out at the close needs nothing a daily bar does not
hold: the day's low (high for a short) says whether the stop was hit, and if it was not, the trade ends at
the close. No target, so there is never a question of which came first. Ten years of daily bars give each
rule some 40 times the sessions. Standard library only.

    python3 openclose.py run --watchlist trader/nifty50.txt             # Yahoo: 10 y daily + 60 d 5-minute
    python3 openclose.py run --watchlist trader/nifty50.txt --write     # also trader/openclose.json
    python3 openclose.py run --offline DIR --offline-5m DIR5 --symbols INFY.NS,TCS.NS

The grid (10 setups x 4 stops x 5 filters = 200 rules). Everything a setup reads is known by the open:
  gap_up_fade1, gap_up_fade2    the stock opened 1 % (2 %) or more above yesterday's close: short it
  gap_up_go1, gap_up_go2        the same gap: buy it
  gap_down_fade1, gap_down_fade2  opened 1 % (2 %) or more below: buy it
  gap_down_go1, gap_down_go2    the same gap: short it
  every_long, every_short       every stock, every day: the BASELINES. They are shown, never passed: a
                                gap rule has to beat the baseline on the same side, stop and filter.
  stops     1 % or 2 % from the entry, or 0.5 or 1 x the 14-day ATR as of yesterday, on the NSE tick
  filters   none; nifty_with / nifty_against: Nifty 50 opened the trade's way (against it) from its own
            previous close; nifty_up / nifty_down: Nifty closed above (below) its 200-day average YESTERDAY

One trade, the way trader-budget and trader-broker would take it:
  entry     the day's official open (NSE's pre-open auction price), one tick worse
  stop      on the NSE tick, away from the entry; a day whose low (high, short) reaches it is out at the stop,
            one tick worse. The open is the entry, so the price cannot gap through it.
  exit      otherwise at the close, one tick worse, MINUS the close adjustment (below)
  size      trader-budget's rule: the most shares whose loss at the stop plus charges fits --max-loss (500),
            at most --max-order (75,000) of stock
  charges   Zerodha's equity intraday rates (edge.py); a short pays the STT on its sale
  R         the result after charges / (shares x the planned risk)

The open and close adjustments. NSE's official open is the price of the pre-open call auction (orders 9:00 to
9:08), and its official close the average price of the last half hour, 3:00 to 3:30. A day trade that starts
when trading opens at 9:15 gets the first trade, not the auction price, and is out by about 3:10 (Zerodha
squares MIS off from 3:20). On the days the 60-day 5-minute bars overlap the daily bars, the report measures
how far the daily open sits from the 9:15 bar's open and the daily close from the 3:05 bar's close, separately
for days that opened 1 % or more up, 1 % or more down, and the rest (on big gap days the auction price is
further out than the first trade), and charges each day's average gap AGAINST the trade, long or short.
With --auction (you place the entry in the pre-open auction, at its price) the open is not adjusted. Without
5-minute bars it charges 0.15 % at the open and the close.

The verdict. A rule PASSES only when all of these hold:
  both halves  the first two thirds of the days and the last third each averaged above 0 R (30+ trades each)
  most years   60 % or more of the calendar years with trades averaged above 0 R
  not luck     months resampled whole (a month's trades move together): one-sided p under 0.05 / 200
  baseline     it beat every_long / every_short with the same stop and filter (today's Nifty 50 survived
               the decade, which flatters longs and hurts shorts; the baseline has the same bias)
And the report re-runs the leading rules on the 60 days of 5-minute bars with a real 3:05 exit, next to the
daily version on the same days, so the daily method's own error is on the page.

Data: Yahoo v8 chart, range=10y interval=1d (adjusted for splits), and range=60d interval=5m for the check;
--offline DIR reads <SYMBOL>.json(.gz), --offline-5m DIR the 5-minute files. A past edge is not a promise.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge   # noqa: E402  (intraday charges, NSE ticks, trader-budget's sizing, 5-minute bars)
import swing  # noqa: E402  (10 years of daily bars, bad-print filter)

INDEX = "^NSEI"
GAPS = (1.0, 2.0)
SETUPS = tuple("gap_%s_%s%d" % (d, k, g) for d in ("up", "down") for k in ("fade", "go") for g in (1, 2)) + \
    ("every_long", "every_short")
BASELINES = ("every_long", "every_short")
STOPS = ("pct1", "pct2", "atr0.5", "atr1")
FILTERS = ("none", "nifty_with", "nifty_against", "nifty_up", "nifty_down")
MIN_TRADES = 30
GOOD_YEARS = 0.6
ALPHA = 0.05
BOOTSTRAP = 1000
ADJ_DEFAULT = 0.15         # % charged at the open and the close when there are no 5-minute bars to measure it
GAP_BUCKET = 1.0           # %: days that opened this far up or down are measured apart from the rest
MIN_CHECK_DAYS = 30        # fewer overlap days in a bucket than this: the all-days average is used
FLAT_SLOT = 70             # the 15:05 bar: its close is the 15:10 flat-by of the edge report
TAX_PCT = edge.TAX_PCT     # speculative business income, the slab rate
TRADES_PER_DAY = edge.TRADES_PER_DAY
SESSIONS_A_YEAR = 248


class OpenCloseError(Exception):
    pass


# ---------------------------------------------------------------- the setups

def side_of(setup):
    if setup == "every_long":
        return True
    if setup == "every_short":
        return False
    _, d, kind = setup.split("_")
    up = d == "up"
    return (not up) if kind.startswith("fade") else up


def fires(setup, gap_pct):
    if setup in BASELINES:
        return True
    _, d, kind = setup.split("_")
    g = float(kind[-1])
    return gap_pct >= g if d == "up" else gap_pct <= -g


def stop_for(entry, long, rule, atr, tick):
    if rule.startswith("pct"):
        dist = entry * float(rule[3:]) / 100.0
    else:
        if not atr:
            return None
        dist = atr * float(rule[3:])
    return edge.tick_down(entry - dist, tick) if long else edge.tick_up(entry + dist, tick)


# ---------------------------------------------------------------- one stock

def prepare(bars):
    """Per day: the gap from yesterday's close and the 14-day ATR as of yesterday (both known by the open)."""
    closes = [b[4] for b in bars]
    atr = swing.atr(bars, 14)
    days = []
    for i in range(15, len(bars)):
        d, o, h, l, c = bars[i]
        days.append({"date": d, "o": o, "h": h, "l": l, "c": c, "gap": 100.0 * (o / closes[i - 1] - 1),
                     "atr": atr[i - 1]})
    return days


def nifty_context(bars):
    """{date: (Nifty's gap at the open in %, Nifty above its 200-day average at YESTERDAY's close)}."""
    closes = [b[4] for b in bars]
    sma = swing.sma(closes, 200)
    out = {}
    for i in range(1, len(bars)):
        up = None if sma[i - 1] is None else closes[i - 1] > sma[i - 1]
        out[bars[i][0]] = (100.0 * (bars[i][1] / closes[i - 1] - 1), up)
    return out


def bucket(gap_pct):
    return "up" if gap_pct >= GAP_BUCKET else "down" if gap_pct <= -GAP_BUCKET else "flat"


def adj_for(cfg, gap_pct, which):
    """The % charged against a trade at the open or the close on a day with this gap."""
    a = cfg[which + "_adj"]
    return a.get(bucket(gap_pct), a.get("all", 0.0)) if isinstance(a, dict) else float(a)


def one_trade(day, long, stop_rule, cfg, path=None, raw_open=False, detail=None):
    """(R, pnl, ret_pct, stopped) or None. path: the day's 5-minute bars to the flat-by, for the check; with
    raw_open the entry is the price given (a 5-minute bar's open), not adjusted. detail: a dict to fill with the
    entry, stop, exit, qty, charges and (with a path) the index in it of the bar the trade ended on."""
    tick = edge.tick_for(day["o"])
    slip = cfg["slippage_ticks"] * tick
    oa = 0.0 if raw_open else adj_for(cfg, day["gap"], "open")
    entry = round(day["o"] * (1 + oa / 100.0) + slip, 2) if long else round(day["o"] * (1 - oa / 100.0) - slip, 2)
    stop = stop_for(entry, long, stop_rule, day["atr"], tick)
    if stop is None or stop <= 0:
        return None
    risk = abs(entry - stop)
    if risk < entry * edge.MIN_RISK_PCT / 100.0:
        return None
    qty = edge.size_for(entry, stop, cfg["max_loss"], cfg["max_order"], cfg.get("charges"))
    if qty < 1:
        return None
    if path is None:
        hit = day["l"] <= stop if long else day["h"] >= stop
        out = None
    else:
        hit, out, at = False, None, len(path) - 1
        for k, b in enumerate(path):
            if (b[2] <= stop) if long else (b[1] >= stop):
                hit, at = True, k
                break
        if not hit:
            out = path[-1][3]
        if detail is not None:
            detail["exit_index"] = at
    if hit:
        x = stop - slip if long else stop + slip
    else:
        c = out if out is not None else day["c"]
        adj = 0.0 if out is not None else adj_for(cfg, day["gap"], "close")
        x = round(c * (1 - adj / 100.0) - slip, 2) if long else round(c * (1 + adj / 100.0) + slip, 2)
    buy, sell = (entry, x) if long else (x, entry)
    charges = edge.round_trip(qty, buy, sell, cfg.get("charges"))
    pnl = qty * (sell - buy) - charges
    if detail is not None:
        detail.update({"entry": entry, "stop": stop, "exit": round(x, 2), "qty": qty, "charges": charges})
    return (round(pnl / (qty * risk), 4), round(pnl, 2), round(100.0 * pnl / (qty * entry), 4), hit)


def trades_for(days, setup, stop_rule, cfg, nifty):
    """[(date, R, pnl, ret_pct, stopped, filters)] for one stock. filters: the set of filters the day meets."""
    long = side_of(setup)
    out = []
    for day in days:
        if not fires(setup, day["gap"]):
            continue
        t = one_trade(day, long, stop_rule, cfg)
        if t is None:
            continue
        ng, up = nifty.get(day["date"], (None, None))
        f = {"none"}
        if ng is not None and ng != 0:
            f.add("nifty_with" if (ng > 0) == long else "nifty_against")
        if up is not None:
            f.add("nifty_up" if up else "nifty_down")
        out.append((day["date"],) + t + (frozenset(f),))
    return out


# ---------------------------------------------------------------- statistics

def stats(ts):
    if not ts:
        return {"trades": 0}
    rs = [t[1] for t in ts]
    return {"trades": len(ts), "win_pct": round(100.0 * sum(1 for t in ts if t[2] > 0) / len(ts), 1),
            "avg_R": round(statistics.mean(rs), 3), "avg_ret_pct": round(statistics.mean(t[3] for t in ts), 3),
            "pnl": round(sum(t[2] for t in ts), 2), "stops": sum(1 for t in ts if t[4])}


def bootstrap(ts, resamples=BOOTSTRAP, seed=7):
    """Months resampled whole: (95 % range of the average R, one-sided p that it is 0 or less)."""
    by = {}
    for t in ts:
        x = by.setdefault(t[0][:7], [0.0, 0])
        x[0] += t[1]
        x[1] += 1
    m = list(by.values())
    if len(m) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        tot = cnt = 0
        for _ in range(len(m)):
            x = m[rng.randrange(len(m))]
            tot += x[0]
            cnt += x[1]
        means.append(tot / cnt)
    means.sort()
    mean = sum(x[0] for x in m) / sum(x[1] for x in m)
    sd = statistics.pstdev(means)
    normal = 0.5 * math.erfc(mean / sd / math.sqrt(2)) if sd > 0 else (0.0 if mean > 0 else 1.0)
    p = max(normal, sum(1 for x in means if x <= 0) / float(resamples))
    return {"ci95": [round(means[int(0.025 * resamples)], 3), round(means[int(0.975 * resamples) - 1], 3)],
            "p": float("%.3g" % p)}


# ---------------------------------------------------------------- the data check

def data_check(daily, five):
    """How the daily bars compare with the 5-minute bars on the days both have. daily {sym: bars}, five
    {sym: {date: {slot: bar}}}. The open and close gaps are daily minus 5-minute, in %, for all days and for
    each bucket (up, down, flat: the day's gap at the open)."""
    rows, opens, closes, by = {}, [], [], {}
    for sym, sess in sorted(five.items()):
        bars_ = daily.get(sym, [])
        d = {b[0]: (b, bars_[i - 1][4] if i else None) for i, b in enumerate(bars_)}
        o_, c_, wide = [], [], 0
        for day, bars in sess.items():
            if day not in d or d[day][1] is None or 0 not in bars or FLAT_SLOT not in bars:
                continue
            D, prev = d[day]
            o_.append(100.0 * (D[1] / bars[0][0] - 1))
            c_.append(100.0 * (D[4] / bars[FLAT_SLOT][3] - 1))
            k = by.setdefault(bucket(100.0 * (D[1] / prev - 1)), ([], []))
            k[0].append(o_[-1])
            k[1].append(c_[-1])
            hi, lo = max(b[1] for b in bars.values()), min(b[2] for b in bars.values())
            wide += D[2] > hi * 1.003 or D[3] < lo * 0.997
        if not o_:
            continue
        rows[sym] = {"days": len(o_), "open_gap_pct": round(statistics.mean(o_), 3),
                     "open_abs_pct": round(statistics.median(abs(x) for x in o_), 3),
                     "close_gap_pct": round(statistics.mean(c_), 3),
                     "close_abs_pct": round(statistics.median(abs(x) for x in c_), 3), "wide_range_days": wide}
        opens += o_
        closes += c_
    if not opens:
        return None
    return {"stocks": rows, "days": len(opens), "open_gap_pct": round(statistics.mean(opens), 3),
            "close_gap_pct": round(statistics.mean(closes), 3),
            "open_abs_pct": round(statistics.median(abs(x) for x in opens), 3),
            "close_abs_pct": round(statistics.median(abs(x) for x in closes), 3),
            "buckets": {k: {"days": len(o), "open_gap_pct": round(statistics.mean(o), 3),
                            "close_gap_pct": round(statistics.mean(c), 3)} for k, (o, c) in sorted(by.items())}}


def adjustments(check, auction=False):
    """{"open": {bucket: %}, "close": {bucket: %}} charged against trades: each bucket's average gap, or the
    all-days average where a bucket has under MIN_CHECK_DAYS days, or ADJ_DEFAULT with no check at all."""
    out = {}
    for w in ("open", "close"):
        if check is None:
            a = {"all": ADJ_DEFAULT}
        else:
            a = {"all": round(abs(check[w + "_gap_pct"]), 3)}
            for k, v in check["buckets"].items():
                a[k] = round(abs(v[w + "_gap_pct"]), 3) if v["days"] >= MIN_CHECK_DAYS else a["all"]
        out[w] = {k: 0.0 for k in a} if (w == "open" and auction) else a
    return out


def recent_check(rule, preps, five, cfg, nifty):
    """The rule on the overlap days twice: from the daily bar (with the adjustments) and from the 5-minute bars,
    in at the 9:15 bar's open (the auction price with --auction) and out at a real 15:10. (trades, avg R daily,
    avg R 5-minute)."""
    long = side_of(rule["setup"])
    d_r, f_r = [], []
    for sym, days in preps.items():
        sess = five.get(sym) or {}
        for day in days:
            bars = sess.get(day["date"])
            if not bars or 0 not in bars or FLAT_SLOT not in bars or not fires(rule["setup"], day["gap"]):
                continue
            ng, up = nifty.get(day["date"], (None, None))
            f = {"none"}
            if ng:
                f.add("nifty_with" if (ng > 0) == long else "nifty_against")
            if up is not None:
                f.add("nifty_up" if up else "nifty_down")
            if rule["filter"] not in f:
                continue
            a = one_trade(day, long, rule["stop"], cfg)
            real = dict(day, o=day["o"] if cfg.get("auction") else bars[0][0])
            b = one_trade(real, long, rule["stop"], cfg, path=[bars[k] for k in sorted(bars) if k <= FLAT_SLOT],
                          raw_open=True)
            if a and b:
                d_r.append(a[0])
                f_r.append(b[0])
    if not d_r:
        return {"trades": 0}
    return {"trades": len(d_r), "daily_R": round(statistics.mean(d_r), 3), "five_min_R": round(statistics.mean(f_r), 3)}


# ---------------------------------------------------------------- the report

def openclose_report(daily_texts, index_text=None, five_texts=None, cfg=None, today=None):
    c = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "tax_pct": TAX_PCT, "close_adj": None,
         "open_adj": None, "auction": False}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    daily, errors = {}, {}
    for sym, text in sorted(daily_texts.items()):
        try:
            b = swing.daily_bars(text, sym, today)
            if len(b) < 260:
                raise OpenCloseError("only %d days of data" % len(b))
            daily[sym] = b
        except (swing.SwingError, OpenCloseError) as e:
            errors[sym] = str(e)
    if not daily:
        raise OpenCloseError("no usable charts")
    five = {}
    for sym, text in sorted((five_texts or {}).items()):
        try:
            five[sym] = edge.sessions_of(text, sym)
        except edge.EdgeError:
            pass
    check = data_check(daily, five)
    measured = adjustments(check, c["auction"])
    for w in ("open", "close"):
        if c[w + "_adj"] is None:
            c[w + "_adj"] = measured[w]
        elif not isinstance(c[w + "_adj"], dict):
            c[w + "_adj"] = {"all": float(c[w + "_adj"])}
    nifty = nifty_context(swing.daily_bars(index_text, INDEX, today)) if index_text else {}
    filters = [f for f in FILTERS if f == "none" or nifty]
    preps = {sym: prepare(b) for sym, b in daily.items()}
    all_days = sorted({d["date"] for p in preps.values() for d in p})
    years = sorted({d[:4] for d in all_days})
    cut = all_days[int(len(all_days) * 2 / 3)]
    n_rules = len(SETUPS) * len(STOPS) * len(filters)
    bar = ALPHA / n_rules
    span_years = max((datetime.date.fromisoformat(all_days[-1]) - datetime.date.fromisoformat(all_days[0])).days
                     / 365.25, 0.1)
    rows, base = [], {}
    for setup in SETUPS:
        for stop in STOPS:
            ts_all = []
            for sym, days in preps.items():
                ts_all += trades_for(days, setup, stop, c, nifty)
            for f in filters:
                ts = [t for t in ts_all if f in t[5]]
                a = stats(ts)
                h1, h2 = stats([t for t in ts if t[0] < cut]), stats([t for t in ts if t[0] >= cut])
                by_year = {y: stats([t for t in ts if t[0][:4] == y]) for y in years}
                active = {y: v for y, v in by_year.items() if v["trades"]}
                good = sum(1 for v in active.values() if v["avg_R"] > 0)
                halves = (h1["trades"] >= MIN_TRADES and h2["trades"] >= MIN_TRADES and h1["avg_R"] > 0
                          and h2["avg_R"] > 0)
                most = bool(active) and good >= GOOD_YEARS * len(active)
                bs = bootstrap(ts) if a["trades"] >= 2 * MIN_TRADES and a["avg_R"] > 0 else None
                r = {"setup": setup, "side": "long" if side_of(setup) else "short", "stop": stop, "filter": f,
                     "baseline": setup in BASELINES, "all": a, "first": h1, "last": h2, "years_good": good,
                     "years_active": len(active), "by_year": {y: v["avg_R"] for y, v in active.items()},
                     "halves": halves, "most_years": most, "bootstrap": bs}
                if setup in BASELINES:
                    base[(r["side"], stop, f)] = a.get("avg_R")
                rows.append(r)
    for r in rows:
        b = base.get((r["side"], r["stop"], r["filter"]))
        r["baseline_R"] = b
        r["beats_baseline"] = (not r["baseline"] and r["all"]["trades"] > 0 and b is not None
                               and r["all"]["avg_R"] > b)
        r["passed"] = bool(r["beats_baseline"] and r["halves"] and r["most_years"] and r["bootstrap"]
                           and r["bootstrap"]["p"] < bar)
        a = r["all"]
        if a["trades"]:
            n_year = min(a["trades"] / span_years, TRADES_PER_DAY * SESSIONS_A_YEAR)
            gross = a["pnl"] / a["trades"] * n_year
            r["per_year"] = {"trades": round(n_year, 1), "rupees": round(gross, 2),
                             "after_tax": round(gross - max(gross, 0) * c["tax_pct"] / 100.0, 2)}
        else:
            r["per_year"] = None
    rows.sort(key=lambda r: -(r["all"].get("avg_R", -9) if r["all"]["trades"] >= 2 * MIN_TRADES else -9))
    if five:
        lead = [r for r in rows if not r["baseline"] and r["all"]["trades"] >= 2 * MIN_TRADES][:5]
        for r in lead + [r for r in rows if r["passed"] and r not in lead]:
            r["recent"] = recent_check(r, preps, five, c, nifty)
    doc = {"created_by": "trader-timemachine openclose.py", "symbols": len(daily), "failed": errors,
           "from": all_days[0], "to": all_days[-1], "years": years, "split": cut, "rules": n_rules,
           "significance": float("%.3g" % bar), "data_check": check,
           "settings": {k: c[k] for k in ("max_loss", "max_order", "slippage_ticks", "tax_pct", "close_adj",
                                          "open_adj", "auction")},
           "rows": rows, "passed": [r for r in rows if r["passed"]]}
    doc["verdict"] = verdict(doc)
    return doc


def name(r):
    return "%s, %s stop%s" % (r["setup"].replace("_", " "), r["stop"],
                              "" if r["filter"] == "none" else ", " + r["filter"].replace("_", " "))


def verdict(doc):
    if doc["passed"]:
        top = max(doc["passed"], key=lambda r: r["last"]["avg_R"])
        return ("%d of %d rules passed every test: both halves, %d %% of the years, the baseline and luck (p under "
                "%s). The strongest in the later years: %s, %+.3f R a trade over %d trades. Worth a forward test, "
                "not proof." % (len(doc["passed"]), doc["rules"], int(GOOD_YEARS * 100), doc["significance"],
                                name(top), top["all"]["avg_R"], top["all"]["trades"]))
    near = [r for r in doc["rows"] if r["halves"] and r["most_years"] and r["beats_baseline"]]
    if near:
        top = max(near, key=lambda r: r["all"]["avg_R"])
        return ("No rule passed every test. %d made money in both halves and most years and beat their baseline, "
                "but not by more than luck explains across %d rules. The best: %s, %+.3f R a trade (p %s). A lead "
                "for a forward test, not an edge." % (len(near), doc["rules"], name(top), top["all"]["avg_R"],
                                                       (top["bootstrap"] or {}).get("p")))
    return ("No rule passed: none made money after intraday charges and the close adjustment in both halves and "
            "most years while beating simply trading the same side every day.")


def report(doc, top=12):
    s, ck = doc["settings"], doc["data_check"]
    L = ["Open-to-close report: %d stocks, %s to %s (%d years), daily bars. Earlier %s to %s, later %s to %s."
         % (doc["symbols"], doc["from"], doc["to"], len(doc["years"]), doc["from"], doc["split"], doc["split"],
            doc["to"]),
         "In at the open, a fixed stop, out at the close; %s max loss a trade, at most %s of stock, intraday "
         "charges, %d tick slippage each way." % (swing.money(s["max_loss"]), swing.money(s["max_order"]),
                                                    s["slippage_ticks"])]
    if ck:
        L.append("Data check on %d days with 5-minute bars too: the daily open is %+.3f %% from the 9:15 bar's open "
                 "(typically %.2f %% either way), the daily close %+.3f %% from the 3:05 bar's close (typically "
                 "%.2f %%)." % (ck["days"], ck["open_gap_pct"], ck["open_abs_pct"], ck["close_gap_pct"],
                                ck["close_abs_pct"]))
        L.append("  by the day's gap: " + "; ".join(
            "%s %d days, open %+.3f %%, close %+.3f %%" % (k, v["days"], v["open_gap_pct"], v["close_gap_pct"])
            for k, v in ck["buckets"].items()))
    else:
        L.append("No 5-minute bars to check the daily bars against: the default is charged.")
    L.append("Charged against every trade, %s: at the open %s; at the close %s."
             % ("entering in the pre-open auction" if s["auction"] else "entering at the first trade after 9:15",
                ", ".join("%s %.3f %%" % kv for kv in sorted(s["open_adj"].items())),
                ", ".join("%s %.3f %%" % kv for kv in sorted(s["close_adj"].items()))))
    L += ["", "%-44s %7s %6s %7s %8s  %s" % ("rule", "trades", "win %", "avg R", "base R",
                                             "earlier / later, good years")]
    shown = [r for r in doc["rows"] if r["all"]["trades"] and not r["baseline"]][:top]
    for r in shown:
        a = r["all"]
        L.append("%-44s %7d %6.1f %+7.3f %+8.3f  %+.3f / %+.3f, %d of %d%s%s"
                 % (name(r)[:44], a["trades"], a["win_pct"], a["avg_R"], r["baseline_R"] or 0,
                    r["first"].get("avg_R", 0), r["last"].get("avg_R", 0), r["years_good"], r["years_active"],
                    ("  p=%s" % r["bootstrap"]["p"]) if r["bootstrap"] else "", "  PASSED" if r["passed"] else ""))
    bl = [r for r in doc["rows"] if r["baseline"] and r["filter"] == "none" and r["all"]["trades"]]
    if bl:
        L += ["", "Baselines (every stock, every day, no filter): " + "; ".join(
            "%s %s %+.3f R" % (r["setup"].replace("_", " "), r["stop"], r["all"]["avg_R"])
            for r in sorted(bl, key=lambda r: (r["setup"], r["stop"])))]
    rec = [r for r in doc["rows"] if r.get("recent", {}).get("trades")]
    if rec:
        L += ["", "The same rules on the last 60 days, from the daily bar and from the 5-minute bars (in at %s, out "
              "at a real 3:10):" % ("the auction price" if s["auction"] else "the 9:15 bar's open")]
        for r in rec:
            x = r["recent"]
            L.append("  %-44s %3d trades: daily %+.3f R, 5-minute %+.3f R" % (name(r)[:44], x["trades"], x["daily_R"],
                                                                            x["five_min_R"]))
    best = next((r for r in doc["rows"] if not r["baseline"] and r["all"]["trades"] >= 2 * MIN_TRADES), None)
    if best and best.get("per_year"):
        py = best["per_year"]
        L += ["", "The top rule for one person: about %.0f trades a year, %s a year before tax, %s after %g %%."
              % (py["trades"], swing.money(py["rupees"]), swing.money(py["after_tax"]), s["tax_pct"])]
        if best["by_year"]:
            L.append("  by year (avg R): " + ", ".join("%s %+.2f" % kv for kv in sorted(best["by_year"].items())))
    L += ["", doc["verdict"],
          "The day's low or high can come after 3:10, when the trade is already out, so a few stops counted here "
          "would not have happened; and the close adjustment is an average, not each day's own gap.",
          "A past edge is not a promise. Not financial advice."]
    if doc["failed"]:
        L.append("Left out: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    return L


def _slim(doc):
    out = dict(doc)
    out["rows"] = [dict(r) for r in doc["rows"]]
    out["passed"] = [name(r) for r in doc["passed"]]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Open-to-close intraday rules on ten years of daily bars.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--watchlist")
    p.add_argument("--symbols")
    p.add_argument("--offline", metavar="DIR", help="daily bars: <SYMBOL>.json(.gz)")
    p.add_argument("--offline-5m", metavar="DIR", help="5-minute bars for the data check")
    p.add_argument("--max-loss", type=float, default=500.0)
    p.add_argument("--max-order", type=float, default=75000.0)
    p.add_argument("--slippage-ticks", type=int, default=1)
    p.add_argument("--close-adj", type=float, help="%% charged at the close on every day (default: measured)")
    p.add_argument("--open-adj", type=float, help="%% charged at the open on every day (default: measured)")
    p.add_argument("--auction", action="store_true",
                   help="you enter in NSE's pre-open auction (9:00 to 9:08) at its price: no open adjustment")
    p.add_argument("--tax-pct", type=float, default=TAX_PCT)
    p.add_argument("--today", help="YYYY-MM-DD: leave out bars from this date on (default: today in India)")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--write", nargs="?", const="trader/openclose.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        syms = swing.symbols_from(a)
        daily, five, fails = {}, {}, {}
        for i, s in enumerate(syms + [INDEX]):
            try:
                if a.offline:
                    daily[s] = swing.read_offline(a.offline, s)
                else:
                    if i:
                        time.sleep(swing.PAUSE)
                    daily[s] = swing.fetch(s)
            except swing.SwingError as e:
                fails[s] = str(e)
            if s == INDEX:
                continue
            try:
                if a.offline_5m:
                    five[s] = edge.read_offline(a.offline_5m, s)
                elif not a.offline:
                    time.sleep(edge.PAUSE)
                    five[s] = edge.fetch(s)
            except edge.EdgeError:
                pass
        idx = daily.pop(INDEX, None)
        today = a.today or (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                            + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
        doc = openclose_report(daily, idx, five, {"max_loss": a.max_loss, "max_order": a.max_order,
                                                  "slippage_ticks": a.slippage_ticks, "tax_pct": a.tax_pct,
                                                  "close_adj": a.close_adj, "open_adj": a.open_adj,
                                                  "auction": a.auction}, today)
        doc["failed"].update({k: v for k, v in fails.items() if k != INDEX})
    except (OpenCloseError, swing.SwingError) as e:
        print("openclose.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(_slim(doc), fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(_slim(doc), indent=2))
    else:
        print("\n".join(report(doc, a.top)))
        if a.write:
            print("Wrote %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
