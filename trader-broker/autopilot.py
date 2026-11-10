#!/usr/bin/env python3
"""The paper autopilot for trader-broker: trades the frozen rule on PAPER, all day, with nobody at the keyboard.
Standard library only.

    python3 autopilot.py run --once            # one pass: run it every 5 minutes, 9:20 to 15:20 IST (cron below)
    python3 autopilot.py run --once --now "2026-11-05 10:05" --offline DIR   # a pass at a past moment (the tests)

Before anything: today's pre-flight (preflight.py: GO or NO-GO). On a NO-GO no new trade is taken all day,
while open positions are still managed; when nobody ran the pre-flight this morning, the first pass runs it.
The day's limits recorded then hold all day, as trader-risk holds them.

Each pass, on the bars that have CLOSED by now (Yahoo's 5-minute bars):
  1. exits    each open autopilot position: the first closed bar that reaches its stop (first) or its target
              fills it there (a stop that gaps fills at the bar's open, less a tick); at the flat-by bar
              (15:05, closing 15:10) anything still open is closed at that bar's close, less a tick
  2. entries  the frozen rule in trader/forward.json (forward.py start), with its filter, on every stock on
              --watchlist: a signal on the last closed bar or the one before (a pass that was missed longer is
              not chased) becomes an order, sized by trader-timemachine's edge.py exactly as the shadow test
              sizes it, and goes through EVERY trader-risk check (limits, cool-off, sector, direction, results
              days, events, the index move, surveillance, the window). A PASS fills on paper at the entry; a
              BLOCK is logged and skipped. At most the shadow's per-day count (3), the earliest first.
  3. records  the approval (reply "autopilot (paper)"), the fills with their charges, and the open stop and
              target, in trader-broker's log and the paper portfolio, so ticket.py journal, forward.py status
              and daily.py see them like any paper trade, and the forward test's PAPER verdict comes from them.

PAPER ONLY. With "paper": false in trader/config.json it refuses to run: a live order always needs your own
yes in the chat, one order at a time, and Kite's Allow once card. The autopilot never talks to a broker.

Run it from your trading folder (the one with trader/), every 5 minutes on market days, for example with cron:
    */5 9-15 * * 1-5  cd ~/trading && python3 /path/to/trader-broker/autopilot.py run --once >> trader/autopilot.log 2>&1
It exits at once outside 9:15 to 15:30 and on NSE holidays. Same prices, same rule and same order of passes
give the same trades as the shadow replay, except where a risk check blocks one. Not financial advice.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("trader-risk",):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.append(p)
sys.path.insert(0, HERE)
import ticket  # noqa: E402
import forward  # noqa: E402
import check  # noqa: E402
import preflight  # noqa: E402

edge = forward.edge
OPEN_MIN = 9 * 60 + 15
FRESH_BARS = 2
REPLY = "autopilot (paper)"


class AutopilotError(Exception):
    pass


def closed_bars(now_ist):
    """How many 5-minute bars of today's session have closed by now (the 9:15 bar closes at 9:20)."""
    m = now_ist.hour * 60 + now_ist.minute - OPEN_MIN
    return max(0, min(75, m // 5))


def side_of(rule):
    return "buy" if rule["side"] == "long" else "sell"


def edge_cfg(cfg, state, rates):
    c = {"slippage_ticks": 1, "max_cost_pct": edge.MAX_COST_PCT, "charges": rates,
         "max_order": float(cfg.get("max_order_inr") or 75000)}
    pack = dict(check.DEFAULTS)
    pack.update(check.inr_keys(cfg))
    dd = check.drawdown(state, check._f(pack.get("budget")), rates)
    c["max_loss"], c["drawdown_note"] = check.max_loss_now(pack, dd)
    c["max_loss"] = float(c["max_loss"] or 500)
    c["first_slot"] = edge.hhmm_slot(pack.get("no_entries_before") or "09:30", "no_entries_before")
    c["last_entry_slot"] = edge.hhmm_slot(pack.get("no_entries_after") or "14:45", "no_entries_after")
    c["flat_slot"] = edge.hhmm_slot(pack.get("flat_by") or "15:10", "flat_by")
    return c


def one_pass(a, now_utc):
    now_ist = check.to_ist(now_utc)
    today = now_ist.date().isoformat()
    out = []
    ok, why = check.trading_day(now_ist.date())
    if not ok:
        return ["%s: no session today (%s)." % (today, why)]
    n = closed_bars(now_ist)
    if n < 1:
        return ["%s %s: the session has not closed a bar yet." % (today, now_ist.strftime("%H:%M"))]
    cfg = forward.read(a.config, "config", required=False) or {}
    if not cfg.get("paper", True):
        raise AutopilotError("trader/config.json says \"paper\": false. The autopilot trades paper only: a live "
                             "order needs your yes in the chat, one at a time")
    # today's pre-flight: entries only on a GO; it runs here if nobody ran it this morning
    paths = preflight.paths_of(a)
    pre = preflight.todays(paths, today)
    if pre is None:
        pre = preflight.run(now_utc, paths, a.offline)
        out += preflight.lines(pre)
    go = pre.get("go", False)
    # the day's limits hold all day: a limit loosened after the morning waits until tomorrow
    morning = forward.read(a.limits, "the day's limits", required=False)
    if morning and morning.get("date") == today:
        eff, held = check.hold_morning_limits(dict(check.DEFAULTS, **check.inr_keys(cfg)), morning.get("limits") or {})
        cfg = dict(cfg, **eff)
        if held and (forward.read(a.out, "pre-flight", required=False) or {}).get("held_warned") != today:
            out += ["%s: %s" % (today, w) for w in held]
            pre["held_warned"] = today
            forward.write(a.out, pre)
    else:
        morning = None
    test = forward.read(a.forward, "forward test (forward.py start first)")
    rule, need = test["rule"], test["filter"].split("+")
    per_day = (test.get("shadow") or {}).get("per_day", forward.PER_DAY)
    state = ticket.load_state(a.state)
    rates = cfg.get("charges")
    c = edge_cfg(cfg, state, rates)
    c["dates"] = {today}
    syms = edge.symbols_from(argparse.Namespace(symbols=a.symbols, watchlist=a.watchlist))
    want_vix = "calm_vix" in need
    charts = {}
    for i, s in enumerate(syms + [edge.INDEX] + ([edge.VIX] if want_vix else [])):
        try:
            if a.offline:
                charts[s] = edge.read_offline(a.offline, s)
            else:
                if i:
                    edge._sleep(edge.PAUSE)
                charts[s] = edge.fetch(s, "60d" if s == edge.VIX else "5d")
        except edge.EdgeError:
            pass
    idx_days = edge.sessions_of(charts.pop(edge.INDEX), edge.INDEX) if edge.INDEX in charts else {}
    vix_days = edge.sessions_of(charts.pop(edge.VIX), edge.VIX) if edge.VIX in charts else {}
    all_days = set(idx_days)
    days = {}
    for s, t in charts.items():
        try:
            days[s] = edge.sessions_of(t, s)
            all_days |= set(days[s])
        except edge.EdgeError:
            continue
    c["expiry"] = edge.expiry_dates(all_days)
    long = rule["side"] == "long"
    slip = c["slippage_ticks"]
    legs = state.setdefault("paper_legs", [])

    # entries: fresh signals of the frozen rule, through every risk check
    taken_today = {l["symbol"] for l in legs if l.get("auto") and l.get("date") == today}
    blocked_today = {b["symbol"] for b in state.get("autopilot_blocked", []) if b.get("date") == today}
    room = per_day - len(taken_today)
    if not go and room > 0 and not any("NO-GO" in x for x in out):
        out.append("%s %s: NO-GO this morning (%s): no new trades today; open positions are still managed."
                   % (today, now_ist.strftime("%H:%M"), "; ".join(pre.get("no_go") or ["see preflight.py"])))
    if go and room > 0 and n - 1 <= c["last_entry_slot"] + 1:
        cands = []
        for sym, d in sorted(days.items()):
            if sym in taken_today or sym in blocked_today or today not in d:
                continue
            sess = [x for x in edge.prepare(d, idx_days, vix_days, c, partial=(today, n)) if x["date"] == today]
            if not sess:
                continue
            e = edge.entry_at(sess[0], rule["setup"], rule["side"], rule["stop"], rule["target_r"], c)
            if e and all(e["filters"].get(x, False) for x in need) and e["slot"] >= n - FRESH_BARS:
                cands.append((e["slot"], sym, e, sess[0]))
        for slot, sym, e, sess in sorted(cands)[:room]:
            order = {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": side_of(rule),
                     "qty": str(e["qty"]), "type": "market", "time_in_force": "day", "order_class": "bracket",
                     "stop_loss_stop_price": "%.2f" % e["stop"], "take_profit_limit_price": "%.2f" % e["target"],
                     "plan": {"entry": e["entry"], "stop": e["stop"], "target": e["target"],
                              "flat_by": cfg.get("flat_by") or "15:10"}}
            pf = forward.read(a.portfolio, "portfolio", required=False)
            res = check.check([order], cfg, pf, {sym: e["entry"]}, state, today, now_utc, None, None, None,
                              (lambda sd: sd.get("sectors", sd) if isinstance(sd, dict) else {})(
                                  forward.read(a.sectors, "sectors", required=False) or {}),
                              check.load_events(forward.read(a.events, "events", required=False)),
                              forward.read(a.surveillance, "surveillance", required=False), test, morning)
            r = res["results"][0]
            if r["verdict"] != "PASS":
                state.setdefault("autopilot_blocked", []).append({"date": today, "symbol": sym, "time": e["time"],
                                                                  "reasons": r["reasons"]})
                ticket.write_json(a.state, state)
                out.append("%s %s: %s signal %s at %.2f BLOCKED by trader-risk: %s"
                           % (today, e["time"], sym, rule["side"], e["entry"], "; ".join(r["reasons"])))
                continue
            loss, stop = ticket.max_loss(order)
            fp = ticket.fingerprint(order)
            stamp = check.to_ist(now_utc).strftime("%Y-%m-%dT%H:%M:%S+05:30")
            state["orders"].append({"at": stamp, "date": today, "fingerprint": fp, "symbol": sym,
                                    "side": side_of(rule), "qty": str(e["qty"]), "kind": "entry", "max_loss": loss,
                                    "stop": e["stop"], "plain": ticket.plain(order), "status": "approved",
                                    "reply": REPLY, "paper": True, "broker": "paper"})
            state["orders"].append({"at": stamp, "date": today, "fingerprint": fp, "symbol": sym,
                                    "side": side_of(rule), "plain": ticket.plain(order), "status": "placed",
                                    "paper": True, "broker_order_id": "AUTO-%s-%d" % (fp[:8], len(state["orders"])),
                                    "qty": str(e["qty"]), "kind": "entry", "max_loss": loss, "stop": e["stop"]})
            leg = {"symbol": sym, "side": "long" if long else "short", "qty": str(e["qty"]), "entry": e["entry"],
                   "stop": e["stop"], "target": e["target"], "date": today, "opened_at": stamp, "fingerprint": fp,
                   "status": "open", "auto": True, "entry_slot": slot, "checked": slot,
                   "stop_entry": e["fill"] is not None, "flat_by": cfg.get("flat_by") or "15:10"}
            legs.append(leg)
            ticket.write_json(a.state, state)
            ticket.apply_fill(a.portfolio, cfg, sym, side_of(rule), e["qty"], e["entry"], today, short=not long)
            ticket.log_fill(a.state, now_utc, today, sym, side_of(rule), e["qty"], e["entry"], cfg, paper=True,
                            note="autopilot entry")
            state = ticket.load_state(a.state)
            legs = state.setdefault("paper_legs", [])
            out.append("%s %s: %s %s %s at %.2f, stop %.2f, target %.2f (paper, autopilot)"
                       % (today, e["time"], "SELL SHORT" if not long else "BUY", e["qty"], sym, e["entry"],
                          e["stop"], e["target"]))

    # exits: closed bars after each open autopilot position's last check
    for leg in [l for l in legs if l.get("auto") and l.get("status") == "open"]:
        sym = leg["symbol"]
        bars = (days.get(sym) or {}).get(leg["date"]) or {}
        lg = leg["side"] == "long"
        tick = edge.observed_tick([x for b in bars.values() for x in b[:4]]) or edge.tick_for(leg["entry"])
        hit, at = None, None
        if leg.get("stop_entry") and leg["checked"] == leg["entry_slot"] and not leg.get("entry_bar_done"):
            leg["entry_bar_done"] = True
            if leg["entry_slot"] in bars:
                hit = edge.exit_on(bars[leg["entry_slot"]], leg, lg, slip, tick, first=True)
                at = leg["entry_slot"]
        last_closed = n - 1 if leg["date"] == today else c["flat_slot"]
        j = leg["checked"] + 1
        while hit is None and j <= min(last_closed, c["flat_slot"]):
            if j not in bars:
                break
            hit = edge.exit_on(bars[j], leg, lg, slip, tick)
            at = j
            leg["checked"] = j
            j += 1
        if hit is None and leg["checked"] >= c["flat_slot"] and c["flat_slot"] in bars:
            hit, at = ("flat-by", edge.flat_price(bars[c["flat_slot"]], lg, slip, tick)), c["flat_slot"]
        if hit is None:
            continue
        why, px = hit
        px = max(px, 0.01)
        out_side = "sell" if lg else "buy"
        leg.update({"status": "filled", "exit": px, "exit_reason": why,
                    "closed_at": check.to_ist(now_utc).strftime("%Y-%m-%dT%H:%M:%S+05:30")})
        ticket.write_json(a.state, state)
        ticket.apply_fill(a.portfolio, cfg, sym, out_side, float(leg["qty"]), px, leg["date"])
        ticket.log_fill(a.state, now_utc, leg["date"], sym, out_side, float(leg["qty"]), px, cfg, paper=True,
                        note="autopilot %s" % why)
        state = ticket.load_state(a.state)
        legs = state.setdefault("paper_legs", [])
        out.append("%s %d:%02d: %s %s %s at %.2f (%s, paper, autopilot)"
                   % (leg["date"], *divmod(OPEN_MIN + 5 * (at + 1), 60), out_side.upper(), leg["qty"], sym, px, why))
    ticket.write_json(a.state, state)
    if not out:
        out.append("%s %s: nothing to do (%d bars closed)." % (today, now_ist.strftime("%H:%M"), n))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="The paper autopilot: the frozen rule, traded on paper (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="one pass, or every --every seconds until --until")
    p.add_argument("--once", action="store_true", help="one pass (run it from cron every 5 minutes)")
    p.add_argument("--now", help="pretend it is this IST time")
    p.add_argument("--watchlist", default="trader/nifty50.txt")
    p.add_argument("--symbols")
    p.add_argument("--offline", metavar="DIR", help="<SYMBOL>.json(.gz) 5-minute charts instead of Yahoo")
    p.add_argument("--forward", default="trader/forward.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--sectors", default="trader/sectors.json")
    p.add_argument("--events", default="trader/events.json")
    p.add_argument("--surveillance", default="trader/surveillance.json")
    p.add_argument("--preflight", dest="out", default=preflight.STATE, help="today's pre-flight (preflight.py)")
    p.add_argument("--limits-state", dest="limits", default=preflight.LIMITS, help="the day's limits (trader-risk)")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        now = check.parse_now(a.now)
        for line in one_pass(a, now):
            print(line)
    except (AutopilotError, forward.ForwardError, ticket.TicketError, edge.EdgeError, check.RiskError) as e:
        print("autopilot.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
