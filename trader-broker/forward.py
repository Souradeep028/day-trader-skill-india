#!/usr/bin/env python3
"""Forward test for trader-broker: prove a rule on trades taken AFTER it was frozen. Standard library only.

A backtest picks the best of many tries, so it flatters. The only honest test is a rule written down in
advance, traded on every signal it gives (on paper first), and judged by limits set before the first trade.

    python3 forward.py start --rule "orb30 short tight 2R low_cost+with_nifty"   # from trader/edge.json
    python3 forward.py start --rule "orb15 long tight 2R none" --expect-r 0.05 --expect-low -0.10
    python3 forward.py signal INFY.NS --taken                     # every signal the rule gives, taken or not
    python3 forward.py signal TCS.NS --skipped --reason "results tomorrow"
    python3 forward.py status                                     # the trades since the start against the plan

start   freezes the rule in trader/forward.json with what the backtest expects: its average R after
        charges and the low end of its 95 % bootstrap range (edge.py --write saved both), or --expect-r
        and --expect-low. And the stop rules, fixed now, not after a bad week:
          --min-trades 40     no verdict before 40 trades
          --max-drawdown-r 8  stop if the trades since the start are ever 8 R under their best
          the verdict at --min-trades: STOP when the average R is under the backtest's low end, or 0 or
          less; PASSED when it is above both
signal  logs each signal of the rule: --taken or --skipped (with --reason). Taking some and skipping others
        quietly changes the rule; the status says how many were skipped and why.
status  pairs the broker log's fills (trader/state/broker.json, paper by default) since the start into round
        trips (ticket.py journal's), and prints: trades, win rate, average R, total R, the worst run in R,
        the verdict, and the trades that went off the plan: a loss bigger than 1.25 R (a stop moved, or a
        fill far past it), no stop recorded (no R), or a trade on a day and stock with no logged signal.

A forward test is evidence, not proof: 40 trades still leave a wide range. Not financial advice.
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ticket  # noqa: E402  (this folder's broker log reader)

MIN_TRADES = 40
MAX_DRAWDOWN_R = 8.0
OFF_PLAN_R = -1.25
EARLY = 10


class ForwardError(Exception):
    pass


def parse_rule(text):
    """'orb30 short tight 2R low_cost+with_nifty' -> edge.py's rule key and filter."""
    parts = str(text or "").split()
    if len(parts) not in (4, 5):
        raise ForwardError("--rule is 'SETUP SIDE STOP TARGET [FILTER]', like 'orb30 short tight 2R low_cost+with_nifty'")
    setup, side, stop, target = parts[:4]
    m = re.match(r"^([0-9]*\.?[0-9]+)R$", target, re.I)
    if side not in ("long", "short") or stop not in ("tight", "wide") or not m:
        raise ForwardError("--rule: side is long or short, stop tight or wide, target like 2R (got %r)" % text)
    return ({"setup": setup, "side": side, "stop": stop, "target_r": float(m.group(1))},
            parts[4] if len(parts) == 5 else "none")


def from_edge(doc, rule, filt):
    for r in (doc or {}).get("rows", []):
        if r.get("rule") == rule and r.get("filter") == filt:
            a = r.get("all") or {}
            bs = r.get("bootstrap") or {}
            low = (bs.get("ci95") or [None])[0]
            return {"avg_R": a.get("avg_R"), "low": low, "trades": a.get("trades"),
                    "in_sample": (r.get("in_sample") or {}).get("avg_R"),
                    "out_of_sample": (r.get("out_of_sample") or {}).get("avg_R"),
                    "source": "trader/edge.json, %s sessions" % doc.get("sessions")}
    raise ForwardError("no row for that rule and filter in the edge report: run edge.py run --write first, "
                       "or give --expect-r and --expect-low")


def read(path, what, required=True):
    if not os.path.exists(path):
        if required:
            raise ForwardError("no %s at %s" % (what, path))
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError as e:
            raise ForwardError("%s is not valid JSON: %s" % (path, e))


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def evaluate(test, trades):
    """Pure function: the frozen test plus the round trips since its start -> the status document."""
    exp = test["expected"]
    rules = test["stop_rules"]
    rs = [t["R"] for t in trades if t.get("R") is not None]
    cum = peak = worst = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    n = len(trades)
    avg = round(sum(rs) / len(rs), 3) if rs else None
    sig = test.get("signals", [])
    taken = [s for s in sig if s.get("taken")]
    skipped = [s for s in sig if not s.get("taken")]
    logged = {(s["date"], s["symbol"]) for s in taken}
    off = []
    for t in trades:
        if t.get("R") is None:
            off.append("%s %s: no stop recorded with its yes, so no R" % (t["date"], t["symbol"]))
        elif t["R"] < OFF_PLAN_R:
            off.append("%s %s: lost %.2f R, more than the planned 1 R (a stop moved, or filled far past it)"
                       % (t["date"], t["symbol"], -t["R"]))
        if sig and (t["date"], t["symbol"]) not in logged:
            off.append("%s %s: no logged signal of this rule that day: not part of the test"
                       % (t["date"], t["symbol"]))
    low = exp.get("low")
    floor = max(low if low is not None else 0.0, 0.0)
    if worst >= rules["max_drawdown_R"] - 1e-9:
        verdict, state = ("STOP: the trades since the start fell %.1f R from their best, past the %.0f R limit "
                          "set at the start" % (worst, rules["max_drawdown_R"])), "stop"
    elif n < EARLY:
        verdict, state = "Too early: %d trade%s of %d. Keep taking every signal." % (
            n, "" if n == 1 else "s", rules["min_trades"]), "running"
    elif n < rules["min_trades"]:
        verdict, state = ("Running: %d of %d trades, average %+.2f R against %+.2f R expected%s. No verdict "
                          "before %d." % (n, rules["min_trades"], avg or 0, exp.get("avg_R") or 0,
                                          " (under the backtest's low end so far)" if low is not None and (avg or 0) < low
                                          else "", rules["min_trades"])), "running"
    elif avg is None or avg <= 0 or (low is not None and avg < low):
        verdict, state = ("STOP: after %d trades the average is %+.2f R, %s. The rule did not hold up going "
                          "forward: drop it, do not re-tune it on these same trades."
                          % (n, avg or 0, "0 or less" if (avg or 0) <= 0 else "under the backtest's low end %+.2f R"
                             % low)), "stop"
    else:
        verdict, state = ("PASSED: after %d trades the average is %+.2f R, above 0 and above the backtest's low "
                          "end%s. Evidence, not proof: if you go live, start at half size and keep the journal."
                          % (n, avg, (" %+.2f R" % low) if low is not None else "")), "passed"
    return {"rule": test["rule"], "filter": test["filter"], "started": test["started"], "trades": n,
            "with_R": len(rs), "wins": sum(1 for t in trades if t["net"] > 0),
            "avg_R": avg, "total_R": round(sum(rs), 2), "worst_drawdown_R": round(worst, 2),
            "net": round(sum(t["net"] for t in trades), 2), "expected": exp, "floor": floor,
            "signals": {"taken": len(taken), "skipped": len(skipped),
                        "skip_reasons": sorted({s.get("reason") or "no reason" for s in skipped})},
            "off_plan": off, "state": state, "verdict": verdict}


def rule_text(test):
    r = test["rule"]
    return "%s %s %s %gR %s" % (r["setup"], r["side"], r["stop"], r["target_r"], test["filter"])


def cmd_start(a):
    if os.path.exists(a.file) and not a.replace:
        raise ForwardError("a forward test is already running in %s (%s). Finish it, or --replace to start a "
                           "new one: replacing it throws its record away" % (a.file, rule_text(read(a.file, "test"))))
    rule, filt = parse_rule(a.rule)
    if a.expect_r is not None:
        exp = {"avg_R": a.expect_r, "low": a.expect_low, "source": "given by hand"}
    else:
        exp = from_edge(read(a.edge, "edge report (edge.py run --write)"), rule, filt)
    today = a.today or datetime.date.today().isoformat()
    test = {"created_by": "trader-broker forward.py", "rule": rule, "filter": filt, "started": today,
            "account": "live" if a.live else "paper", "expected": exp,
            "stop_rules": {"min_trades": a.min_trades, "max_drawdown_R": a.max_drawdown_r}, "signals": []}
    write(a.file, test)
    print("Frozen on %s: %s (%s account)." % (today, rule_text(test), test["account"]))
    print("Expected: %s R a trade after charges%s (%s)." % (
        "%+.2f" % exp["avg_R"] if exp.get("avg_R") is not None else "unknown",
        (", low end %+.2f R" % exp["low"]) if exp.get("low") is not None else "", exp.get("source")))
    print("Stop rules, fixed now: no verdict before %d trades; STOP if the trades fall %.0f R from their best; "
          "at %d, STOP under the low end or at 0 R or less." % (a.min_trades, a.max_drawdown_r, a.min_trades))
    print("Log every signal: forward.py signal SYMBOL.NS --taken (or --skipped --reason ...).")
    return 0


def cmd_signal(a):
    test = read(a.file, "forward test (forward.py start first)")
    if a.taken == a.skipped:
        raise ForwardError("say --taken or --skipped")
    sym = a.symbol.upper()
    if not sym.endswith(".NS"):
        sym += ".NS"
    rec = {"date": a.date or datetime.date.today().isoformat(), "symbol": sym, "taken": bool(a.taken)}
    if a.reason:
        rec["reason"] = a.reason
    if a.skipped and not a.reason:
        raise ForwardError("a skipped signal needs --reason: the reasons are how you find out you are "
                           "trading a different rule")
    test["signals"].append(rec)
    write(a.file, test)
    print("Logged: %s %s on %s%s." % (sym, "taken" if a.taken else "skipped", rec["date"],
                                      (" (%s)" % a.reason) if a.reason else ""))
    return 0


def cmd_status(a):
    test = read(a.file, "forward test (forward.py start first)")
    cfg = read(a.config, "config", required=False) or {}
    state = ticket.load_state(a.state)
    paper = test.get("account", "paper") == "paper"
    trades, still_open = ticket.round_trips(state, test["started"], a.today, paper=paper, rates=cfg.get("charges"))
    res = evaluate(test, trades)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0 if res["state"] != "stop" else 1
    print("Forward test: %s, %s account, since %s." % (rule_text(test), test.get("account", "paper"), test["started"]))
    exp = res["expected"]
    print("  %d trade%s (%d won), average %s R, total %+.2f R, worst run %.2f R, net %s."
          % (res["trades"], "" if res["trades"] == 1 else "s", res["wins"],
             "%+.2f" % res["avg_R"] if res["avg_R"] is not None else "n/a", res["total_R"],
             res["worst_drawdown_R"], ("-" if res["net"] < 0 else "+") + ticket._money(abs(res["net"]))))
    print("  The backtest expected %s R%s." % ("%+.2f" % exp["avg_R"] if exp.get("avg_R") is not None else "?",
                                               (", low end %+.2f R" % exp["low"]) if exp.get("low") is not None else ""))
    sg = res["signals"]
    if sg["taken"] or sg["skipped"]:
        print("  Signals: %d taken, %d skipped%s." % (sg["taken"], sg["skipped"],
                                                     (" (" + "; ".join(sg["skip_reasons"]) + ")") if sg["skipped"] else ""))
        total = sg["taken"] + sg["skipped"]
        if total and sg["skipped"] > 0.2 * total:
            print("  You skipped more than a fifth of the signals: this tests your picks, not the rule.")
    for o in res["off_plan"]:
        print("  Off plan: " + o)
    if still_open:
        print("  Still open: " + ", ".join("%s %s" % (x["date"], x["symbol"]) for x in still_open))
    print(res["verdict"])
    print("A forward test is evidence, not proof. Not financial advice.")
    return 0 if res["state"] != "stop" else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forward test a frozen rule (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start", help="freeze a rule, its expected R and its stop rules")
    p.add_argument("--rule", required=True, help="'orb30 short tight 2R low_cost+with_nifty' (edge.py's names)")
    p.add_argument("--edge", default="trader/edge.json")
    p.add_argument("--expect-r", type=float, help="the expected average R, when there is no edge report row")
    p.add_argument("--expect-low", type=float, help="the low end of its range")
    p.add_argument("--min-trades", type=int, default=MIN_TRADES)
    p.add_argument("--max-drawdown-r", type=float, default=MAX_DRAWDOWN_R)
    p.add_argument("--live", action="store_true", help="test the live account's trades (default: paper)")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("signal", help="log a signal of the rule, taken or skipped")
    p.add_argument("symbol")
    p.add_argument("--taken", action="store_true")
    p.add_argument("--skipped", action="store_true")
    p.add_argument("--reason")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p = sub.add_parser("status", help="the trades since the start against the plan")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.add_argument("--file", default="trader/forward.json")
        sp.add_argument("--today", help="YYYY-MM-DD (default today)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"start": cmd_start, "signal": cmd_signal, "status": cmd_status}[a.cmd](a)
    except (ForwardError, ticket.TicketError) as e:
        print("forward.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
