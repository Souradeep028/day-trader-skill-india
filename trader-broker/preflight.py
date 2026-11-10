#!/usr/bin/env python3
"""The pre-flight check for trader-broker: GO or NO-GO before the open. Standard library only.

    python3 preflight.py                  # today, before 9:15 IST (the morning brief runs it too)
    python3 preflight.py --json
    python3 preflight.py --now "2026-11-05 09:00" --offline DIR

It answers one question before the first trade: is everything in place to trade today? It checks, in order:

  calendar    an NSE session today (weekends, holidays and special sessions like Muhurat trading are not
              traded): no session is a plain "closed", not a fault
  config      trader/config.json reads, has a budget; a live account ("paper": false) is reported with
              trader-risk's live lock (the autopilot itself trades paper only)
  rule        the frozen rule in trader/forward.json, a setup edge.py knows; a short rule needs
              "allow_short": true
  data        Yahoo answers for Nifty 50 (^NSEI) with the last session's 5-minute bars
  files       trader/state is writable
  leftovers   no position and no open paper stop or target from an earlier day: an intraday position should
              never be left overnight. NO-GO until it is closed and reconciled (ticket.py reconcile)
  limits      the week's and the month's losses still leave room for one full trade at the max loss; the
              drawdown rule's size is reported
  events      no market event today in trader/events.json (RBI policy, the Budget...), when
              skip_market_event_days is on; Nifty's weekly expiry is a note, or NO-GO with skip_expiry_days
  upkeep      the reminders that are due (surveillance lists, next year's holidays, the charges)

Every check that fails makes it NO-GO, with the reason. The result goes to trader/state/preflight.json, and
the day's limits to trader/state/limits.json (trader-risk's morning snapshot: a limit loosened later today
waits until tomorrow). The autopilot enters trades only on today's GO, runs this itself when it finds none,
and keeps managing open positions either way. Exit code 0 on GO (or closed), 1 on NO-GO.
"""

import argparse
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
p = os.path.join(ROOT, "trader-risk")
if p not in sys.path:
    sys.path.append(p)
sys.path.insert(0, HERE)
import ticket  # noqa: E402
import forward  # noqa: E402
import check  # noqa: E402

edge = forward.edge
STATE = "trader/state/preflight.json"
LIMITS = "trader/state/limits.json"


def run_checks(now_utc, cfg, test, state, portfolio, events_doc, surveillance_doc, index_text, state_dir):
    """Pure apart from the writable-folder probe. Returns the document."""
    now_ist = check.to_ist(now_utc)
    d = now_ist.date()
    today = d.isoformat()
    doc = {"date": today, "checked_at": now_ist.strftime("%Y-%m-%d %H:%M"), "go": True, "closed": False,
           "checks": [], "no_go": [], "notes": []}

    def add(name, ok, detail, hard=True):
        doc["checks"].append({"check": name, "ok": ok, "detail": detail})
        if not ok and hard:
            doc["go"] = False
            doc["no_go"].append("%s: %s" % (name, detail))

    ok, why = check.trading_day(d)
    if not ok:
        special = check.NSE_SPECIAL_SESSIONS.get(today)
        doc.update(go=False, closed=True)
        doc["checks"].append({"check": "calendar", "ok": False,
                              "detail": ("a special session (%s): not traded" % special) if special else why})
        return doc
    add("calendar", True, "NSE session 9:15 to 15:30" + (", Nifty's weekly expiry" if check.expiry_day(d) else ""))
    c = dict(check.DEFAULTS)
    c.update(check.inr_keys(cfg or {}))
    if cfg is None:
        add("config", False, "trader/config.json does not read: copy trader-budget/config.example.json")
    elif not check._f(c.get("budget")):
        add("config", False, "no budget in trader/config.json")
    else:
        live = not c.get("paper", True)
        add("config", True, "%s account, ₹%s budget, %s max loss a trade" % (
            "LIVE" if live else "paper", check.indian_commas("%.0f" % check._f(c["budget"])),
            check.money(check._f(c.get("max_loss_per_trade_inr")) or 0)))
        if live:
            gate, why_ = check.live_gate(test, today)
            doc["notes"].append(("live trading open (trader-risk's live lock)" if gate else "live trading LOCKED: "
                                 + why_) + "; the autopilot trades paper only")
    if not test:
        add("rule", False, "no frozen rule (trader/forward.json): forward.py start --rule ...")
    else:
        r = test.get("rule") or {}
        if r.get("setup") not in edge.SETUPS or r.get("side") not in edge.SIDES:
            add("rule", False, "trader/forward.json has a rule edge.py does not know: %s" % json.dumps(r))
        elif r["side"] == "short" and not c.get("allow_short"):
            add("rule", False, "%s is a short and \"allow_short\" is off in trader/config.json"
                % forward.rule_text(test))
        else:
            add("rule", True, forward.rule_text(test))
    prev = check.previous_trading_day(d).isoformat()
    try:
        days = edge.sessions_of(index_text, edge.INDEX) if index_text else {}
    except edge.EdgeError:
        days = {}
    if not days:
        add("data", False, "no Nifty 50 bars from Yahoo: the feed is down or blocked; nothing can be traded blind")
    elif prev not in days:
        add("data", False, "Yahoo's last Nifty 50 session is %s, not %s: the feed is behind" % (max(days), prev))
    else:
        add("data", True, "Yahoo has Nifty 50 to %s" % prev)
    try:
        os.makedirs(state_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir, delete=True):
            pass
        add("files", True, "%s is writable" % state_dir)
    except OSError as e:
        add("files", False, "%s is not writable: %s" % (state_dir, e))
    left = []
    for leg in (state or {}).get("paper_legs", []):
        if leg.get("status") == "open" and leg.get("date", today) < today:
            left.append("an open paper %s on %s from %s" % ("stop and target", leg.get("symbol"), leg.get("date")))
    for pos in (portfolio or {}).get("positions", []):
        q = check._f(pos.get("shares", pos.get("qty"))) or 0
        if abs(q) > 1e-9:
            left.append("%s %s in the portfolio" % (ticket._str_num(q), pos.get("ticker") or pos.get("symbol")))
    opened = check.open_today(state, prev) if state else {}
    for sym, q in opened.items():
        left.append("%s %s not closed on %s" % (ticket._str_num(q), sym, prev))
    if left:
        add("leftovers", False, "; ".join(sorted(set(left))) + ". Close it and run ticket.py reconcile before trading")
    else:
        add("leftovers", True, "nothing carried over")
    rates = c.get("charges")
    max_loss = check._f(c.get("max_loss_per_trade_inr")) or 0
    dd = check.drawdown(state, check._f(c.get("budget")), rates) if state else None
    size_now, dd_note = check.max_loss_now(c, dd)
    week_from = (d - datetime.timedelta(days=d.weekday())).isoformat()
    month_from = d.replace(day=1).isoformat()
    recent = check.closed_trades(state, min(week_from, month_from), prev, rates) if state else []
    room = []
    for label, since, key in (("week", week_from, "max_weekly_loss_inr"), ("month", month_from, "max_monthly_loss_inr")):
        cap = check._f(c.get(key))
        if not cap:
            continue
        lost = max(-sum(t[2] for t in recent if t[0] >= since), 0)
        left_ = cap - lost
        room.append("%s %s of %s left" % (label, check.money(left_), check.money(cap)))
        if size_now and left_ < size_now - 0.005:
            add("limits", False, "the %s's loss limit has %s left, under one trade's %s max loss: no new trades "
                "until %s" % (label, check.money(max(left_, 0)), check.money(size_now),
                              "next Monday" if label == "week" else "next month"))
    if all(x["check"] != "limits" for x in doc["checks"]):
        add("limits", True, ", ".join(room) or "no weekly or monthly limit set")
    if dd_note:
        doc["notes"].append("drawdown: " + dd_note)
    ev = check.load_events(events_doc)
    market = [w for dd_, w, k in ev.get("MARKET", []) if dd_ == today and k == "market"]
    if market and c.get("skip_market_event_days"):
        add("events", False, "%s today: no new entries (skip_market_event_days)" % ", ".join(market))
    elif check.expiry_day(d) and c.get("skip_expiry_days"):
        add("events", False, "Nifty's weekly expiry and skip_expiry_days is on")
    else:
        mine = sorted("%s (%s)" % (s, w) for s, rows in ev.items() if s != "MARKET"
                      for dd_, w, k in rows if dd_ == today or (k == "results" and dd_ == prev))
        add("events", True, ("stocks off limits today: " + ", ".join(mine)) if mine else "no market event today")
    doc["notes"] += check.staleness(d)
    if surveillance_doc is None:
        doc["notes"].append("no ASM/GSM lists (trader/surveillance.json): scan.py surveillance")
    elif surveillance_doc.get("updated"):
        age = (d - datetime.date.fromisoformat(str(surveillance_doc["updated"])[:10])).days
        if age > check.STALE_SURVEILLANCE_DAYS:
            doc["notes"].append("the ASM/GSM lists are %d days old: refresh them (scan.py surveillance)" % age)
    doc["morning_limits"] = check.limits_snapshot(c)
    return doc


def lines(doc):
    if doc["closed"]:
        return ["%s: no session today (%s)." % (doc["date"], doc["checks"][0]["detail"])]
    L = ["Pre-flight %s at %s IST: %s" % (doc["date"], doc["checked_at"][11:], "GO" if doc["go"] else "NO-GO")]
    for x in doc["checks"]:
        L.append("  %-9s %-4s %s" % (x["check"], "ok" if x["ok"] else "FAIL", x["detail"]))
    for n in doc["notes"]:
        L.append("  note      " + n)
    if not doc["go"]:
        L.append("No new trades today until this is fixed and the pre-flight run again; open positions are still "
                 "managed.")
    return L


def run(now_utc, paths, offline=None, write=True):
    """Gather the inputs, run the checks, save the result and the morning limits. Returns the document."""
    cfg = forward.read(paths["config"], "config", required=False)
    test = forward.read(paths["forward"], "forward test", required=False)
    state = ticket.load_state(paths["state"]) if os.path.exists(paths["state"]) else None
    pf = forward.read(paths["portfolio"], "portfolio", required=False)
    idx = None
    try:
        idx = edge.read_offline(offline, edge.INDEX) if offline else edge.fetch(edge.INDEX, "5d")
    except edge.EdgeError:
        idx = None
    doc = run_checks(now_utc, cfg, test, state, pf, forward.read(paths["events"], "events", required=False),
                     forward.read(paths["surveillance"], "surveillance", required=False), idx,
                     os.path.dirname(paths["out"]) or ".")
    if write and not doc["closed"]:
        snap = doc.pop("morning_limits")
        forward.write(paths["out"], doc)
        lim = forward.read(paths["limits"], "the day's limits", required=False)
        if not lim or lim.get("date") != doc["date"]:
            forward.write(paths["limits"], {"date": doc["date"], "limits": snap})
    else:
        doc.pop("morning_limits", None)
    return doc


def todays(paths, today):
    """Today's saved pre-flight, or None."""
    doc = forward.read(paths["out"], "pre-flight", required=False)
    return doc if doc and doc.get("date") == today else None


def add_args(ap):
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--forward", default="trader/forward.json")
    ap.add_argument("--state", default="trader/state/broker.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--events", default="trader/events.json")
    ap.add_argument("--surveillance", default="trader/surveillance.json")
    ap.add_argument("--preflight", dest="out", default=STATE, help="where the result is saved")
    ap.add_argument("--limits-state", dest="limits", default=LIMITS, help="the day's limits (trader-risk)")


def paths_of(a):
    return {k: getattr(a, k) for k in ("config", "forward", "state", "portfolio", "events", "surveillance", "out",
                                       "limits")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pre-flight: GO or NO-GO before the open (trader-broker).")
    add_args(ap)
    ap.add_argument("--now", help="pretend it is this IST time")
    ap.add_argument("--offline", metavar="DIR", help="read ^NSEI from DIR instead of Yahoo")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        doc = run(check.parse_now(a.now), paths_of(a), a.offline, not a.no_write)
    except (forward.ForwardError, ticket.TicketError, check.RiskError) as e:
        print("preflight.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2) if a.json else "\n".join(lines(doc)))
    return 0 if doc["go"] or doc["closed"] else 1


if __name__ == "__main__":
    sys.exit(main())
