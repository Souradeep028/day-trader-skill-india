#!/usr/bin/env python3
"""The trade audit page for trader-broker: every paper and shadow trade on its own 5-minute chart.

Run it in the trading folder after the daily report and archive.py save. It writes one self-contained page
(trader/audit/index.html) with the data inside: a summary for each test, the trades with filters, each trade's
5-minute candles with the entry, stop, target and exit marked (TradingView's open-source Lightweight Charts),
and a cost card (gross, each charge, net). Publish or open it as you like; nothing in it is live.

    python3 audit.py                 # trader/audit/index.html
    python3 audit.py --replay        # also (re)work out both rules on the archived sessions before their freeze

Every trade, each with its day's 5-minute bars from trader/bars/5m (trader-timemachine's archive.py):
  orb-shadow   the frozen ORB rule's shadow (trader/forward.json)
  orb-paper    the autopilot's paper trades (trader/state/broker.json)
  gap-shadow   the gap-up short's shadow (trader/forward-gap.json)
  replay-*     the same two rules on the archived sessions before they were frozen (trader/audit/replay.json):
               context for the page, not part of either test
"""

import argparse
import datetime
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("trader-timemachine", "trader-risk"):
    p = os.path.normpath(os.path.join(HERE, "..", sub))
    if p not in sys.path:
        sys.path.append(p)
sys.path.insert(0, HERE)
import edge  # noqa: E402
import forward  # noqa: E402
import gapshadow  # noqa: E402
import swing  # noqa: E402
import ticket  # noqa: E402

BARS = os.path.join("trader", "bars", "5m")
OUT = os.path.join("trader", "audit")
TEMPLATE = os.path.join(HERE, "audit.html")
OPEN_MIN = 9 * 60 + 15
FLAT_SLOT = 70
IST = 19800


def day_bars(day, sym, cache={}):
    """[[t_ist, o, h, l, c], ...] for one stock's session, times shifted to India time for the chart."""
    if day not in cache:
        p = os.path.join(BARS, "%s.json.gz" % day)
        cache[day] = json.load(gzip.open(p, "rt"))["bars"] if os.path.exists(p) else {}
    return [[r[0] + IST, r[1], r[2], r[3], r[4]] for r in cache[day].get(sym, [])]


def slot_of(t_ist):
    m = (t_ist // 60) % 1440
    return (m - OPEN_MIN) // 5


def bar_time(bars, slot):
    for b in bars:
        if slot_of(b[0]) == slot:
            return b[0]
    return bars[-1][0] if bars else None


def exit_slot(bars, entry_slot, side, stop, target, outcome, exit_px):
    """The bar the trade ended on: the first after the entry to reach the stop (or the target), else the flat-by."""
    if outcome in ("stop", "target"):
        for b in bars:
            k = slot_of(b[0])
            if k <= entry_slot or k > FLAT_SLOT:
                continue
            hit_stop = b[2] >= stop if side == "short" else b[3] <= stop
            hit_tgt = target is not None and (b[3] <= target if side == "short" else b[2] >= target)
            if hit_stop or hit_tgt:
                return k
    if exit_px is not None and outcome not in ("close", "flat"):
        for b in bars:
            k = slot_of(b[0])
            if entry_slot < k <= FLAT_SLOT and b[3] <= exit_px <= b[2]:
                return k
    return FLAT_SLOT


def costs(side, qty, entry, exit_px):
    buy, sell = (entry, exit_px) if side == "long" else (exit_px, entry)
    a, b = edge.order_charges("buy", qty * buy), edge.order_charges("sell", qty * sell)
    return {k: round(a[k] + b[k], 2) for k in a}


def card(kind, t, bars, entry_slot, xslot, extra=None):
    side = t.get("side", "short")
    qty = int(float(t["qty"]))
    c = costs(side, qty, t["entry"], t["exit"])
    gross = round((t["exit"] - t["entry"]) * qty * (1 if side == "long" else -1), 2)
    tick = edge.tick_for(t["entry"])
    out = {"kind": kind, "date": t["date"], "symbol": t["symbol"], "side": side, "qty": qty,
           "entry": t["entry"], "stop": t["stop"], "target": t.get("target"), "exit": t["exit"],
           "outcome": t.get("outcome"), "R": t.get("R"), "net": t.get("net", t.get("pnl")), "gross": gross,
           "costs": c, "tick_cost": round(2 * tick * qty, 2),
           "entry_time": bar_time(bars, entry_slot), "exit_time": bar_time(bars, xslot),
           "entry_label": "%d:%02d" % divmod(OPEN_MIN + 5 * (entry_slot + 1), 60) if kind.endswith("orb") or
           "orb" in kind else "9:15 (auction)", "exit_label": "%d:%02d" % divmod(OPEN_MIN + 5 * (xslot + 1), 60),
           "bars": [b[:5] for b in bars]}
    out.update(extra or {})
    return out


def orb_cards(sessions, kind):
    out = []
    for day in sorted(sessions):
        for t in sessions[day]:
            bars = day_bars(day, t["symbol"])
            if not bars:
                continue
            h, m = map(int, t["time"].split(":"))
            k = (h * 60 + m - OPEN_MIN) // 5 - 1
            x = exit_slot(bars, k, t["side"], t["stop"], t.get("target"), t["outcome"], t["exit"])
            out.append(card(kind, t, bars, k, x))
    return out


def gap_cards(sessions, kind):
    out = []
    for day in sorted(sessions):
        for t in sessions[day]:
            bars = day_bars(day, t["symbol"])
            if not bars or "entry" not in t:
                continue
            out.append(card(kind, t, bars, 0, t.get("exit_slot", FLAT_SLOT) or FLAT_SLOT,
                            {"gap_pct": t.get("gap_pct"), "entry_label": "9:15 (auction)"}))
    return out


def paper_cards(state, started, today, cfg):
    trips, _ = ticket.round_trips(state, started, today, paper=True, rates=cfg.get("charges"))
    legs = {(l["date"], l["symbol"]): l for l in state.get("paper_legs", []) if l.get("status") == "filled"}
    out = []
    for r in trips:
        leg = legs.get((r["date"], r["symbol"]))
        bars = day_bars(r["date"], r["symbol"])
        if not leg or not bars:
            continue
        k = int(leg.get("entry_slot", 0))
        t = {"date": r["date"], "symbol": r["symbol"], "side": leg["side"], "qty": leg["qty"], "entry": leg["entry"],
             "stop": leg["stop"], "target": leg.get("target"), "exit": leg["exit"], "R": r.get("R"), "net": r["net"],
             "outcome": "stop" if "stop" in (leg.get("exit_reason") or "") else
             "target" if "target" in (leg.get("exit_reason") or "") else "flat"}
        x = exit_slot(bars, k, t["side"], t["stop"], t["target"], t["outcome"], t["exit"])
        c = card("orb-paper", t, bars, k, x, {"signal": leg.get("signal"), "slippage": leg.get("slippage"),
                                               "exit_reason": leg.get("exit_reason"), "costs": r["charges"]})
        c["gross"] = r["gross"]
        out.append(c)
    return out


def summary(cards):
    groups = {}
    for c in cards:
        groups.setdefault(c["kind"], []).append(c)
    out = {}
    for k, cs in groups.items():
        rs = [c["R"] for c in cs if c["R"] is not None]
        p = ticket.performance(cs)
        out[k] = {"trades": len(cs), "wins": sum(1 for c in cs if c["net"] > 0),
                  "avg_R": round(sum(rs) / len(rs), 3) if rs else None, "net": round(sum(c["net"] for c in cs), 2),
                  "profit_factor": p.get("profit_factor")}
    return out


def replay(test, gtest, watchlist="trader/nifty50.txt"):
    """Both rules on the archived sessions before their freeze."""
    days = sorted(f[:10] for f in os.listdir(BARS) if f.endswith(".json.gz"))
    pre_orb = [d for d in days if d < test["started"]]
    pre_gap = [d for d in days if d < gtest["started"]]
    tmp = os.path.join(OUT, ".export")
    import archive
    archive.export(os.path.join("trader", "bars"), "5m", tmp)
    syms = edge.symbols_from(argparse.Namespace(symbols=None, watchlist=watchlist))
    charts = {s: edge.read_offline(tmp, s) for s in syms}
    idx, vix = edge.read_offline(tmp, edge.INDEX), edge.read_offline(tmp, edge.VIX)
    orb = forward.shadow_trades(test, charts, idx, vix, pre_orb, forward.PER_DAY)
    daily, five = {}, {}
    for i, s in enumerate(syms):
        if i:
            time.sleep(0.3)
        daily[s] = swing.daily_bars(edge.fetch(s, "6mo", "1d"), s)
        five[s] = edge.sessions_of(charts[s], s)
    gap = {d: gapshadow.session_trades(d, daily, five) for d in pre_gap}
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return {"orb": orb, "gap": gap, "from": days[0] if days else None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="(re)work out the replay before the freeze (Yahoo daily bars)")
    ap.add_argument("--today")
    ap.add_argument("--watchlist", default="trader/nifty50.txt")
    ap.add_argument("--out", default=os.path.join(OUT, "index.html"))
    a = ap.parse_args(argv)
    today = a.today or (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
    cfg = forward.read("trader/config.json", "config", required=False) or {}
    test = forward.read("trader/forward.json", "forward test")
    gtest = forward.read("trader/forward-gap.json", "gap-up test", required=False)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    rp = os.path.join(os.path.dirname(a.out) or ".", "replay.json")
    if a.replay and gtest:
        with open(rp, "w") as fh:
            json.dump(replay(test, gtest, a.watchlist), fh)
    rep = {}
    if os.path.exists(rp):
        with open(rp) as fh:
            rep = json.load(fh)
    cards = orb_cards((test.get("shadow") or {}).get("sessions", {}), "orb-shadow")
    if os.path.exists("trader/state/broker.json"):
        cards += paper_cards(ticket.load_state("trader/state/broker.json"), test["started"], today, cfg)
    if gtest:
        cards += gap_cards((gtest.get("shadow") or {}).get("sessions", {}), "gap-shadow")
    cards += orb_cards(rep.get("orb", {}), "replay-orb") + gap_cards(rep.get("gap", {}), "replay-gap")
    cards.sort(key=lambda c: (c["date"], c["entry_time"] or 0, c["symbol"]), reverse=True)
    for i, c in enumerate(cards):
        c["id"] = i
    doc = {"built": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC"), "today": today,
           "orb_rule": forward.rule_text(test), "orb_started": test["started"],
           "gap_rule": gapshadow.rule_text(), "gap_started": gtest["started"] if gtest else None,
           "replay_from": rep.get("from"), "summary": summary(cards), "trades": cards}
    with open(TEMPLATE, encoding="utf-8") as fh:
        page = fh.read()
    data = json.dumps(doc, separators=(",", ":")).replace("</", "<\\/")
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(page.replace("/*AUDIT_DATA*/null", data))
    print("Audit page: %d trades (%s): %s." % (len(cards), ", ".join("%s %d" % (k, v["trades"]) for k, v in
                                                                     sorted(doc["summary"].items())) or "none", a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
