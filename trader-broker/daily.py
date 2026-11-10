#!/usr/bin/env python3
"""The daily report for trader-broker: one page after the close, every trading day. Standard library only.

    python3 daily.py                                  # today, after 15:30 IST
    python3 daily.py --date 2026-11-11 --no-write     # print only
    python3 daily.py --offline DIR --date 2026-11-05  # saved charts instead of Yahoo (the tests)

What it puts together, each part from the pack's own tools:

  1. The market     Nifty 50 and India VIX on the day, how many of the watchlist rose and fell, the biggest
                    movers (Yahoo's 5-minute bars, the ones the shadow test replays)
  2. Forward test   the frozen rule's shadow trades for the day and every session since the start it has not
                    replayed yet (forward.py's shadow), the running totals and the verdict, saved to
                    trader/forward.json so trader-risk's live lock sees it
  3. Your trading   the day's round trips from trader-broker's log, paper and live apart, P&L after charges,
                    any position still open (an intraday position should not be), and your limits: the
                    day's losing streak, the week's and the month's realised P&L against their limits, the
                    drawdown, and the live lock and ramp
  4. The core       the SIP in trader/sip.json, its value and XIRR, if there is one
  5. Tomorrow       the next session, Nifty's weekly expiry, results, ex-dates and market events from
                    trader/events.json, and the upkeep that is due (surveillance lists, holidays, charges)

On a day the NSE is closed it says so in one line. The report is written to trader/daily/<date>.md and
printed. It places nothing. Not financial advice.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("trader-risk", "trader-budget"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.append(p)
sys.path.insert(0, HERE)
import ticket  # noqa: E402
import forward  # noqa: E402  (imports trader-timemachine's edge.py)
import check  # noqa: E402  (trader-risk: the calendar, the limits, the live lock)
import sip  # noqa: E402  (trader-budget: the index-fund core)

edge = forward.edge
MOVERS = 3


class DailyError(Exception):
    pass


def money(x):
    return ("+" if x > 0 else "") + ticket._money(x) if x >= 0 else "-" + ticket._money(abs(x))


def day_change(days, day):
    """(close, previous close, percent) for one symbol's sessions {date: {slot: bar}}, or None."""
    if day not in days or not days[day]:
        return None
    before = sorted(d for d in days if d < day and days[d])
    if not before:
        return None
    close = days[day][max(days[day])][3]
    prev = days[before[-1]][max(days[before[-1]])][3]
    return close, prev, round(100.0 * (close / prev - 1), 2)


def next_session(d):
    for i in range(1, 15):
        n = d + datetime.timedelta(days=i)
        if check.trading_day(n)[0]:
            return n
    return None


def market(charts, index_text, vix_text, day):
    out = {"nifty": None, "vix": None, "up": 0, "down": 0, "movers": []}
    if index_text:
        out["nifty"] = day_change(edge.sessions_of(index_text, edge.INDEX), day)
    if vix_text:
        out["vix"] = day_change(edge.sessions_of(vix_text, edge.VIX), day)
    moves = []
    for sym, text in charts.items():
        try:
            ch = day_change(edge.sessions_of(text, sym), day)
        except edge.EdgeError:
            continue
        if ch:
            moves.append((ch[2], sym, ch[0]))
    out["up"] = sum(1 for m in moves if m[0] > 0)
    out["down"] = sum(1 for m in moves if m[0] < 0)
    moves.sort()
    ups = [m for m in moves if m[0] > 0]
    downs = [m for m in moves if m[0] < 0]
    out["movers"] = {"top": [(s, c, p) for p, s, c in reversed(ups[-MOVERS:])],
                     "bottom": [(s, c, p) for p, s, c in downs[:MOVERS]]}
    return out


def forward_part(test, charts, index_text, vix_text, day, cfg):
    """Replay the day, and any session since the start not replayed yet; record the verdict."""
    seen = set()
    for t in charts.values():
        try:
            seen |= set(edge.sessions_of(t, "x"))
        except edge.EdgeError:
            pass
    sh = test.setdefault("shadow", {"per_day": forward.PER_DAY, "sessions": {}})
    days = sorted(d for d in seen if test["started"] <= d <= day and (d == day or d not in sh["sessions"]))
    if not days:
        return None, []
    lim = {k: cfg[k] for k in ("no_entries_before", "no_entries_after", "flat_by") if cfg.get(k)}
    lim["max_loss"] = float(cfg.get("max_loss_per_trade_inr") or 500)
    lim["max_order"] = float(cfg.get("max_order_inr") or 75000)
    got = forward.shadow_trades(test, charts, index_text, vix_text, days, sh.get("per_day", forward.PER_DAY), lim)
    sh["sessions"].update(got)
    trades = [t for d in sorted(sh["sessions"]) for t in sh["sessions"][d]]
    res = forward.evaluate(dict(test, signals=[]), trades)
    forward.record(test, "shadow", res, day)
    return res, sh["sessions"].get(day, [])


def trading_part(state, cfg, day, forward_doc):
    rates = cfg.get("charges")
    out = {"trades": {}, "open": [], "limits": [], "live": None}
    for label, paper in (("paper", True), ("live", False)):
        ts, still = ticket.round_trips(state, day, day, paper=paper, rates=rates)
        if ts:
            out["trades"][label] = ts
        out["open"] += [dict(x, account=label) for x in still]
    c = dict(check.DEFAULTS)
    c.update(check.inr_keys(cfg))
    d = datetime.date.fromisoformat(day)
    streak, closed = check.losing_streak(state, day, rates)
    cap_s = int(c.get("max_losing_streak") or 0)
    if closed:
        out["limits"].append("Losing streak at the close: %d of %d%s" % (streak, cap_s, " (the cool-off ended "
                                                                         "the day's entries)" if cap_s and streak >= cap_s else ""))
    week_from = (d - datetime.timedelta(days=d.weekday())).isoformat()
    month_from = d.replace(day=1).isoformat()
    recent = check.closed_trades(state, min(week_from, month_from), day, rates)
    for label, since, key in (("week", week_from, "max_weekly_loss_inr"), ("month", month_from, "max_monthly_loss_inr")):
        net = round(sum(t[2] for t in recent if t[0] >= since), 2)
        cap = check._f(c.get(key))
        if recent or net:
            out["limits"].append("This %s: %s realised%s" % (label, money(net), (", %s of your %s loss limit used"
                                                                               % (ticket._money(max(-net, 0)), ticket._money(cap)))
                                                             if cap else ""))
    dd = check.drawdown(state, check._f(c.get("budget")), rates)
    if dd and dd[2] >= 0.1:
        out["limits"].append("Drawdown: %.1f %% under the peak%s" % (dd[2], " (size halved)" if check._f(
            c.get("drawdown_cut_pct")) and dd[2] >= check._f(c["drawdown_cut_pct"]) else ""))
    if not c.get("paper", True):
        ok, why = check.live_gate(forward_doc, day)
        if not ok:
            out["live"] = "Live account LOCKED: %s." % why
        else:
            ramp = check.live_ramp(forward_doc, state, day, rates)
            out["live"] = ("Live account STOPPED: %s." % ramp["stop"]) if ramp["stop"] else (
                "Live account open at %d %% size (%d live trades since the forward test passed)."
                % (int(ramp["factor"] * 100), ramp["trades"]))
    return out


def tomorrow_part(day, events_doc, surveillance_doc):
    d = datetime.date.fromisoformat(day)
    nxt = next_session(d)
    out = {"next": nxt.isoformat() if nxt else None, "notes": []}
    if not nxt:
        out["notes"].append("the next session is past the holiday list: check nseindia.com")
        return out
    if check.expiry_day(nxt):
        out["notes"].append("Nifty's weekly F&O expiry")
    special = [v for k, v in check.NSE_SPECIAL_SESSIONS.items() if d < datetime.date.fromisoformat(k) < nxt]
    for s in special:
        out["notes"].append("before it, a special session (%s): not traded" % s)
    ev = check.load_events(events_doc)
    for sym, rows in sorted(ev.items()):
        for date, what, kind in rows:
            if date == nxt.isoformat() or (kind == "results" and date == day):
                out["notes"].append("%s: %s%s" % (sym, what, " (today, after the close: no entries tomorrow)"
                                                  if date == day else ""))
    out["notes"] += check.staleness(nxt)
    if surveillance_doc and surveillance_doc.get("updated"):
        age = (nxt - datetime.date.fromisoformat(str(surveillance_doc["updated"])[:10])).days
        if age > check.STALE_SURVEILLANCE_DAYS:
            out["notes"].append("the ASM/GSM lists are %d days old: refresh them (scan.py surveillance)" % age)
    elif surveillance_doc is None:
        out["notes"].append("no ASM/GSM lists (trader/surveillance.json): scan.py surveillance")
    return out


def report(day, mk, fw, fw_today, test, tr, core, tm):
    d = datetime.date.fromisoformat(day)
    L = ["# Daily report: %s" % d.strftime("%A %d %B %Y").replace(" 0", " "), "", "## The market"]
    if mk["nifty"]:
        L.append("- Nifty 50 %.2f (%+.2f %%)%s." % (mk["nifty"][0], mk["nifty"][2], (", India VIX %.2f (%+.2f %%)"
                                                                                      % (mk["vix"][0], mk["vix"][2]))
                                                    if mk["vix"] else ""))
    if mk["up"] or mk["down"]:
        L.append("- The watchlist: %d up, %d down." % (mk["up"], mk["down"]))
        if mk["movers"]["top"]:
            L.append("- Up most: " + ", ".join("%s %+.1f %%" % (s, p) for s, c, p in mk["movers"]["top"]) + ".")
        if mk["movers"]["bottom"]:
            L.append("- Down most: " + ", ".join("%s %+.1f %%" % (s, p) for s, c, p in mk["movers"]["bottom"]) + ".")
    L += ["", "## Forward test"]
    if test is None:
        L.append("No frozen rule (trader/forward.json): forward.py start --rule ... to begin one.")
    elif fw is None:
        L.append("%s: no sessions since its start (%s) in the data yet." % (forward.rule_text(test), test["started"]))
    else:
        L.append("Rule: %s, frozen %s." % (forward.rule_text(test), test["started"]))
        if fw_today:
            for t in fw_today:
                L.append("- %s %s %s: %s, %s, %+.2f R" % (t["time"], t["side"], t["symbol"], t["outcome"],
                                                          money(t["net"]), t["R"]))
        else:
            L.append("- No signal today.")
        L.append("- So far: %d sessions, %d trades, average %s R, total %+.2f R, worst run %.2f R." % (
            len(test["shadow"]["sessions"]), fw["trades"], "%+.2f" % fw["avg_R"] if fw["avg_R"] is not None else "n/a",
            fw["total_R"], fw["worst_drawdown_R"]))
        L.append("- " + fw["verdict"])
    L += ["", "## Your trading"]
    if tr is None:
        L.append("No trader-broker log here (trader/state/broker.json): run this report where you trade to see "
                 "your trades and limits.")
    else:
        if not tr["trades"]:
            L.append("- No trades closed today.")
        for label, ts in sorted(tr["trades"].items()):
            net = round(sum(t["net"] for t in ts), 2)
            rs = [t["R"] for t in ts if t.get("R") is not None]
            L.append("- %s: %d trade%s, %s after %s of charges%s." % (
                label.capitalize(), len(ts), "" if len(ts) == 1 else "s", money(net),
                ticket._money(sum(t["charges"]["total"] for t in ts)),
                (", %+.2f R" % sum(rs)) if rs else ""))
            for t in ts:
                L.append("    %s %s: %s%s" % (t["side"], t["symbol"], money(t["net"]),
                                              (" (%+.2f R)" % t["R"]) if t.get("R") is not None else ""))
        for o in tr["open"]:
            L.append("- STILL OPEN after the close (%s): %s %s on %s. An intraday position should not be: check "
                     "the broker (ticket.py reconcile)." % (o["account"], ticket._str_num(o["qty"]), o["symbol"], o["date"]))
        for x in tr["limits"]:
            L.append("- " + x + ".")
        if tr["live"]:
            L.append("- " + tr["live"])
    if core:
        L += ["", "## The core (SIP)",
              "- %s: %s in, worth %s (%s)%s." % (core["fund"], ticket._money(core["invested"]), ticket._money(core["value"]),
                                                money(core["gain"]), (", XIRR %+.2f %% a year" % core["xirr_pct"])
                                                if core.get("xirr_pct") is not None else "")]
    L += ["", "## Tomorrow"]
    if tm["next"]:
        L.append("- Next session: %s." % datetime.date.fromisoformat(tm["next"]).strftime("%A %d %B").replace(" 0", " "))
    for n in tm["notes"]:
        L.append("- " + n[0].upper() + n[1:] + ("" if n.endswith(".") else "."))
    L += ["", "Nothing was placed. Not financial advice."]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="The daily report after the close (trader-broker).")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--watchlist", default="trader/nifty50.txt")
    ap.add_argument("--offline", metavar="DIR", help="<SYMBOL>.json(.gz) 5-minute charts instead of Yahoo")
    ap.add_argument("--forward", default="trader/forward.json")
    ap.add_argument("--state", default="trader/state/broker.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--sip", default="trader/sip.json")
    ap.add_argument("--sip-price", type=float, help="the fund's price (default: Yahoo)")
    ap.add_argument("--events", default="trader/events.json")
    ap.add_argument("--surveillance", default="trader/surveillance.json")
    ap.add_argument("--out", help="default trader/daily/<date>.md")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    try:
        day = a.date or (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date().isoformat()
        d = datetime.date.fromisoformat(day)
        ok, why = check.trading_day(d)
        if not ok:
            nxt = next_session(d)
            print("No NSE session on %s (%s). The next one is %s." % (day, why, nxt.isoformat() if nxt else "unknown"))
            return 0
        cfg = forward.read(a.config, "config", required=False) or {}
        syms = edge.symbols_from(argparse.Namespace(symbols=None, watchlist=a.watchlist))
        charts = {}
        for i, s in enumerate(syms + [edge.INDEX, edge.VIX]):
            try:
                if a.offline:
                    charts[s] = edge.read_offline(a.offline, s)
                else:
                    if i:
                        edge._sleep(edge.PAUSE)
                    charts[s] = edge.fetch(s)
            except edge.EdgeError:
                pass
        idx, vix = charts.pop(edge.INDEX, None), charts.pop(edge.VIX, None)
        if not charts and not idx:
            raise DailyError("no market data for %s" % day)
        mk = market(charts, idx, vix, day)
        test = forward.read(a.forward, "forward test", required=False)
        fw, fw_today = (None, [])
        if test:
            fw, fw_today = forward_part(test, charts, idx, vix, day, cfg)
            if not a.no_write:
                forward.write(a.forward, test)
        state = ticket.load_state(a.state) if os.path.exists(a.state) else None
        tr = trading_part(state, cfg, day, test) if state else None
        core = None
        sdoc = forward.read(a.sip, "sip", required=False)
        if sdoc and sdoc.get("buys"):
            try:
                core = sip.status(sdoc, a.sip_price or sip.last_price(sdoc.get("fund", sip.FUND)), day)
            except sip.SipError:
                core = None
        tm = tomorrow_part(day, forward.read(a.events, "events", required=False),
                           forward.read(a.surveillance, "surveillance", required=False))
        lines = report(day, mk, fw, fw_today, test, tr, core, tm)
    except (DailyError, forward.ForwardError, edge.EdgeError, ticket.TicketError) as e:
        print("daily.py: %s" % e, file=sys.stderr)
        return 1
    text = "\n".join(lines) + "\n"
    if not a.no_write:
        out = a.out or os.path.join("trader", "daily", "%s.md" % day)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
