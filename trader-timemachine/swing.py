#!/usr/bin/env python3
"""Swing edge report for trader-timemachine: do multi-day trades in delivery (CNC) make money after charges?

Intraday rules lose to charges: the moves are small next to them. A swing trade holds for days, its moves
are bigger, and Zerodha charges no brokerage on delivery. And daily bars go back ten years for free, so a
rule can be tested through the 2020 crash, the 2022 slide and the 2023 rally, not one quiet stretch.
Standard library only.

    python3 swing.py run --watchlist trader/nifty50.txt            # Yahoo, 10 years of daily bars
    python3 swing.py run --watchlist trader/nifty50.txt --write    # also trader/swing.json
    python3 swing.py run --offline DIR --symbols INFY.NS,TCS.NS --risk 1000 --positions 5

The grid (4 entries x 5 exits x 2 filters = 40 rules, longs only):
  entries   a signal on a day's CLOSE, bought at the NEXT day's open (nothing is known before it happened)
    breakout20   the close is above every close of the 20 days before it
    breakout55   the same over 55 days
    pullback     the close is above its 200-day average and the 2-day RSI is under 10 (a dip in an uptrend)
    oversold     the 14-day RSI was under 30 yesterday and today closed up (a bounce)
  exits     every trade also has a stop 2 x ATR(14) under the entry, set on the signal day: a day whose low
            reaches it sells at the stop, or at the open if the day gapped under it
    time5, time10, time20   sell at the close of the 5th, 10th or 20th day held
    trail10      sell at the close when it closes under the lowest low of the 10 days before (at most 60 days)
    sma5         sell at the close when it closes above its 5-day average (at most 20 days)
  filters   none, or nifty_up: Nifty 50 closed above its own 200-day average on the signal day

One trade:
  size      the most shares whose loss at the stop is --risk (1,000: 1 % of a 1,00,000 account), and no
            more than --capital / --positions (20,000) of stock. One trade per stock at a time.
  charges   Zerodha's equity DELIVERY rates (zerodha.com/charges, read 2026-11-09): no brokerage, STT 0.1 %
            on the buy and the sell, exchange 0.00307 %, SEBI 10 rupees a crore, stamp duty 0.015 % on the
            buy, 18 % GST on exchange and SEBI, and a 15.34-rupee DP charge on each sale
  R         the rupee result after charges / (shares x the planned risk)

The verdict. A rule PASSES only when all of these hold:
  both halves  the first two thirds of the years and the last third each made money after charges (30+ trades)
  most years   at least 60 % of the calendar years with trades made money
  not luck     a bootstrap over months (a month's trades move together) puts the p-value under 0.05 / 40
  beats drift  its average net return a trade beats simply buying the same stocks on any day and holding
               as long (minus the same charges). Today's Nifty 50 are the stocks that did well enough to be
               in it now (survivorship), so every long rule on them looks good; the drift has the same bias,
               and beating it is what counts.
It also says what a person would have made a year with --positions at a time, after --tax-pct (20: the
short-term capital gains rate on listed shares held under a year), against holding Nifty 50.

Data: Yahoo v8 chart, range=10y interval=1d (prices adjusted for splits, not for dividends), one request
per symbol; --offline DIR reads <SYMBOL>.json(.gz). A bar dated today (India time) is left out: the day is
not over. A past edge is not a promise.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"
HOSTS = ("query2", "query1")
PAUSE = 0.4
INDEX = "^NSEI"
ENTRIES = ("breakout20", "breakout55", "pullback", "oversold")
EXITS = ("time5", "time10", "time20", "trail10", "sma5")
FILTERS = ("none", "nifty_up")
MAX_HOLD = {"trail10": 60, "sma5": 20}
ATR_STOP = 2.0
MIN_TRADES = 30
GOOD_YEARS = 0.6
BOOTSTRAP = 1000
ALPHA = 0.05
DELIVERY = {  # Zerodha, equity delivery on the NSE: zerodha.com/charges, read 2026-11-09
    "brokerage_pct": 0.0, "stt_pct": 0.1, "exchange_pct": 0.00307, "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.015, "gst_pct": 18.0, "dp_per_sale": 15.34,
}


class SwingError(Exception):
    pass


# ---------------------------------------------------------------- charges

def delivery_charges(side, value, rates=None):
    """Charges on one delivery order of this value, each to the paisa, plus the DP charge on a sale."""
    r = dict(DELIVERY)
    r.update(rates or {})
    v = float(value)
    out = {"brokerage": round(v * r["brokerage_pct"] / 100.0, 2), "stt": round(v * r["stt_pct"] / 100.0, 2),
           "exchange": round(v * r["exchange_pct"] / 100.0, 2), "sebi": round(v * r["sebi_per_crore"] / 1e7, 2),
           "stamp": round(v * r["stamp_buy_pct"] / 100.0, 2) if side == "buy" else 0.0,
           "dp": r["dp_per_sale"] if side == "sell" else 0.0}
    out["gst"] = round((out["brokerage"] + out["exchange"] + out["sebi"]) * r["gst_pct"] / 100.0, 2)
    out["total"] = round(sum(out.values()), 2)
    return out


def round_trip(qty, buy, sell, rates=None):
    return round(delivery_charges("buy", qty * buy, rates)["total"] + delivery_charges("sell", qty * sell, rates)["total"], 2)


# ---------------------------------------------------------------- data

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch(sym):
    path = "/v8/finance/chart/%s?range=10y&interval=1d" % urllib.parse.quote(sym, safe="")
    last = "no answer"
    for host in HOSTS:
        try:
            return _get("https://%s.finance.yahoo.com%s" % (host, path))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code == 429 or e.code >= 500:
                time.sleep(5)
                continue
            raise SwingError("Yahoo answered %s for %s" % (last, sym))
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
    raise SwingError("Yahoo did not answer for %s (%s)" % (sym, last))


def read_offline(folder, sym):
    for name in (sym, sym.replace("^", "%5E")):
        for ext in (".json", ".json.gz"):
            p = os.path.join(folder, name + ext)
            if os.path.exists(p):
                if ext.endswith(".gz"):
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        return fh.read()
                with open(p, encoding="utf-8") as fh:
                    return fh.read()
    raise SwingError("no offline file for %s in %s" % (sym, folder))


def daily_bars(text, sym, today=None):
    """Chart JSON -> [(date, open, high, low, close)] in date order, IST dates, to the paisa. Days with a gap
    in the data are left out, and so is today's bar (the session is not over)."""
    try:
        res = json.loads(text)["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise SwingError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        raise SwingError("Yahoo has no data for %s" % sym)
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out = {}
    for i, t in enumerate(r.get("timestamp") or []):
        vals = [(q.get(k) or [])[i] if i < len(q.get(k) or []) else None for k in ("open", "high", "low", "close")]
        if t is None or None in vals or min(vals) <= 0:
            continue
        d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t), hours=5, minutes=30)).date().isoformat()
        if today and d >= today:
            continue
        out[d] = tuple(round(v, 2) for v in vals)
    return [(d,) + out[d] for d in sorted(out)]


# ---------------------------------------------------------------- indicators

def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(closes, n):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    trs = [None] * len(bars)
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def prepare(bars):
    closes = [b[4] for b in bars]
    return {"bars": bars, "close": closes, "sma5": sma(closes, 5), "sma200": sma(closes, 200),
            "rsi2": rsi(closes, 2), "rsi14": rsi(closes, 14), "atr": atr(bars, 14)}


def signal(p, i, entry):
    c = p["close"]
    if entry in ("breakout20", "breakout55"):
        n = 20 if entry == "breakout20" else 55
        return i >= n and c[i] > max(c[i - n:i])
    if entry == "pullback":
        return p["sma200"][i] is not None and p["rsi2"][i] is not None and c[i] > p["sma200"][i] and p["rsi2"][i] < 10
    if entry == "oversold":
        return i >= 1 and p["rsi14"][i - 1] is not None and p["rsi14"][i - 1] < 30 and c[i] > c[i - 1]
    raise SwingError("unknown entry %r" % entry)


# ---------------------------------------------------------------- the replay

def trades_for(sym, p, entry, exit_rule, cfg, regime=None):
    """Every trade the rule takes on one stock. regime {date: Nifty above its 200-day average}."""
    bars, out, i = p["bars"], [], 200
    n = len(bars)
    while i < n - 1:
        if p["atr"][i] is None or not signal(p, i, entry):
            i += 1
            continue
        k = i + 1                                  # bought at the next day's open
        e = bars[k][1]
        stop = round(e - ATR_STOP * p["atr"][i], 2)
        risk = e - stop
        if risk <= 0 or stop <= 0:
            i += 1
            continue
        qty = min(int(math.floor(cfg["risk"] / risk)), int(math.floor(cfg["max_position"] / e)))
        if qty <= 0:
            i += 1
            continue
        hold = {"time5": 5, "time10": 10, "time20": 20}.get(exit_rule, MAX_HOLD.get(exit_rule, 20))
        j, px, why = k, None, None
        while j < n:
            o, h, l, c = bars[j][1:5]
            if l <= stop:
                px, why = (min(stop, o) if j > k else stop), "stop"
                break
            held = j - k + 1
            if exit_rule == "trail10" and j >= 10 and c < min(b[3] for b in bars[j - 10:j]) and j > k:
                px, why = c, "trail"
                break
            if exit_rule == "sma5" and p["sma5"][j] is not None and c > p["sma5"][j]:
                px, why = c, "sma5"
                break
            if held >= hold:
                px, why = c, "time"
                break
            j += 1
        if px is None:
            break                                   # still open at the end of the data: not counted
        ch = round_trip(qty, e, px, cfg.get("charges"))
        pnl = round(qty * (px - e) - ch, 2)
        out.append({"symbol": sym, "signal": bars[i][0], "date": bars[k][0], "exit_date": bars[j][0],
                    "entry": e, "stop": stop, "exit": px, "why": why, "qty": qty, "days": j - k + 1,
                    "charges": ch, "pnl": pnl, "R": round(pnl / (qty * risk), 4),
                    "ret_pct": round(100.0 * pnl / (qty * e), 4),
                    "nifty_up": bool(regime and regime.get(bars[i][0]))})
        i = j + 1                                   # one trade per stock at a time
    return out


def drift(preps, days, cfg):
    """The same stocks bought at ANY day's open and sold at the close `days` later: the average net return
    in percent, after the same delivery charges on a 20,000 position. The bar every long rule must clear."""
    rets = []
    for p in preps.values():
        b = p["bars"]
        for k in range(200, len(b) - days):
            e, x = b[k][1], b[k + days - 1][4]
            qty = max(int(cfg["max_position"] // e), 1)
            rets.append(100.0 * (qty * (x - e) - round_trip(qty, e, x, cfg.get("charges"))) / (qty * e))
    return round(statistics.mean(rets), 4) if rets else None


def stats(ts):
    if not ts:
        return {"trades": 0}
    rs = [t["R"] for t in ts]
    return {"trades": len(ts), "win_pct": round(100.0 * sum(1 for t in ts if t["pnl"] > 0) / len(ts), 1),
            "avg_R": round(statistics.mean(rs), 3), "avg_ret_pct": round(statistics.mean(t["ret_pct"] for t in ts), 3),
            "avg_days": round(statistics.mean(t["days"] for t in ts), 1), "pnl": round(sum(t["pnl"] for t in ts), 2),
            "charges": round(sum(t["charges"] for t in ts), 2),
            "stops": sum(1 for t in ts if t["why"] == "stop")}


def bootstrap(ts, resamples=BOOTSTRAP, seed=7):
    """Months resampled whole: (95 % range of the average R, one-sided p that it is 0 or less)."""
    by = {}
    for t in ts:
        x = by.setdefault(t["date"][:7], [0.0, 0])
        x[0] += t["R"]
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


def swing_report(charts, index_text=None, cfg=None, today=None):
    c = {"risk": 1000.0, "capital": 100000.0, "positions": 5, "tax_pct": 20.0}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    c["max_position"] = c["capital"] / c["positions"]
    preps, errors = {}, {}
    for sym, text in sorted(charts.items()):
        try:
            b = daily_bars(text, sym, today)
            if len(b) < 260:
                raise SwingError("only %d days of data" % len(b))
            preps[sym] = prepare(b)
        except SwingError as e:
            errors[sym] = str(e)
    if not preps:
        raise SwingError("no usable charts")
    regime, nifty = {}, None
    if index_text:
        nb = daily_bars(index_text, INDEX, today)
        nifty = prepare(nb)
        for i, b in enumerate(nb):
            if nifty["sma200"][i] is not None:
                regime[b[0]] = b[4] > nifty["sma200"][i]
    all_days = sorted({b[0] for p in preps.values() for b in p["bars"][200:]})
    years = sorted({d[:4] for d in all_days})
    cut = all_days[int(len(all_days) * 2 / 3)]
    filters = [f for f in FILTERS if f != "nifty_up" or regime]
    n_rules = len(ENTRIES) * len(EXITS) * len(filters)
    bar = ALPHA / n_rules
    drift_cache = {}
    rows = []
    for entry in ENTRIES:
        for ex in EXITS:
            base = []
            for sym, p in preps.items():
                base += trades_for(sym, p, entry, ex, c, regime)
            for f in filters:
                ts = [t for t in base if f == "none" or t["nifty_up"]]
                a = stats(ts)
                h1, h2 = stats([t for t in ts if t["date"] < cut]), stats([t for t in ts if t["date"] >= cut])
                by_year = {y: stats([t for t in ts if t["date"][:4] == y]) for y in years}
                active = [v for v in by_year.values() if v["trades"]]
                good = sum(1 for v in active if v["avg_R"] > 0)
                hold = int(round(a.get("avg_days") or 5))
                if hold not in drift_cache:
                    drift_cache[hold] = drift(preps, hold, c)
                dr = drift_cache[hold]
                bs = bootstrap(ts) if a["trades"] >= 2 * MIN_TRADES and a["avg_R"] > 0 else None
                halves = (h1["trades"] >= MIN_TRADES and h2["trades"] >= MIN_TRADES and h1["avg_R"] > 0
                          and h2["avg_R"] > 0)
                most = bool(active) and good >= GOOD_YEARS * len(active)
                beats = dr is not None and a["trades"] and a["avg_ret_pct"] > dr
                passed = bool(halves and most and beats and bs and bs["p"] < bar)
                rows.append({"entry": entry, "exit": ex, "filter": f, "all": a, "first": h1, "last": h2,
                             "years_good": good, "years_active": len(active),
                             "by_year": {y: v for y, v in by_year.items() if v["trades"]},
                             "drift_pct": dr, "beats_drift": bool(beats), "halves": halves, "most_years": most,
                             "bootstrap": bs, "passed": passed})
    rows.sort(key=lambda r: -(r["all"].get("avg_R", -9) if r["all"]["trades"] >= MIN_TRADES else -9))
    nifty_cagr = None
    if nifty and len(nifty["bars"]) > 200:
        b0, b1 = nifty["bars"][200], nifty["bars"][-1]
        yrs = (datetime.date.fromisoformat(b1[0]) - datetime.date.fromisoformat(b0[0])).days / 365.25
        if yrs > 0:
            nifty_cagr = round(100.0 * ((b1[4] / b0[4]) ** (1 / yrs) - 1), 2)
    span_years = max((datetime.date.fromisoformat(all_days[-1]) - datetime.date.fromisoformat(all_days[0])).days
                     / 365.25, 0.1)
    for r in rows:
        a = r["all"]
        if not a["trades"]:
            r["per_year"] = None
            continue
        per_trade = a["pnl"] / a["trades"]
        capacity = c["positions"] * 248.0 / max(a["avg_days"], 1)
        n_year = min(a["trades"] / span_years, capacity)
        gross = per_trade * n_year
        r["per_year"] = {"trades": round(n_year, 1), "rupees": round(gross, 2),
                         "after_tax": round(gross - max(gross, 0) * c["tax_pct"] / 100.0, 2),
                         "pct": round(100.0 * (gross - max(gross, 0) * c["tax_pct"] / 100.0) / c["capital"], 2)}
    doc = {"created_by": "trader-timemachine swing.py", "symbols": len(preps), "failed": errors,
           "from": all_days[0], "to": all_days[-1], "years": years, "split": cut, "rules": n_rules,
           "significance": float("%.3g" % bar), "settings": {k: c[k] for k in ("risk", "capital", "positions",
                                                                                "max_position", "tax_pct")},
           "nifty_cagr": nifty_cagr, "rows": rows, "passed": [r for r in rows if r["passed"]]}
    doc["verdict"] = verdict(doc)
    return doc


def name(r):
    return "%s, %s exit%s" % (r["entry"], r["exit"], "" if r["filter"] == "none" else ", " + r["filter"].replace("_", " "))


def verdict(doc):
    if doc["passed"]:
        top = max(doc["passed"], key=lambda r: r["last"]["avg_R"])
        return ("%d of %d rules passed every test: both halves, %d %% of the years, the drift, and luck (p under "
                "%s). The strongest in the later years: %s, %+.2f R a trade, %+.2f %% net a trade against a drift "
                "of %+.2f %%. Evidence worth a forward test, not proof." % (
                    len(doc["passed"]), doc["rules"], int(GOOD_YEARS * 100), doc["significance"], name(top),
                    top["all"]["avg_R"], top["all"]["avg_ret_pct"], top["drift_pct"]))
    near = [r for r in doc["rows"] if r["halves"] and r["most_years"] and r["beats_drift"]]
    if near:
        top = max(near, key=lambda r: r["all"]["avg_R"])
        return ("No rule passed every test. %d made money in both halves and most years and beat the drift, but "
                "not by more than luck explains across %d rules. The best: %s, %+.2f R a trade (p %s). A lead "
                "for a forward test, not an edge." % (len(near), doc["rules"], name(top), top["all"]["avg_R"],
                                                       (top["bootstrap"] or {}).get("p")))
    return ("No rule passed: none made money after delivery charges in both halves and most years while beating "
            "simply holding the same stocks.")


def money(x):
    s = "%.0f" % abs(x)
    if len(s) > 3:
        head, groups = s[:-3], [s[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(groups)
    return ("-" if x < 0 else "") + "₹" + s


def report(doc, top=12):
    s = doc["settings"]
    L = ["Swing edge report: %d stocks, %s to %s (%d years), daily bars. Earlier %s to %s, later %s to %s."
         % (doc["symbols"], doc["from"], doc["to"], len(doc["years"]), doc["from"], doc["split"], doc["split"], doc["to"]),
         "Bought at the next open after the signal, a 2 x ATR stop, %s risked a trade, at most %s a position, "
         "delivery charges." % (money(s["risk"]), money(s["max_position"])), "",
         "%-34s %6s %6s %7s %8s %6s %7s  %s" % ("rule", "trades", "win %", "avg R", "net %", "days", "drift %",
                                              "earlier / later, good years")]
    for r in [r for r in doc["rows"] if r["all"]["trades"]][:top]:
        a = r["all"]
        L.append("%-34s %6d %6.1f %+7.3f %+8.3f %6.1f %+7.3f  %+.2f / %+.2f, %d of %d%s%s"
                 % (name(r)[:34], a["trades"], a["win_pct"], a["avg_R"], a["avg_ret_pct"], a["avg_days"],
                    r["drift_pct"] or 0, r["first"].get("avg_R", 0), r["last"].get("avg_R", 0), r["years_good"],
                    r["years_active"], ("  p=%s" % r["bootstrap"]["p"]) if r["bootstrap"] else "",
                    "  PASSED" if r["passed"] else ""))
    best = next((r for r in doc["rows"] if r["all"]["trades"] >= MIN_TRADES), None)
    if best and best.get("per_year"):
        py = best["per_year"]
        L += ["", "The best rule for one person with %d positions on %s: about %.0f trades a year, %s a year, %s "
              "after %g %% tax (%+.1f %% a year)." % (s["positions"], money(s["capital"]), py["trades"],
                                                    money(py["rupees"]), money(py["after_tax"]), s["tax_pct"], py["pct"])]
        if best["by_year"]:
            L.append("  by year (avg R): " + ", ".join("%s %+.2f" % (y, v["avg_R"]) for y, v in sorted(best["by_year"].items())))
    if doc.get("nifty_cagr") is not None:
        L.append("Holding Nifty 50 over the same years: %+.2f %% a year (before tax and the fund's small costs)."
                 % doc["nifty_cagr"])
    L += ["", doc["verdict"],
          "Today's Nifty 50 are the survivors of the last ten years, so long rules on them flatter; the drift "
          "column (buying them on any day) carries the same bias, and a rule has to beat it.",
          "A past edge is not a promise. Not financial advice."]
    if doc["failed"]:
        L.append("Left out: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    return L


def symbols_from(a):
    if a.symbols:
        return [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    if a.watchlist:
        with open(a.watchlist, encoding="utf-8") as fh:
            return [ln.split("|")[0].strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]
    raise SwingError("which stocks? --watchlist trader/nifty50.txt or --symbols INFY.NS,TCS.NS")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing edge report: multi-day delivery rules after charges.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--watchlist")
    p.add_argument("--symbols")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--risk", type=float, default=1000.0, help="rupees lost at the stop a trade (1,000)")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--positions", type=int, default=5, help="positions at a time (5)")
    p.add_argument("--tax-pct", type=float, default=20.0, help="short-term capital gains on a profit (20)")
    p.add_argument("--today", help="YYYY-MM-DD: leave out bars from this date on (default: today in India)")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--write", nargs="?", const="trader/swing.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        syms = symbols_from(a)
        charts, fails = {}, {}
        for i, s in enumerate(syms + [INDEX]):
            try:
                if a.offline:
                    charts[s] = read_offline(a.offline, s)
                else:
                    if i:
                        time.sleep(PAUSE)
                    charts[s] = fetch(s)
            except SwingError as e:
                fails[s] = str(e)
        idx = charts.pop(INDEX, None)
        today = a.today or (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
        doc = swing_report(charts, idx, {"risk": a.risk, "capital": a.capital, "positions": a.positions,
                                         "tax_pct": a.tax_pct}, today)
        doc["failed"].update({k: v for k, v in fails.items() if k != INDEX})
    except SwingError as e:
        print("swing.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        slim = dict(doc, rows=[{k: v for k, v in r.items()} for r in doc["rows"]])
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(slim, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(report(doc, a.top)))
        if a.write:
            print("Wrote %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
