#!/usr/bin/env python3
"""Edge report for trader-timemachine: does any setup make money after charges?

The Time Machine replays ONE plan. This runs a whole grid of intraday rules over every stock in a list
and every session in the data, sized and charged the way trader-budget would, with slippage, and says
which rules (if any) made money after charges in both the earlier and the later sessions, and whether
by more than luck would explain. Standard library only.

    python3 edge.py run --watchlist trader/nifty50.txt           # Yahoo, 60 days of 5-min bars
    python3 edge.py run --watchlist trader/nifty50.txt --write   # also save trader/edge.json
    python3 edge.py run --offline DIR --symbols INFY.NS,TCS.NS --json
    python3 edge.py run --watchlist trader/nifty50.txt --setups orb30 --sides short --max-cost-pct 10

The grid: 4 setups x long and short x 2 stops x 4 targets (1R, 1.5R, 2R, 3R) = 64 rules.
  orb15, orb30  the first 5-minute bar after the 15- or 30-minute opening range to CLOSE above its high
                (long) or below its low (short). Stops: the range midpoint (tight) or the far side of
                the range (wide).
  pdb           the first bar to close above the previous session's high (long) or below its low
                (short), having closed on the other side the bar before.
  vwap          a pullback to VWAP: the bar before closed above VWAP, this bar's low touched it and it
                closed back above, and above the day's open (long; mirrored for short).
  For pdb and vwap the tight stop is the entry bar's low (high for a short), the wide stop the day's
  low (high) so far.
Each rule runs with no filter, with each of these, and with low_cost plus each other one (14 in all):
  with_nifty  Nifty 50 (^NSEI) has moved the trade's way from its 9:15 open at the entry bar's close
  stronger    the stock has moved the trade's way from its open by more than Nifty has
  volume      the entry bar traded at least the average of the day's bars before it
  no_gap      the stock opened within 1 % of the previous session's last close
  calm_vix    India VIX (^INDIAVIX) opened under the median of its last 20 sessions' closes
  low_cost    the round trip's charges at the stop are at most --max-cost-pct (15) % of the planned
              risk: a wide enough stop that charges do not eat the trade (trader-budget's
              max_charges_pct_of_risk)
  not_expiry  not Nifty's weekly F&O expiry (Tuesday, or the trading day before a Tuesday holiday)

One trade, the way the other skills would take it:
  entry       the setup's bar's close, closing between no_entries_before (09:30) and no_entries_after
              (14:45), plus --slippage-ticks ticks (default 1) for a buy, minus for a short sale
  stop        on the NSE tick the stock traded at that day, rounded away from the entry; refused
              when the risk is under 0.05 %
  target      entry +/- R x risk, on the tick
  exit        the stop first in any bar that touches both. A stop fills at the stop, or at the bar's
              open if it gapped through, less the slippage. A target fills at the target. Otherwise
              out at the flat-by (15:10, the 15:05 bar's close) less the slippage.
  size        the most shares whose loss at the stop plus the round trip's charges fits --max-loss
              (500), and no more than --max-order (75,000) of stock
  charges     Zerodha's equity intraday rates on the NSE (see trader-budget); a short sale pays the
              STT on its sale at the entry and the stamp duty on its buy at the exit
  R           the trade's rupee result after charges / (shares x planned risk)

The sessions are split in time: the first two thirds (in-sample) and the last third (out-of-sample).
A rule "held up" when its average R after charges is above 0 in BOTH halves with at least 30 trades in
each. With 896 combinations tested, some will hold up by chance, so a rule only PASSES when it also
clears a session bootstrap: whole days resampled (a day's trades move together), with a one-sided
p-value under 0.05 / the number of combinations (Bonferroni). A past edge is not a promise.

Data: Yahoo v8 chart, range=60d interval=5m, one request per symbol, half a second apart; --offline
DIR reads <SYMBOL>.json or .json.gz from DIR.
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
PAUSE = 0.5
BACKOFF = 5.0
OPEN_MIN = 9 * 60 + 15
INDEX = "^NSEI"
VIX = "^INDIAVIX"
SETUPS = ("orb15", "orb30", "orb15s", "orb30s", "pdb", "vwap")
SIDES = ("long", "short")
STOPS = ("tight", "wide")
TARGETS = (1.0, 1.5, 2.0, 3.0)
SINGLE = ("none", "with_nifty", "stronger", "volume", "no_gap", "calm_vix", "low_cost", "not_expiry", "not_10_to_11")
FILTERS = SINGLE + tuple("low_cost+" + f for f in SINGLE[1:] if f != "low_cost")
SETUP_NAMES = {"orb15": "15-min range break", "orb30": "30-min range break",
               "orb15s": "15-min range stop entry", "orb30s": "30-min range stop entry",
               "pdb": "previous-day break", "vwap": "VWAP pullback"}
TAX_PCT = 30.0            # speculative business income is taxed at the slab rate: 30 % plus cess at the top
TRADES_PER_DAY = 3        # trader-budget's max_trades_per_day
CAPITAL = 100000.0
MIN_TRADES = 30
MAX_COST_PCT = 15.0
EXPIRY_WEEKDAY = 1        # Tuesday: NSE's weekly Nifty expiry since 1 September 2025
BOOTSTRAP = 1000
ALPHA = 0.05
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
    """trader-budget's max-loss rule (charges included) and its max-order cap. A stop under the entry is
    a buy; a stop over it is a short sale, bought back at the stop."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0
    buy, sell = (entry, stop) if stop < entry else (stop, entry)
    qty = min(int(math.floor(max_loss / risk + 1e-9)), int(math.floor(max_order / entry + 1e-9)))
    while qty > 0 and qty * risk + round_trip(qty, buy, sell, rates) > max_loss + 0.005:
        qty -= 1
    return max(qty, 0)


# ---------------------------------------------------------------- data

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(s):
    time.sleep(s)


def fetch(sym, rng="60d"):
    path = "/v8/finance/chart/%s?range=%s&interval=5m" % (urllib.parse.quote(sym, safe=""), rng)
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

def expiry_dates(all_dates, weekday=EXPIRY_WEEKDAY):
    """Nifty's weekly F&O expiry sessions among these trading dates: the last trading day in the seven
    days up to each expiry weekday (Tuesday on the NSE since 1 September 2025; 0 is Monday). So a
    holiday on the Tuesday moves it to the Monday, as NSE does."""
    ds = sorted(set(all_dates))
    if not ds:
        return set()
    day = datetime.date.fromisoformat(ds[0])
    day += datetime.timedelta(days=(weekday - day.weekday()) % 7)
    end = datetime.date.fromisoformat(ds[-1])     # an expiry after the data ends is not known yet
    out = set()
    while day <= end:
        lo, hi = (day - datetime.timedelta(days=7)).isoformat(), day.isoformat()
        cands = [d for d in ds if lo < d <= hi]
        if cands:
            out.add(cands[-1])
        day += datetime.timedelta(days=7)
    return out


def context(index_days, vix_days, day, prev_day=None):
    """Nifty's bars and 9:15 open on day, and whether India VIX opened calm (under its last 20 closes' median)."""
    nifty = index_days.get(day) or {}
    n_open = nifty[0][0] if 0 in nifty else None
    calm = None
    if vix_days and day in vix_days and 0 in vix_days[day]:
        before = sorted(d for d in vix_days if d < day)[-20:]
        closes = [vix_days[d][max(vix_days[d])][3] for d in before if vix_days[d]]
        if len(closes) >= 10:
            calm = vix_days[day][0][0] < statistics.median(closes)
    return nifty, n_open, calm


def prepare(days, index_days, vix_days, cfg, partial=None):
    """One stock's sessions, ready for every rule: the bars to the flat-by, the tick it traded at, VWAP,
    the previous session's high, low and close, Nifty's bars, India VIX's mood and the expiry flag.
    partial (date, n): that session is still going, with its first n bars closed; it is kept with those."""
    last = cfg["flat_slot"]
    keep = cfg.get("dates")
    expiry = cfg.get("expiry") or set()
    dates = sorted(days)
    out = []
    for di, d in enumerate(dates):
        bars = days[d]
        if keep is not None and d not in keep:
            continue
        n_bars = partial[1] if partial and d == partial[0] else last + 1
        if n_bars <= 0 or not all(k in bars for k in range(n_bars)):
            continue
        B = [bars[k] for k in range(n_bars)]
        if partial and d == partial[0]:
            bars = {k: bars[k] for k in range(n_bars)}
        prev = days[dates[di - 1]] if di else None
        vw, pv, v = [], 0.0, 0.0
        for o, h, l, c, vol in B:
            pv += (h + l + c) / 3.0 * vol
            v += vol
            vw.append(pv / v if v else None)
        nifty, n_open, calm = context(index_days, vix_days, d, None)
        out.append({"date": d, "bars": B,
                    "tick": observed_tick([x for b in bars.values() for x in b[:4]]) or tick_for(B[0][3]),
                    "vwap": vw, "prev_close": prev[max(prev)][3] if prev else None,
                    "prev_high": max(b[1] for b in prev.values()) if prev else None,
                    "prev_low": min(b[2] for b in prev.values()) if prev else None,
                    "nifty": nifty, "n_open": n_open, "calm": calm, "expiry": d in expiry})
    return out


def trigger(s, setup, long, cfg):
    """(entry bar, tight stop, wide stop, fill) for one session, or None. Prices are raw; the trade rounds
    them. fill is None for a close entry (the bar's close); a stop entry fills inside the bar."""
    B, first = s["bars"], cfg["first_slot"]
    last_entry = min(cfg["last_entry_slot"], len(B) - 1)           # an unfinished session: its closed bars only
    if setup in ("orb15s", "orb30s"):
        # a buy-stop resting a tick over the range high (a sell-stop a tick under the low) from the first bar
        # that STARTS inside the entry window: it fills the moment the price gets there, or at the open of a
        # bar that gaps through it
        n = 3 if setup == "orb15s" else 6
        oh = max(b[1] for b in B[:n])
        ol = min(b[2] for b in B[:n])
        t = s["tick"]
        trig = tick_up(oh + t, t) if long else tick_down(ol - t, t)
        k = next((k for k in range(max(n, first + 1), last_entry + 1)
                  if (B[k][1] >= trig if long else B[k][2] <= trig)), None)
        if k is None:
            return None
        fill = max(trig, B[k][0]) if long else min(trig, B[k][0])
        return k, (oh + ol) / 2.0, ol if long else oh, fill
    if setup in ("orb15", "orb30"):
        n = 3 if setup == "orb15" else 6
        oh = max(b[1] for b in B[:n])
        ol = min(b[2] for b in B[:n])
        k = next((k for k in range(max(n, first), last_entry + 1)
                  if (B[k][3] > oh if long else B[k][3] < ol)), None)
        return None if k is None else (k, (oh + ol) / 2.0, ol if long else oh, None)
    if setup == "pdb":
        level = s["prev_high"] if long else s["prev_low"]
        if level is None:
            return None
        k = next((k for k in range(max(1, first), last_entry + 1)
                  if (B[k][3] > level >= B[k - 1][3] if long else B[k][3] < level <= B[k - 1][3])), None)
    elif setup == "vwap":
        vw, day_open = s["vwap"], B[0][0]
        k = next((k for k in range(max(3, first), last_entry + 1) if vw[k] and vw[k - 1] and (
            (B[k - 1][3] > vw[k - 1] and B[k][2] <= vw[k] < B[k][3] and B[k][3] > day_open) if long else
            (B[k - 1][3] < vw[k - 1] and B[k][1] >= vw[k] > B[k][3] and B[k][3] < day_open))), None)
    else:
        raise EdgeError("unknown setup %r" % setup)
    if k is None:
        return None
    if long:
        return k, B[k][2], min(b[2] for b in B[:k + 1]), None
    return k, B[k][1], max(b[1] for b in B[:k + 1]), None


def entry_at(s, setup, side, stop_rule, target_r, cfg):
    """The rule's entry in one (possibly unfinished) session: {slot, fill, entry, stop, target, risk, qty, time,
    cost_pct, filters}, or None. Uses only the bars up to the entry: what a trader knew at that moment.
    trades_for() replays the exit on the rest of the day; trader-broker's autopilot uses it live."""
    long = side == "long"
    slip = cfg["slippage_ticks"]
    got = trigger(s, setup, long, cfg)
    if got is None:
        return None
    k, tight, wide, fill = got
    B, t = s["bars"], s["tick"]
    px = B[k][3] if fill is None else fill
    raw = tight if stop_rule == "tight" else wide
    if long:
        entry, stop = tick_up(px + slip * t, t), tick_down(raw, t)
        risk = round(entry - stop, 2)
    else:
        entry, stop = tick_down(px - slip * t, t), tick_up(raw, t)
        risk = round(stop - entry, 2)
    if risk <= 0 or risk < entry * MIN_RISK_PCT / 100.0:
        return None
    target = tick_round(entry + target_r * risk if long else entry - target_r * risk, t)
    if target <= 0:
        return None
    qty = size_for(entry, stop, cfg["max_loss"], cfg["max_order"], cfg.get("charges"))
    if qty <= 0:
        return None
    sign = 1 if long else -1
    at_stop = round_trip(qty, *((entry, stop) if long else (stop, entry)), rates=cfg.get("charges"))
    # the filters see only what was known at the entry: the entry bar's close for a close entry, the bar
    # before it for a stop entry (which fills inside its bar)
    fk = k if fill is None else k - 1
    close = B[fk][3]
    n_at = s["nifty"].get(fk, (None,) * 4)[3] if s["nifty"] else None
    n_open, s_open = s["n_open"], B[0][0]
    prior = [b[4] for b in B[:fk]]
    avg_v = sum(prior) / float(len(prior)) if prior else 0
    cost_pct = round(100.0 * at_stop / (qty * risk), 2)
    at_min = OPEN_MIN + 5 * (k + 1 if fill is None else k)
    filt = {
        "none": True,
        "with_nifty": bool(n_open and n_at and (n_at > n_open if long else n_at < n_open)),
        "stronger": bool(n_open and n_at and sign * ((close / s_open - 1) - (n_at / n_open - 1)) > 0),
        "volume": bool(avg_v and B[fk][4] >= avg_v),
        "no_gap": bool(s["prev_close"] and abs(s_open / s["prev_close"] - 1) * 100 < GAP_PCT),
        "calm_vix": bool(s["calm"]),
        "low_cost": cost_pct <= cfg["max_cost_pct"],
        "not_expiry": not s["expiry"],
        "not_10_to_11": not 600 <= at_min < 660,
    }
    return {"slot": k, "fill": fill, "entry": entry, "stop": stop, "target": target, "risk": risk, "qty": qty,
            "time": "%d:%02d" % divmod(at_min, 60), "cost_pct": cost_pct, "filters": filt}


def exit_on(bar, e, long, slip, tick, first=False):
    """(outcome, price) if this bar takes the trade out, else None: the stop first in a bar that touches both.
    first: the entry bar of a stop entry, where only the stop counts (the order inside the bar is unknown)."""
    o, h, l, c, v = bar
    stop, target = e["stop"], e["target"]
    if first:
        if (l <= stop) if long else (h >= stop):
            return "stop", tick_down(stop - slip * tick, tick) if long else tick_up(stop + slip * tick, tick)
        return None
    if long and l <= stop:
        return "stop", tick_down(min(stop, o) - slip * tick, tick)
    if not long and h >= stop:
        return "stop", tick_up(max(stop, o) + slip * tick, tick)
    if (h >= target) if long else (l <= target):
        return "target", target
    return None


def flat_price(bar, long, slip, tick):
    return tick_down(bar[3] - slip * tick, tick) if long else tick_up(bar[3] + slip * tick, tick)


def trades_for(sym, sessions, setup, side, stop_rule, target_r, cfg):
    """Every trade one rule takes on one stock's prepared sessions: [{date, time, side, entry, stop,
    target, exit, outcome, qty, charges, pnl, R_gross, R, cost_pct, filters: {name: bool}}]."""
    long = side == "long"
    last, slip = cfg["flat_slot"], cfg["slippage_ticks"]
    out = []
    for s in sessions:
        e = entry_at(s, setup, side, stop_rule, target_r, cfg)
        if e is None:
            continue
        B, t, k = s["bars"], s["tick"], e["slot"]
        hit = exit_on(B[k], e, long, slip, t, first=True) if e["fill"] is not None else None
        j = k + 1
        while hit is None and j <= last:
            hit = exit_on(B[j], e, long, slip, t)
            j += 1
        outcome, exit_px = hit if hit else ("close", flat_price(B[last], long, slip, t))
        exit_px = max(exit_px, 0.01)
        sign = 1 if long else -1
        entry, qty, risk = e["entry"], e["qty"], e["risk"]
        buy, sell = (entry, exit_px) if long else (exit_px, entry)
        charges = round_trip(qty, buy, sell, cfg.get("charges"))
        pnl = round(sign * qty * (exit_px - entry) - charges, 2)
        out.append({"symbol": sym, "date": s["date"], "time": e["time"], "slot": k, "side": side, "entry": entry,
                    "stop": e["stop"], "target": e["target"], "exit": exit_px, "outcome": outcome, "qty": qty,
                    "risk": risk, "charges": charges, "pnl": pnl, "cost_pct": e["cost_pct"],
                    "R_gross": round(sign * (exit_px - entry) / risk, 4), "R": round(pnl / (qty * risk), 4),
                    "filters": e["filters"]})
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


def bootstrap(trades, resamples=BOOTSTRAP, seed=7):
    """How sure the average R is above zero. Resamples whole sessions (the trades of one day move
    together), so a few big days cannot pass for many independent trades. Returns the 95 % interval of the
    average R and a one-sided p-value that the true average is 0 or less: the larger of the share of resamples
    at or under 0 and the normal tail from the resamples' spread."""
    by_day = {}
    for t in trades:
        x = by_day.setdefault(t["date"], [0.0, 0])
        x[0] += t["R"]
        x[1] += 1
    days = list(by_day.values())
    n = len(days)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        tot = cnt = 0
        for _ in range(n):
            x = days[rng.randrange(n)]
            tot += x[0]
            cnt += x[1]
        means.append(tot / cnt)
    means.sort()
    mean = sum(x[0] for x in days) / sum(x[1] for x in days)
    sd = statistics.pstdev(means)
    normal = 0.5 * math.erfc(mean / sd / math.sqrt(2)) if sd > 0 else (0.0 if mean > 0 else 1.0)
    share = sum(1 for m in means if m <= 0) / float(resamples)
    p = max(normal, share)        # the normal tail resolves small p; the resampled share catches lumpy days
    return {"ci95": [round(means[int(0.025 * resamples)], 3), round(means[int(0.975 * resamples) - 1], 3)],
            "p": float("%.3g" % p)}


def rule_key(setup, side, stop, target_r):
    return {"setup": setup, "side": side, "stop": stop, "target_r": target_r}


BASELINE = rule_key("orb15", "long", "tight", 2.0)


def edge_report(charts, index_text=None, vix_text=None, cfg=None):
    """{symbol: chart text} -> the report document."""
    c = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "no_entries_before": "09:30",
         "no_entries_after": "14:45", "flat_by": "15:10", "max_cost_pct": MAX_COST_PCT,
         "expiry_weekday": EXPIRY_WEEKDAY, "bootstrap": BOOTSTRAP, "setups": SETUPS, "sides": SIDES,
         "tax_pct": TAX_PCT, "trades_per_day": TRADES_PER_DAY, "capital": CAPITAL}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    c["first_slot"] = hhmm_slot(c["no_entries_before"], "no_entries_before")
    c["last_entry_slot"] = hhmm_slot(c["no_entries_after"], "no_entries_after")
    c["flat_slot"] = hhmm_slot(c["flat_by"], "flat_by")
    if c["last_entry_slot"] >= c["flat_slot"]:
        raise EdgeError("no_entries_after must be before flat_by")
    for x in c["setups"]:
        if x not in SETUPS:
            raise EdgeError("unknown setup %r: pick from %s" % (x, ", ".join(SETUPS)))
    for x in c["sides"]:
        if x not in SIDES:
            raise EdgeError("unknown side %r: long or short" % x)
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
    all_days = set(dates) | set(index_days)
    for days in per_symbol.values():
        all_days |= set(days)
    c["dates"] = dates
    c["expiry"] = expiry_dates(all_days, c["expiry_weekday"])
    prepared = {sym: prepare(days, index_days, vix_days, c) for sym, days in per_symbol.items()}
    filters = [f for f in FILTERS if not (set(f.split("+")) & {"with_nifty", "stronger"} and not index_days)
               and not ("calm_vix" in f.split("+") and not vix_days)]
    rows = []
    for setup in c["setups"]:
        for side in c["sides"]:
            for stop_rule in STOPS:
                for tr in TARGETS:
                    all_trades = []
                    for sym, sess in prepared.items():
                        all_trades += trades_for(sym, sess, setup, side, stop_rule, tr, c)
                    for f in filters:
                        need = f.split("+")
                        ts = [t for t in all_trades if all(t["filters"][x] for x in need)]
                        a, b = stats([t for t in ts if t["date"] in ins]), stats([t for t in ts if t["date"] in oos])
                        held = (a["trades"] >= MIN_TRADES and b["trades"] >= MIN_TRADES and a["avg_R"] > 0
                                and b["avg_R"] > 0)
                        rows.append({"rule": rule_key(setup, side, stop_rule, tr), "filter": f,
                                     "all": stats(ts), "in_sample": a, "out_of_sample": b, "held_up": held,
                                     "_trades": ts})
    n = len(rows)
    bar = ALPHA / n
    for r in rows:
        r["bootstrap"] = None
        r["passed"] = False
        if r["all"]["trades"] >= 2 * MIN_TRADES and r["all"]["avg_R"] > 0:
            r["bootstrap"] = bootstrap(r["_trades"], c["bootstrap"])
            r["passed"] = bool(r["held_up"] and r["bootstrap"] and r["bootstrap"]["p"] < bar)

    rows.sort(key=lambda r: (-rank_key(r), SETUPS.index(r["rule"]["setup"]), r["rule"]["side"], r["rule"]["stop"],
                             r["rule"]["target_r"], r["filter"]))
    best = rows[0]
    base = [r for r in rows if r["filter"] == "none" and r["rule"] == BASELINE]
    by_setup = []
    for setup in c["setups"]:
        for side in c["sides"]:
            mine = [r for r in rows if r["rule"]["setup"] == setup and r["rule"]["side"] == side]
            plain = [r for r in mine if r["filter"] == "none"]
            by_setup.append({"setup": setup, "side": side,
                             "unfiltered": _slim(max(plain, key=rank_key)) if plain else None,
                             "best": _slim(max(mine, key=rank_key)) if mine else None})

    def breakdown(r):
        by_sym, by_time = {}, {}
        for t in r["_trades"]:
            by_sym.setdefault(t["symbol"], []).append(t)
            bucket = "9:30-10:00" if t["slot"] < 9 else "10:00-11:00" if t["slot"] < 21 else "11:00-14:45"
            by_time.setdefault(bucket, []).append(t)
        syms = sorted(((s, stats(v)) for s, v in by_sym.items()), key=lambda x: -x[1]["pnl"])
        return {"by_symbol": [{"symbol": s, **v} for s, v in syms],
                "by_time": {k: stats(by_time[k]) for k in ("9:30-10:00", "10:00-11:00", "11:00-14:45") if k in by_time}}
    costs = sorted(t["cost_pct"] for r in rows if r["filter"] == "none" and r["rule"]["target_r"] == 2.0
                   for t in r["_trades"])
    doc = {
        "created_by": "trader-timemachine edge.py",
        "symbols": len(per_symbol), "failed": errors, "sessions": len(dates),
        "in_sample": {"from": ins_span[0], "to": ins_span[1], "sessions": len(ins)},
        "out_of_sample": {"from": oos_span[0], "to": oos_span[1], "sessions": len(oos)} if oos_span else None,
        "expiry_sessions": sorted(d for d in c["expiry"] if d in dates),
        "settings": {k: c[k] for k in ("max_loss", "max_order", "slippage_ticks", "no_entries_before",
                                        "no_entries_after", "flat_by", "max_cost_pct", "expiry_weekday",
                                        "bootstrap")},
        "setups": list(c["setups"]), "sides": list(c["sides"]),
        "combinations": n, "significance": float("%.3g" % bar),
        "charges_in_R": {"median_pct": round(statistics.median(costs), 1) if costs else None,
                         "share_under_limit": round(100.0 * sum(1 for x in costs if x <= c["max_cost_pct"])
                                                    / len(costs), 1) if costs else None},
        "held_up": [_slim(r) for r in rows if r["held_up"]],
        "passed": [_slim(r) for r in rows if r["passed"]],
        "best": dict(_slim(best), **breakdown(best)),
        "baseline": dict(_slim(base[0]), **breakdown(base[0])) if base else None,
        "by_setup": by_setup,
        "rows": [_slim(r) for r in rows],
    }
    nifty_pct = None
    if index_days:
        nd = sorted(d for d in dates if d in index_days and index_days[d])
        if nd and 0 in index_days[nd[0]]:
            last_bar = index_days[nd[-1]][max(index_days[nd[-1]])]
            nifty_pct = round(100.0 * (last_bar[3] / index_days[nd[0]][0][0] - 1), 2)
    doc["benchmark"] = {"nifty_pct": nifty_pct, "capital": c["capital"], "tax_pct": c["tax_pct"],
                        "trades_per_day": c["trades_per_day"]}
    pick = max(doc["held_up"], key=lambda r: r["out_of_sample"]["avg_R"]) if doc["held_up"] else None
    doc["earnings"] = {name: dict(rule=r["rule"], filter=r["filter"], **(earnings(r, len(dates), c) or {}))
                       for name, r in (("best", best), ("best_held_up", pick), ("baseline", doc["baseline"])) if r}
    doc["verdict"] = verdict(doc)
    return doc


def earnings(r, sessions, cfg):
    """What one person could have taken from a rule: the grid takes every signal on every stock, a person
    takes at most trades_per_day a day. So: the rule's average rupees a trade (after charges) x the trades
    a day you would take (its signals a session, capped) x the sessions, then tax on a profit."""
    a = r["all"]
    if not a.get("trades") or not sessions:
        return None
    per_trade = a["pnl"] / a["trades"]
    per_day = min(float(cfg["trades_per_day"]), a["trades"] / float(sessions))
    rupees = round(per_trade * per_day * sessions, 2)
    tax = round(max(rupees, 0) * cfg["tax_pct"] / 100.0, 2)
    return {"per_trade": round(per_trade, 2), "trades_per_day": round(per_day, 2), "rupees": rupees,
            "tax": tax, "after_tax": round(rupees - tax, 2),
            "pct_of_capital": round(100.0 * (rupees - tax) / cfg["capital"], 2) if cfg["capital"] else None}


def rank_key(r):
    """Rules ranked by average R after charges, among those with enough trades in BOTH halves to judge."""
    few = min(r["in_sample"]["trades"], r["out_of_sample"]["trades"])
    return r["all"]["avg_R"] if few >= MIN_TRADES else -1000 + few


def _slim(r):
    return {k: v for k, v in r.items() if k != "_trades"}


STOP_NAMES = {("orb", "tight"): "midpoint", ("orb", "wide"): "range", ("x", "tight"): "entry-bar",
              ("x", "wide"): "day"}


def rule_name(r):
    k = r["rule"]
    return "%s %s, %s stop, %gR%s" % (
        k["side"], SETUP_NAMES[k["setup"]], STOP_NAMES["orb" if k["setup"].startswith("orb") else "x", k["stop"]],
        k["target_r"],
        "" if r["filter"] == "none" else ", " + r["filter"].replace("_", " ").replace("+", " + "))


def verdict(doc):
    held, passed = doc["held_up"], doc.get("passed") or []
    n = doc["combinations"]
    if passed:
        top = max(passed, key=lambda r: r["out_of_sample"]["avg_R"])
        return ("%d of %d combinations made money after charges in both halves AND by more than luck would "
                "explain across %d tests (bootstrap p under %s). The best out-of-sample: %s, %+.2f R a trade "
                "in-sample and %+.2f R out-of-sample. That is evidence, not proof: paper trade it and keep "
                "the journal before believing it." % (len(passed), n, n, doc["significance"], rule_name(top),
                                                     top["in_sample"]["avg_R"], top["out_of_sample"]["avg_R"]))
    if held:
        top = max(held, key=lambda r: r["out_of_sample"]["avg_R"])
        return ("%d of %d combinations made money after charges in both halves, but none by more than luck "
                "would explain across %d tests. The best out-of-sample: %s, %+.2f R in-sample and %+.2f R "
                "out-of-sample. Treat it as a lead to re-test on fresh data and paper trade, not an edge."
                % (len(held), n, n, rule_name(top), top["in_sample"]["avg_R"], top["out_of_sample"]["avg_R"]))
    return ("No rule made money after charges in both halves of the data with %d or more trades in each. "
            "On these %d stocks over these %d sessions, none of the %d combinations had an edge after costs."
            % (MIN_TRADES, doc["symbols"], doc["sessions"], n))


def money(x):
    s = "%.0f" % abs(x)
    if len(s) > 3:
        head, groups = s[:-3], [s[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(groups)
    return ("-" if x < 0 else "") + "₹" + s


def _row(r, width=66):
    a, i, o = r["all"], r["in_sample"], r["out_of_sample"]
    bs = r.get("bootstrap")
    tag = "  PASSED" if r.get("passed") else "  held up" if r["held_up"] else ""
    return ("%-*s %6d %6.1f %+7.3f %+8.3f %11s  %+.2f (%d) / %+.2f (%d)%s%s"
            % (width, rule_name(r)[:width], a["trades"], a["win_pct"], a["avg_R"], a["avg_R_gross"], money(a["pnl"]),
               i.get("avg_R", 0), i["trades"], o.get("avg_R", 0), o["trades"],
               "  p=%s" % bs["p"] if bs else "", tag))


def report(doc, top=12):
    s = doc["settings"]
    lines = ["Edge report: %d stocks, %d sessions (in-sample %s to %s, out-of-sample %s to %s)."
             % (doc["symbols"], doc["sessions"], doc["in_sample"]["from"], doc["in_sample"]["to"],
                doc["out_of_sample"]["from"], doc["out_of_sample"]["to"]),
             "%d combinations: %s; %s; tight or wide stop; 1 to 3R; no filter, each filter, and low cost with each." % (
                 doc["combinations"], ", ".join(SETUP_NAMES[x] for x in doc["setups"]), " and ".join(doc["sides"])),
             "Each trade sized to a %s max loss with charges (max order %s), %d tick%s of slippage, entries %s to %s, "
             "flat by %s." % (money(s["max_loss"]), money(s["max_order"]), s["slippage_ticks"],
                               "" if s["slippage_ticks"] == 1 else "s", s["no_entries_before"], s["no_entries_after"],
                               s["flat_by"]), ""]
    head = "%-66s %6s %6s %7s %8s %11s  %s" % ("rule", "trades", "win %", "avg R", "R gross", "P&L",
                                              "in / out of sample")
    lines.append(head)
    for r in [r for r in doc["rows"] if r["all"]["trades"]][:top]:
        lines.append(_row(r))
    lines += ["", "Best of each setup (unfiltered, then with its best filter):"]
    for x in doc["by_setup"]:
        for r in (x["unfiltered"], x["best"] if x["best"] != x["unfiltered"] else None):
            if r and r["all"]["trades"]:
                lines.append(_row(r))
    b = doc.get("baseline")
    if b:
        lines += ["", "The pack's default (long 15-min range break, midpoint stop, 2R, no filter): %d trades, "
                  "%.1f %% won, %+.3f R after charges (%+.3f before), %s after %s of charges." % (
                      b["all"]["trades"], b["all"].get("win_pct", 0), b["all"].get("avg_R", 0),
                      b["all"].get("avg_R_gross", 0), money(b["all"].get("pnl", 0)), money(b["all"].get("charges", 0)))]
        bt = b.get("by_time") or {}
        if bt:
            lines.append("  by entry time: " + "; ".join("%s %d trades %+.2f R" % (k, v["trades"], v["avg_R"])
                                                         for k, v in bt.items() if v["trades"]))
    ci = doc.get("charges_in_R") or {}
    if ci.get("median_pct") is not None:
        lines.append("Charges at the stop are a median %.1f %% of the planned risk; %.0f %% of trades are under the "
                     "low-cost limit of %g %%." % (ci["median_pct"], ci["share_under_limit"], s["max_cost_pct"]))
    bm, ea = doc.get("benchmark") or {}, doc.get("earnings") or {}
    if ea:
        lines += ["", "What it would have made for one person over these %d sessions, taking at most %d trades a day "
                  "on a %s account, with %g %% tax on a profit:" % (doc["sessions"], bm.get("trades_per_day", 3),
                                                                  money(bm.get("capital") or 0), bm.get("tax_pct", 30))]
        for name, label in (("best", "best rule"), ("best_held_up", "best that held up"), ("baseline", "pack's default")):
            e = ea.get(name)
            if not e or e.get("rupees") is None:
                continue
            lines.append("  %-18s %s: %.1f trades a day, %s a trade, %s, %s after tax (%+.1f %%)"
                         % (label, rule_name(e), e["trades_per_day"], money(e["per_trade"]), money(e["rupees"]),
                            money(e["after_tax"]), e["pct_of_capital"] or 0))
        if bm.get("nifty_pct") is not None:
            lines.append("  Holding Nifty 50 over the same sessions: %+.2f %% (before the index fund's small costs)."
                         % bm["nifty_pct"])
        lines.append("  The best rules are picked with hindsight from %d tries: a fresh period usually gives back "
                     "much of it. Only the default was fixed in advance." % doc["combinations"])
    lines += ["", doc["verdict"],
              "A rule passes only when it made money after charges in both halves (%d+ trades each) and its "
              "session-bootstrap p-value is under %s (0.05 shared across %d tests)." % (
                  MIN_TRADES, doc["significance"], doc["combinations"]),
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
    p.add_argument("--setups", help="comma list from %s (default: all)" % ",".join(SETUPS))
    p.add_argument("--sides", help="long, short or long,short (default: both)")
    p.add_argument("--max-cost-pct", type=float, default=MAX_COST_PCT,
                   help="the low_cost filter: charges at the stop at most this %% of the planned risk")
    p.add_argument("--expiry-weekday", type=int, default=EXPIRY_WEEKDAY, help="0 Monday ... 4 Friday (1, Tuesday)")
    p.add_argument("--bootstrap", type=int, default=BOOTSTRAP, help="session resamples per positive rule")
    p.add_argument("--tax-pct", type=float, default=TAX_PCT, help="your slab rate on a profit (30)")
    p.add_argument("--trades-per-day", type=int, default=TRADES_PER_DAY, help="the most you would take a day (3)")
    p.add_argument("--capital", type=float, default=CAPITAL, help="the account, for the return (1,00,000)")
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
               "no_entries_before": a.no_entries_before, "no_entries_after": a.no_entries_after, "flat_by": a.flat_by,
               "max_cost_pct": a.max_cost_pct, "expiry_weekday": a.expiry_weekday, "bootstrap": a.bootstrap,
               "tax_pct": a.tax_pct, "trades_per_day": a.trades_per_day, "capital": a.capital,
               "setups": tuple(x.strip() for x in a.setups.split(",")) if a.setups else None,
               "sides": tuple(x.strip() for x in a.sides.split(",")) if a.sides else None}
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
