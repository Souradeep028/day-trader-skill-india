#!/usr/bin/env python3
"""Edge report for trader-timemachine: does any setup make money after charges?

The Time Machine replays ONE plan. This runs a whole grid of opening-range
breakout rules over every stock in a list and every session in the data,
sized and charged the way trader-budget would, with slippage, and says which
rules (if any) made money after charges in both the earlier and the later
sessions. Standard library only.

    python3 edge.py run --watchlist trader/nifty50.txt           # Yahoo, 60 days of 5-min bars
    python3 edge.py run --watchlist trader/nifty50.txt --write   # also save trader/edge.json
    python3 edge.py run --offline DIR --symbols INFY.NS,TCS.NS --json
    python3 edge.py run --watchlist trader/nifty50.txt --slippage-ticks 2 --max-loss 1000

The grid (16 rules, longs only):
  opening range   15 or 30 minutes from the 9:15 open
  stop            the range midpoint, or the range low
  target          1R, 1.5R, 2R or 3R
each run with no filter and with each of these, one at a time:
  nifty_up    Nifty 50 (^NSEI) is above its own 9:15 open at the entry bar's close
  stronger    the stock is up more from its 9:15 open than Nifty is, at the entry
  volume      the entry bar traded at least the opening-range bars' average volume
  no_gap      the stock opened within 1 % of the previous session's last close
  calm_vix    India VIX (^INDIAVIX) opened under the median of its last 20 sessions' closes

One trade, the way the other skills would take it:
  entry       the first 5-minute bar after the range to CLOSE above the range high,
              closing between no_entries_before (09:30) and no_entries_after (14:45),
              bought at that close plus --slippage-ticks ticks (default 1)
  stop        on the NSE tick the stock traded at that day, rounded down; refused
              when the risk is under 0.05 %
  target      entry + R x risk, on the tick
  exit        the stop first in any bar that touches both. A stop sells at the stop,
              or at the bar's open if it gapped under, minus the slippage. A target
              sells at the target. Otherwise it sells at the flat-by (15:10, the
              15:05 bar's close) minus the slippage.
  size        the most shares whose loss at the stop plus the round trip's charges
              fits --max-loss (500), and no more than --max-order (75,000) of stock
  charges     Zerodha's equity intraday rates on the NSE (see trader-budget)
  R           the trade's rupee result after charges / (shares x planned risk)

The sessions are split in time: the first two thirds (in-sample) and the last
third (out-of-sample). A rule is only "held up" when its average R after
charges is above 0 in BOTH halves with at least 30 trades in each. With 96
rule and filter combinations tested, some will look good by chance; the
report says so. A past edge is not a promise.

Data: Yahoo v8 chart, range=60d interval=5m, one request per symbol, half a
second apart; --offline DIR reads <SYMBOL>.json or .json.gz from DIR.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"
HOSTS = ("query2", "query1")
PAUSE = 0.5
BACKOFF = 5.0
OPEN_MIN = 9 * 60 + 15
INDEX = "^NSEI"
VIX = "^INDIAVIX"
OR_MINUTES = (15, 30)
STOPS = ("or_mid", "or_low")
TARGETS = (1.0, 1.5, 2.0, 3.0)
FILTERS = ("none", "nifty_up", "stronger", "volume", "no_gap", "calm_vix")
MIN_TRADES = 30
MIN_RISK_PCT = 0.05
GAP_PCT = 1.0
TICKS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))
CHARGES = {  # Zerodha, equity intraday on the NSE: zerodha.com/charges, read 2026-11-08
    "brokerage_pct": 0.03, "brokerage_max": 20.0, "stt_sell_pct": 0.025, "exchange_pct": 0.00307,
    "sebi_per_crore": 10.0, "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}


class EdgeError(Exception):
    pass


# ---------------------------------------------------------------- money and ticks (as in trader-budget / trader-exit)

def order_charges(side, value, rates=None):
    """Charges on one executed intraday order of this value (rupees), each to the paisa.
    Same function in trader-budget, trader-risk, trader-broker and timemachine.py."""
    r = dict(CHARGES)
    r.update(rates or {})
    v = float(value)
    if v <= 0:
        return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0, "gst": 0.0, "total": 0.0}
    out = {"brokerage": round(min(v * r["brokerage_pct"] / 100.0, r["brokerage_max"]), 2),
           "stt": round(v * r["stt_sell_pct"] / 100.0, 2) if side == "sell" else 0.0,
           "exchange": round(v * r["exchange_pct"] / 100.0, 2),
           "sebi": round(v * r["sebi_per_crore"] / 1e7, 2),
           "stamp": round(v * r["stamp_buy_pct"] / 100.0, 2) if side == "buy" else 0.0}
    out["gst"] = round((out["brokerage"] + out["exchange"] + out["sebi"]) * r["gst_pct"] / 100.0, 2)
    out["total"] = round(sum(out.values()), 2)
    return out


def round_trip(qty, buy, sell, rates=None):
    return round(order_charges("buy", qty * buy, rates)["total"] + order_charges("sell", qty * sell, rates)["total"], 2)


def tick_for(price):
    for below, tick in TICKS:
        if price < below:
            return tick
    return 5.00


def tick_down(x, tick):
    return round(math.floor(x / tick + 1e-6) * tick, 2)


def tick_up(x, tick):
    return round(math.ceil(x / tick - 1e-6) * tick, 2)


def tick_round(x, tick):
    return round(math.floor(x / tick + 0.5 + 1e-6) * tick, 2)


def observed_tick(prices, min_count=12):
    """The NSE tick a stock is really trading at, from its own prices: the largest tick every price is
    a multiple of. NSE sets each stock's tick in a monthly review, so a stock that just crossed 1,000 or
    5,000 can keep its old, smaller tick for weeks. None with too few prices to tell."""
    vals = [int(round(float(p) * 100)) for p in prices if p]
    if len(vals) < min_count:
        return None
    for t in (500, 100, 50, 10, 5):
        if all(v % t == 0 for v in vals):
            return t / 100.0
    return 0.01


def size_for(entry, stop, max_loss, max_order, rates=None):
    """trader-budget's max-loss rule (charges included) and its max-order cap."""
    risk = entry - stop
    if risk <= 0:
        return 0
    qty = min(int(math.floor(max_loss / risk + 1e-9)), int(math.floor(max_order / entry + 1e-9)))
    while qty > 0 and qty * risk + round_trip(qty, entry, stop, rates) > max_loss + 0.005:
        qty -= 1
    return max(qty, 0)


# ---------------------------------------------------------------- data

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(s):
    time.sleep(s)


def fetch(sym):
    path = "/v8/finance/chart/%s?range=60d&interval=5m" % urllib.parse.quote(sym, safe="")
    last = "no answer"
    for host in HOSTS:
        for attempt in (0, 1):
            try:
                return _get("https://%s.finance.yahoo.com%s" % (host, path))
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                raise EdgeError("Yahoo answered %s for %s" % (last, sym))
            except (urllib.error.URLError, OSError) as e:
                last = "could not connect: %s" % (getattr(e, "reason", e),)
                break
    raise EdgeError("Yahoo did not answer for %s (%s)" % (sym, last))


def read_offline(folder, sym):
    for name in (sym + ".json", sym + ".json.gz", sym.replace("^", "%5E") + ".json",
                 sym.replace("^", "%5E") + ".json.gz"):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            if p.endswith(".gz"):
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    return fh.read()
            with open(p, encoding="utf-8") as fh:
                return fh.read()
    raise EdgeError("no offline file for %s in %s" % (sym, folder))


def sessions_of(text, sym):
    """Chart JSON -> {date: {slot: (o, h, l, c, v)}} of IST 5-minute bars on the grid, to the paisa."""
    try:
        res = json.loads(text)["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EdgeError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        raise EdgeError("Yahoo has no data for %s" % sym)
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    days = {}
    for i, t in enumerate(r.get("timestamp") or []):
        vals = [(q.get(k) or [None] * (i + 1))[i] if i < len(q.get(k) or []) else None
                for k in ("open", "high", "low", "close", "volume")]
        if t is None or t % 300 or None in vals[:4]:
            continue
        local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t), hours=5, minutes=30)
        slot = (local.hour * 60 + local.minute - OPEN_MIN) // 5
        if not 0 <= slot < 75:
            continue
        o, h, l, c, v = vals
        days.setdefault(local.date().isoformat(), {})[slot] = (round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                                                                v or 0)
    return days


def hhmm_slot(text, what):
    """'14:45' -> the slot of the bar that CLOSES at that time."""
    try:
        h, m = [int(x) for x in str(text).split(":")]
    except ValueError:
        raise EdgeError("%s wants a time like 14:45, not %r" % (what, text))
    mins = h * 60 + m - OPEN_MIN
    if mins % 5 or not 5 <= mins <= 375:
        raise EdgeError("%s %s must be on the 5-minute grid between 9:20 and 15:30" % (what, text))
    return mins // 5 - 1


# ---------------------------------------------------------------- the replay

def context(index_days, vix_days, day, prev_day):
    """Nifty's 9:15 open on day, and whether India VIX opened calm (under its last 20 closes' median)."""
    nifty = index_days.get(day) or {}
    n_open = nifty[0][0] if 0 in nifty else None
    calm = None
    if vix_days and day in vix_days and 0 in vix_days[day]:
        before = sorted(d for d in vix_days if d < day)[-20:]
        closes = [vix_days[d][max(vix_days[d])][3] for d in before if vix_days[d]]
        if len(closes) >= 10:
            calm = vix_days[day][0][0] < statistics.median(closes)
    return nifty, n_open, calm


def trades_for(sym, days, index_days, vix_days, or_min, stop_rule, target_r, cfg):
    """Every trade the rule takes on one stock: [{date, time, entry, stop, target, exit, outcome, qty,
    R_gross, R, pnl, filters: {name: bool}}]."""
    n = or_min // 5
    first, last_entry, last = cfg["first_slot"], cfg["last_entry_slot"], cfg["flat_slot"]
    slip = cfg["slippage_ticks"]
    out = []
    dates = sorted(days)
    for di, d in enumerate(dates):
        if d < cfg.get("from", "") or d > cfg.get("to", "9999"):
            continue
        bars = days[d]
        if not all(k in bars for k in range(last + 1)):
            continue                      # a gap before the flat-by: Yahoo's data or a halt
        oh = max(bars[k][1] for k in range(n))
        ol = min(bars[k][2] for k in range(n))
        k = next((k for k in range(max(n, first), last_entry + 1) if bars[k][3] > oh), None)
        if k is None:
            continue
        close = bars[k][3]
        tick = observed_tick([x for b in bars.values() for x in b[:4]]) or tick_for(close)
        entry = tick_up(close + slip * tick, tick)
        raw_stop = (oh + ol) / 2.0 if stop_rule == "or_mid" else ol
        stop = tick_down(raw_stop, tick)
        risk = round(entry - stop, 2)
        if risk <= 0 or risk < entry * MIN_RISK_PCT / 100.0:
            continue
        target = tick_round(entry + target_r * risk, tick)
        qty = size_for(entry, stop, cfg["max_loss"], cfg["max_order"], cfg.get("charges"))
        if qty <= 0:
            continue
        outcome, exit_px = "close", None
        for j in range(k + 1, last + 1):
            o, h, l, c, v = bars[j]
            if l <= stop:
                outcome = "stop"
                exit_px = tick_down(min(stop, o) - slip * tick, tick)
                break
            if h >= target:
                outcome, exit_px = "target", target
                break
        if exit_px is None:
            exit_px = tick_down(bars[last][3] - slip * tick, tick)
        exit_px = max(exit_px, 0.01)
        charges = round_trip(qty, entry, exit_px, cfg.get("charges"))
        pnl = round(qty * (exit_px - entry) - charges, 2)
        nifty, n_open, calm = context(index_days, vix_days, d, dates[di - 1] if di else None)
        prev_close = None
        if di and days[dates[di - 1]]:
            pb = days[dates[di - 1]]
            prev_close = pb[max(pb)][3]
        s_open = bars[0][0]
        n_at = nifty.get(k, (None,) * 4)[3] if nifty else None
        vols = [bars[i][4] for i in range(n)]
        avg_v = sum(vols) / float(n) if vols else 0
        filt = {
            "none": True,
            "nifty_up": bool(n_open and n_at and n_at > n_open),
            "stronger": bool(n_open and n_at and (close / s_open - 1) > (n_at / n_open - 1)),
            "volume": bool(avg_v and bars[k][4] >= avg_v),
            "no_gap": bool(prev_close and abs(s_open / prev_close - 1) * 100 < GAP_PCT),
            "calm_vix": bool(calm),
        }
        out.append({"symbol": sym, "date": d, "time": "%d:%02d" % divmod(OPEN_MIN + 5 * (k + 1), 60),
                    "slot": k, "entry": entry, "stop": stop, "target": target, "exit": exit_px, "outcome": outcome,
                    "qty": qty, "risk": risk, "charges": charges, "pnl": pnl,
                    "R_gross": round((exit_px - entry) / risk, 4), "R": round(pnl / (qty * risk), 4),
                    "filters": filt})
    return out


def stats(trades):
    if not trades:
        return {"trades": 0}
    rs = [t["R"] for t in trades]
    wins = [t for t in trades if t["pnl"] > 0]
    gain = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["date"], x["slot"], x["symbol"])):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"trades": len(trades), "win_pct": round(100.0 * len(wins) / len(trades), 1),
            "avg_R": round(statistics.mean(rs), 3), "avg_R_gross": round(statistics.mean(t["R_gross"] for t in trades), 3),
            "pnl": round(sum(t["pnl"] for t in trades), 2), "charges": round(sum(t["charges"] for t in trades), 2),
            "profit_factor": round(gain / loss, 2) if loss else None, "max_drawdown": round(dd, 2),
            "targets": sum(1 for t in trades if t["outcome"] == "target"),
            "stops": sum(1 for t in trades if t["outcome"] == "stop"),
            "closes": sum(1 for t in trades if t["outcome"] == "close")}


def split_dates(all_dates):
    """The first two thirds of the sessions are in-sample, the last third out-of-sample."""
    ds = sorted(all_dates)
    cut = int(math.ceil(len(ds) * 2 / 3.0))
    return set(ds[:cut]), set(ds[cut:]), (ds[0], ds[cut - 1]) if ds else None, (ds[cut], ds[-1]) if len(ds) > cut else None


def edge_report(charts, index_text=None, vix_text=None, cfg=None):
    """{symbol: chart text} -> the report document."""
    c = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "no_entries_before": "09:30",
         "no_entries_after": "14:45", "flat_by": "15:10"}
    c.update(cfg or {})
    c["first_slot"] = hhmm_slot(c["no_entries_before"], "no_entries_before")
    c["last_entry_slot"] = hhmm_slot(c["no_entries_after"], "no_entries_after")
    c["flat_slot"] = hhmm_slot(c["flat_by"], "flat_by")
    if c["last_entry_slot"] >= c["flat_slot"]:
        raise EdgeError("no_entries_after must be before flat_by")
    index_days = sessions_of(index_text, INDEX) if index_text else {}
    vix_days = sessions_of(vix_text, VIX) if vix_text else {}
    per_symbol = {}
    errors = {}
    for sym, text in sorted(charts.items()):
        try:
            per_symbol[sym] = sessions_of(text, sym)
        except EdgeError as e:
            errors[sym] = str(e)
    if not per_symbol:
        raise EdgeError("no usable charts")
    dates = set()
    for days in per_symbol.values():
        dates |= {d for d, b in days.items() if all(k in b for k in range(c["flat_slot"] + 1))}
    if index_days:
        dates &= set(index_days)
    if len(dates) < 20:
        raise EdgeError("only %d full sessions in the data; the report needs at least 20" % len(dates))
    ins, oos, ins_span, oos_span = split_dates(dates)
    c["from"], c["to"] = min(dates), max(dates)
    rows = []
    for or_min in OR_MINUTES:
        for stop_rule in STOPS:
            for tr in TARGETS:
                all_trades = []
                for sym, days in per_symbol.items():
                    kept = {d: b for d, b in days.items() if d in dates or d < c["from"]}
                    all_trades += [t for t in trades_for(sym, kept, index_days, vix_days, or_min, stop_rule, tr, c)
                                   if t["date"] in dates]
                for f in FILTERS:
                    if f in ("nifty_up", "stronger") and not index_days:
                        continue
                    if f == "calm_vix" and not vix_days:
                        continue
                    ts = [t for t in all_trades if t["filters"][f]]
                    a, b = stats([t for t in ts if t["date"] in ins]), stats([t for t in ts if t["date"] in oos])
                    held = (a["trades"] >= MIN_TRADES and b["trades"] >= MIN_TRADES and a["avg_R"] > 0 and b["avg_R"] > 0)
                    rows.append({"rule": {"or_minutes": or_min, "stop": stop_rule, "target_r": tr}, "filter": f,
                                 "all": stats(ts), "in_sample": a, "out_of_sample": b, "held_up": held,
                                 "_trades": ts})
    rows.sort(key=lambda r: (-(r["all"].get("avg_R", -99) if r["all"]["trades"] >= MIN_TRADES else -99),
                             r["rule"]["or_minutes"], r["rule"]["stop"], r["rule"]["target_r"], r["filter"]))
    best = rows[0]
    base = [r for r in rows if r["filter"] == "none" and r["rule"] == {"or_minutes": 15, "stop": "or_mid",
                                                                      "target_r": 2.0}][0]

    def breakdown(r):
        by_sym, by_time = {}, {}
        for t in r["_trades"]:
            by_sym.setdefault(t["symbol"], []).append(t)
            bucket = "9:30-10:00" if t["slot"] < 9 else "10:00-11:00" if t["slot"] < 21 else "11:00-14:45"
            by_time.setdefault(bucket, []).append(t)
        syms = sorted(((s, stats(v)) for s, v in by_sym.items()), key=lambda x: -x[1]["pnl"])
        return {"by_symbol": [{"symbol": s, **v} for s, v in syms],
                "by_time": {k: stats(by_time[k]) for k in ("9:30-10:00", "10:00-11:00", "11:00-14:45") if k in by_time}}
    doc = {
        "created_by": "trader-timemachine edge.py",
        "symbols": len(per_symbol), "failed": errors, "sessions": len(dates),
        "in_sample": {"from": ins_span[0], "to": ins_span[1], "sessions": len(ins)},
        "out_of_sample": {"from": oos_span[0], "to": oos_span[1], "sessions": len(oos)} if oos_span else None,
        "settings": {k: c[k] for k in ("max_loss", "max_order", "slippage_ticks", "no_entries_before",
                                        "no_entries_after", "flat_by")},
        "combinations": len(rows),
        "held_up": [_slim(r) for r in rows if r["held_up"]],
        "best": dict(_slim(best), **breakdown(best)),
        "baseline": dict(_slim(base), **breakdown(base)),
        "rows": [_slim(r) for r in rows],
    }
    doc["verdict"] = verdict(doc)
    return doc


def _slim(r):
    return {k: v for k, v in r.items() if k != "_trades"}


def rule_name(r):
    return "%d-min range, %s stop, %gR target%s" % (
        r["rule"]["or_minutes"], "midpoint" if r["rule"]["stop"] == "or_mid" else "range-low", r["rule"]["target_r"],
        "" if r["filter"] == "none" else ", " + r["filter"].replace("_", " "))


def verdict(doc):
    held = doc["held_up"]
    if not held:
        return ("No rule made money after charges in both halves of the data with %d or more trades in each. "
                "On these %d stocks over these %d sessions, the opening-range breakout had no edge after costs."
                % (MIN_TRADES, doc["symbols"], doc["sessions"]))
    top = max(held, key=lambda r: r["out_of_sample"]["avg_R"])
    return ("%d of %d combinations made money after charges in both halves. The best out-of-sample: %s, "
            "%+.2f R a trade in-sample and %+.2f R out-of-sample. With %d combinations tested, a few will look "
            "good by chance: paper trade it before believing it."
            % (len(held), doc["combinations"], rule_name(top), top["in_sample"]["avg_R"],
               top["out_of_sample"]["avg_R"], doc["combinations"]))


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
    lines = ["Edge report: %d stocks, %d sessions (in-sample %s to %s, out-of-sample %s to %s)."
             % (doc["symbols"], doc["sessions"], doc["in_sample"]["from"], doc["in_sample"]["to"],
                doc["out_of_sample"]["from"], doc["out_of_sample"]["to"]),
             "Each trade sized to a %s max loss with charges (max order %s), %d tick%s of slippage, entries %s to %s, "
             "flat by %s." % (money(s["max_loss"]), money(s["max_order"]), s["slippage_ticks"],
                               "" if s["slippage_ticks"] == 1 else "s", s["no_entries_before"], s["no_entries_after"],
                               s["flat_by"]), ""]
    head = "%-48s %6s %6s %8s %8s %11s  %s" % ("rule", "trades", "win %", "avg R", "R gross", "P&L", "in / out of sample")
    lines.append(head)
    for r in doc["rows"][:top]:
        a, i, o = r["all"], r["in_sample"], r["out_of_sample"]
        if not a["trades"]:
            continue
        lines.append("%-48s %6d %6.1f %+8.3f %+8.3f %11s  %+.2f (%d) / %+.2f (%d)%s"
                     % (rule_name(r)[:48], a["trades"], a["win_pct"], a["avg_R"], a["avg_R_gross"], money(a["pnl"]),
                        i.get("avg_R", 0), i["trades"], o.get("avg_R", 0), o["trades"], "  held up" if r["held_up"] else ""))
    b = doc["baseline"]
    lines += ["", "The pack's default (15-min range, midpoint stop, 2R, no filter): %d trades, %.1f %% won, %+.3f R "
              "after charges (%+.3f before), %s after %s of charges." % (
                  b["all"]["trades"], b["all"].get("win_pct", 0), b["all"].get("avg_R", 0), b["all"].get("avg_R_gross", 0),
                  money(b["all"].get("pnl", 0)), money(b["all"].get("charges", 0)))]
    bt = b.get("by_time") or {}
    if bt:
        lines.append("  by entry time: " + "; ".join("%s %d trades %+.2f R" % (k, v["trades"], v["avg_R"])
                                                     for k, v in bt.items() if v["trades"]))
    lines += ["", doc["verdict"],
              "Charges are Zerodha's intraday rates; slippage is a guess, and real fills can be worse. A past edge "
              "is not a promise. Not financial advice."]
    if doc["failed"]:
        lines.append("No data for: %s." % ", ".join(sorted(doc["failed"])))
    return lines


# ---------------------------------------------------------------- command line

def symbols_from(a):
    if a.symbols:
        return [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.watchlist:
        with open(a.watchlist, encoding="utf-8") as fh:
            text = fh.read()
        if text.lstrip().startswith("{"):
            return [str(h.get("symbol")).upper() for h in json.loads(text).get("hits", []) if h.get("symbol")]
        return [ln.split("|")[0].strip().upper() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    raise EdgeError("which stocks? --watchlist trader/nifty50.txt or --symbols INFY.NS,TCS.NS")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Edge report: which opening-range rules made money after charges.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="run the grid over a list of stocks")
    p.add_argument("--watchlist", help="a text file of symbols (trader/nifty50.txt) or a watchlist.json")
    p.add_argument("--symbols", help="INFY.NS,TCS.NS")
    p.add_argument("--offline", metavar="DIR", help="read <SYMBOL>.json(.gz) from DIR instead of Yahoo")
    p.add_argument("--max-loss", type=float, default=500.0)
    p.add_argument("--max-order", type=float, default=75000.0)
    p.add_argument("--slippage-ticks", type=int, default=1)
    p.add_argument("--no-entries-before", default="09:30")
    p.add_argument("--no-entries-after", default="14:45")
    p.add_argument("--flat-by", default="15:10")
    p.add_argument("--top", type=int, default=12, help="rows to print")
    p.add_argument("--write", nargs="?", const="trader/edge.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        syms = symbols_from(a)
        charts = {}
        fails = {}
        for i, s in enumerate(syms + [INDEX, VIX]):
            try:
                if a.offline:
                    text = read_offline(a.offline, s)
                else:
                    if i:
                        _sleep(PAUSE)
                    text = fetch(s)
                charts[s] = text
            except EdgeError as e:
                fails[s] = str(e)
        idx, vix = charts.pop(INDEX, None), charts.pop(VIX, None)
        cfg = {"max_loss": a.max_loss, "max_order": a.max_order, "slippage_ticks": a.slippage_ticks,
               "no_entries_before": a.no_entries_before, "no_entries_after": a.no_entries_after, "flat_by": a.flat_by}
        doc = edge_report(charts, idx, vix, cfg)
        doc["failed"].update({k: v for k, v in fails.items() if k not in (INDEX, VIX)})
        if INDEX in fails or VIX in fails:
            doc["notes"] = ["no %s data: its filter was skipped" % s for s in (INDEX, VIX) if s in fails]
    except EdgeError as e:
        print("edge.py: %s" % e, file=sys.stderr)
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
        print("\n".join(report(doc, a.top)))
        for n in doc.get("notes", []):
            print("Note: " + n + ".")
        if a.write:
            print("Wrote %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
