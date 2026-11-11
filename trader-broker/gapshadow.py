#!/usr/bin/env python3
"""The gap-up shadow test for trader-broker: openclose.py's one passing rule, judged on sessions after it.

openclose.py found one rule that passed on ten years of daily bars, IF the entry is made in NSE's pre-open
auction: short a Nifty 50 stock that opened 2 % or more above yesterday's close, a stop 1 % above the entry,
out at 15:10. It also found it fading in the later years. A backtest that picked one rule out of 200 flatters
it, so, like forward.py, this freezes the rule and judges it only on sessions after the freeze. Nothing is
traded: it needs pre-open orders and pre-open prices that only a broker's feed has. The shadow is what the
rule would have done, worked out after the close from Yahoo's daily bar (the official open IS the auction
price) and the day's 5-minute bars (the stop, minute by minute, and the 15:10 exit).

    python3 gapshadow.py start                       # freeze it today (trader/forward-gap.json)
    python3 gapshadow.py shadow                      # replay every session since, after the close
    python3 gapshadow.py status

One session: the stocks that opened 2 % or more up, the 3 biggest gaps (3 trades a day, as the pack trades),
each sold short at the official open less a tick, the stop 1 % above on the tick, a day whose 5-minute bars
reach it out at the stop plus a tick, otherwise bought back at the 15:05 bar's close plus a tick; sized to
the ₹500 max loss with charges, intraday charges. The verdict is forward.py's: none before 40 trades, STOP
on an 8 R fall from the best, at 40 STOP at 0 R or under the backtest's low end. Paper and shadow only.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, "..", "trader-timemachine"),):
    p = os.path.normpath(p)
    if p not in sys.path:
        sys.path.append(p)
sys.path.insert(0, HERE)
import forward  # noqa: E402  (the verdict, the file handling)
import edge  # noqa: E402
import swing  # noqa: E402
import openclose  # noqa: E402

FILE = "trader/forward-gap.json"
RULE = {"kind": "openclose", "setup": "gap_up_fade2", "stop": "pct1", "filter": "none", "entry": "auction",
        "per_day": 3, "pick": "the biggest gaps"}
# openclose.py on the Nifty 50, 2016-12-01 to 2026-11-09, --auction: +0.283 R a trade over 2,275 trades,
# the month bootstrap's 95 % range +0.147 to +0.411 R
EXPECTED = {"avg_R": 0.283, "low": 0.147, "source": "openclose.py --auction, Nifty 50, 2016-12 to 2026-11"}
CFG = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "open_adj": {"all": 0.0},
       "close_adj": {"all": 0.0}}


class GapError(Exception):
    pass


def rule_text(test=None):
    return "short a 2 %+ gap-up at the auction price, 1 % stop, out at 15:10 (3 a day, biggest gaps)"


def freeze(today, min_trades=forward.MIN_TRADES, max_dd=forward.MAX_DRAWDOWN_R):
    return {"created_by": "trader-broker gapshadow.py", "rule": dict(RULE), "filter": RULE["filter"],
            "started": today, "account": "shadow", "expected": dict(EXPECTED),
            "stop_rules": {"min_trades": min_trades, "max_drawdown_R": max_dd}, "signals": [],
            "shadow": {"sessions": {}}}


def fetch_daily(sym):
    path = "/v8/finance/chart/%s?range=1mo&interval=1d" % urllib.parse.quote(sym, safe="")
    last = "no answer"
    for host in swing.HOSTS:
        try:
            return swing._get("https://%s.finance.yahoo.com%s" % (host, path))
        except Exception as e:  # noqa: BLE001  (any failure: try the other host)
            last = str(e)
    raise GapError("Yahoo did not answer for %s (%s)" % (sym, last))


def session_trades(day, daily, five, cfg=None):
    """The rule on one session. daily {sym: [(date, o, h, l, c)]}, five {sym: {date: {slot: bar}}}. Only stocks
    whose 5-minute bars reach the 15:05 bar count (a finished session)."""
    c = dict(CFG)
    c.update(cfg or {})
    cands = []
    for sym, bars in daily.items():
        idx = {b[0]: i for i, b in enumerate(bars)}
        i = idx.get(day)
        sess = (five.get(sym) or {}).get(day)
        if not i or not sess or openclose.FLAT_SLOT not in sess:
            continue
        o, prev = bars[i][1], bars[i - 1][4]
        gap = 100.0 * (o / prev - 1)
        if openclose.fires(RULE["setup"], gap):
            cands.append((gap, sym, bars[i], sess))
    out = []
    for gap, sym, b, sess in sorted(cands, key=lambda x: (-x[0], x[1]))[:RULE["per_day"]]:
        d = {"date": day, "o": b[1], "h": b[2], "l": b[3], "c": b[4], "gap": gap, "atr": None}
        path = [sess[k] for k in sorted(sess) if k <= openclose.FLAT_SLOT]
        t = openclose.one_trade(d, False, RULE["stop"], c, path=path)
        if t is None:
            continue
        R, pnl, ret, stopped = t
        out.append({"date": day, "symbol": sym, "gap_pct": round(gap, 2), "entry_open": b[1],
                    "outcome": "stop" if stopped else "flat", "net": pnl, "R": R})
    return out


def update(test, daily_texts, five_texts, today):
    """Replay every finished session from the start to today that the data holds; earlier sessions the data no
    longer holds are kept. -> the evaluation (forward.evaluate)."""
    daily, five = {}, {}
    for sym, text in daily_texts.items():
        try:
            daily[sym] = swing.daily_bars(text, sym)
        except swing.SwingError:
            continue
    for sym, text in five_texts.items():
        try:
            five[sym] = edge.sessions_of(text, sym)
        except edge.EdgeError:
            continue
    days = sorted({d for s in five.values() for d in s if test["started"] <= d <= today})
    sessions = test.setdefault("shadow", {}).setdefault("sessions", {})
    for d in days:
        if any(openclose.FLAT_SLOT in (five[s].get(d) or {}) for s in five):
            sessions[d] = session_trades(d, daily, five)
    test["shadow"]["replayed"] = today
    trades = [t for d in sorted(sessions) for t in sessions[d]]
    res = forward.evaluate(test, trades)
    forward.record(test, "shadow", res, today)
    return res


def lines(test, res, today):
    L = ["Rule: %s, frozen %s. Shadow only: it needs pre-open orders." % (rule_text(test), test["started"])]
    todays = (test.get("shadow") or {}).get("sessions", {}).get(today)
    if todays is None:
        L.append("- Today: not replayed (no finished 5-minute bars yet).")
    elif not todays:
        L.append("- Today: no stock opened 2 % or more up.")
    for t in todays or []:
        L.append("- Today: %s opened %+.1f %%, short at %.2f, %s, %+.2f R (%s)." % (
            t["symbol"], t["gap_pct"], t["entry_open"], "stopped" if t["outcome"] == "stop" else "out at 15:10",
            t["R"], swing.money(t["net"])))
    L.append("- Since the start: %d trades, %s R a trade, %s R in all. %s" % (
        res["trades"], ("%+.2f" % res["avg_R"]) if res["avg_R"] is not None else "n/a", "%+.2f" % res["total_R"],
        res["verdict"]))
    if res.get("performance", {}).get("trades"):
        L.append("- " + forward.ticket.performance_text(res["performance"]) + ".")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="The gap-up shadow test (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start")
    s.add_argument("--file", default=FILE)
    s.add_argument("--today")
    s.add_argument("--replace", action="store_true")
    for name in ("shadow", "status"):
        p = sub.add_parser(name)
        p.add_argument("--file", default=FILE)
        p.add_argument("--today")
        if name == "shadow":
            p.add_argument("--watchlist", default="trader/nifty50.txt")
            p.add_argument("--offline", metavar="DIR", help="daily charts <SYMBOL>.json(.gz)")
            p.add_argument("--offline-5m", metavar="DIR", help="5-minute charts")
    a = ap.parse_args(argv)
    today = a.today if getattr(a, "today", None) else (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
    try:
        if a.cmd == "start":
            if os.path.exists(a.file) and not a.replace:
                raise GapError("a gap-up shadow test is already running in %s (frozen %s)"
                               % (a.file, forward.read(a.file, "test")["started"]))
            forward.write(a.file, freeze(today))
            print("Frozen on %s: %s. Expected %+.3f R a trade, low end %+.3f R (%s). No verdict before %d trades."
                  % (today, rule_text(), EXPECTED["avg_R"], EXPECTED["low"], EXPECTED["source"], forward.MIN_TRADES))
            return 0
        test = forward.read(a.file, "gap-up shadow test (gapshadow.py start)")
        if a.cmd == "status":
            trades = [t for d in sorted(test["shadow"]["sessions"]) for t in test["shadow"]["sessions"][d]]
            print("\n".join(lines(test, forward.evaluate(test, trades), today)))
            return 0
        if a.cmd != "shadow":
            ap.print_help()
            return 1
        syms = edge.symbols_from(argparse.Namespace(symbols=None, watchlist=a.watchlist))
        daily_t, five_t = {}, {}
        for i, sym in enumerate(syms):
            try:
                if a.offline:
                    daily_t[sym] = swing.read_offline(a.offline, sym)
                else:
                    if i:
                        time.sleep(swing.PAUSE)
                    daily_t[sym] = fetch_daily(sym)
                five_t[sym] = edge.read_offline(a.offline_5m, sym) if a.offline_5m else edge.fetch(sym)
            except (GapError, swing.SwingError, edge.EdgeError):
                continue
        res = update(test, daily_t, five_t, today)
        forward.write(a.file, test)
        print("\n".join(lines(test, res, today)))
    except (GapError, forward.ForwardError) as e:
        print("gapshadow.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
