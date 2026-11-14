#!/usr/bin/env python3
"""Research harness for trader-timemachine: a few rules written down in advance, tested walk-forward on years of
bars with honest fills. Where edge.py searches 1,536 combinations on Yahoo's 60 days, this tests ~10 named
hypotheses, each with a reason someone would pay it, on data you bring. Standard library only.

    python3 research.py run --data DIR                       # every <SYMBOL>.csv in DIR, the example hypotheses
    python3 research.py run --data DIR --members members.csv --hypotheses my.json --slippage-bp 4
    python3 research.py run --data DIR --holdout-from 2026-01-01          # keep the last year untouched
    python3 research.py run --data DIR --holdout-from 2026-01-01 --final  # look at it, once (it is recorded)
    python3 research.py run --data tests/fixtures/trader-timemachine/edge --fold-sessions 20   # Yahoo JSON works too

Data (--data DIR). One file a symbol, <SYMBOL>.csv or .csv.gz (INFY.NS.csv; the index as ^NSEI.csv or
%5ENSEI.csv), with a header row. Columns, any case: a timestamp (datetime, timestamp, or date and time), open,
high, low, close, volume. Times without an offset are India time; epoch seconds or milliseconds work too. Bars of
1 minute (a data vendor's) are rolled up into the 5-minute grid from 9:15, and their minute opens are kept for
the fills. 5-minute bars and edge.py's Yahoo JSON files (<SYMBOL>.json[.gz]) are read as they are.

Point-in-time membership (--members FILE): a CSV of symbol,from,to (to may be empty: still a member). A stock
counts only on sessions it was in the index, so the test is not run on today's survivors. Without it the report
says the result carries survivorship bias.

Hypotheses (--hypotheses FILE, default hypotheses.example.json next to this file): a JSON list of
{"name", "rule", "why"}, the rule in edge.py's words ("orb30 short wide 2R low_cost"). Write them down BEFORE
looking at the data, with the reason someone pays you; the fewer, the less the luck correction costs.

Fills, always honest here: with 1-minute data an entry meets the open of the minute --latency-min (1) after the
signal bar closed, the stop only counting in that 5-minute bar; with 5-minute data, the next bar's open. Then
--slippage-bp (3) on every market fill, targets only when the price trades a tick through them, Zerodha's
charges. Use the paper autopilot's measured slippage (daily report, "Entry slippage") once it has 30 trades.

Walk-forward. The sessions are cut, in time order, into folds of --fold-sessions (60, about a quarter). Each
hypothesis is judged on every fold, and it PASSES only when, after charges and slippage:
  - the pooled average R is above 0 with a session-bootstrap p under 0.05 / the number of hypotheses,
  - it made money in at least two thirds of the folds (3 folds or more), and
  - it beats its baseline (every stock, every session, the same side and stop width, in at 9:30).
And the selection itself is tested: before each fold, pick the hypothesis with the best average R so far and
trade only it in that fold. If that loses, picking winners from the past did not work here, however good the
best hypothesis looks on the whole.

Holdout (--holdout-from DATE): sessions from DATE are left out. --final puts them in and records it in
trader/holdout.json; the next --final says the holdout has already been seen and is no longer untouched.
A past edge is not a promise. Not financial advice.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(os.path.dirname(HERE), "trader-broker"))
import edge  # noqa: E402  (the trade logic, charges, fills, bootstrap and baseline)
import archive  # noqa: E402  (bars -> the Yahoo chart format edge.py reads)

HYPOTHESES = os.path.join(HERE, "hypotheses.example.json")
HOLDOUT = os.path.join("trader", "holdout.json")
FOLD_SESSIONS = 60
MIN_FOLD_TRADES = 10
EPOCH = datetime.datetime(1970, 1, 1)


class ResearchError(Exception):
    pass


# ---------------------------------------------------------------- data

def _num(x):
    x = str(x).strip().replace(",", "")
    return float(x) if x not in ("", "-", "null", "None", "nan") else None


def parse_time(text):
    """A timestamp -> epoch seconds (UTC). India time when the text has no offset."""
    t = str(text).strip()
    if t.replace(".", "", 1).isdigit():
        v = float(t)
        return int(v / 1000 if v > 1e11 else v)
    t = t.replace("T", " ").replace("Z", "+00:00")
    off = None
    for sign in ("+", "-"):
        i = t.rfind(sign)
        if i > 10 and ":" in t[i:]:
            hh, mm = t[i + 1:].split(":")[:2]
            off = (1 if sign == "+" else -1) * (int(hh) * 60 + int(mm))
            t = t[:i].strip()
            break
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y%m%d %H:%M:%S",
                "%Y%m%d %H:%M"):
        try:
            local = datetime.datetime.strptime(t.split(".")[0], fmt)
            break
        except ValueError:
            continue
    else:
        raise ResearchError("cannot read the time %r" % text)
    off = 330 if off is None else off
    return int((local - EPOCH).total_seconds()) - off * 60


def read_csv(text, sym):
    """CSV text -> [[epoch, o, h, l, c, v]] in time order."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ResearchError("%s: an empty file" % sym)
    head = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None
    ts, d, tm = col("datetime", "timestamp", "date_time", "time_stamp"), col("date"), col("time")
    o, h, l, c, v = col("open"), col("high"), col("low"), col("close"), col("volume", "vol")
    if None in (o, h, l, c) or (ts is None and d is None):
        raise ResearchError("%s: the header needs a timestamp (or date and time), open, high, low and close; it "
                            "has %s" % (sym, ", ".join(head)))
    out = []
    for r in rows[1:]:
        if not r or len(r) < len(head):
            continue
        when = r[ts] if ts is not None else (r[d] + (" " + r[tm] if tm is not None else ""))
        vals = [_num(r[i]) for i in (o, h, l, c)]
        if None in vals:
            continue
        out.append([parse_time(when)] + vals + [_num(r[v]) or 0 if v is not None else 0])
    out.sort()
    return out


def interval_of(rows):
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:]) if 0 < b[0] - a[0] <= 3600]
    return statistics.median(gaps) if gaps else 300


def roll_up(rows, minutes=5):
    """1-minute rows -> 5-minute rows on the grid from 9:15 India time (each bar's open time), and the minute
    opens {date: {minute of the day: open}} for the fills."""
    step = minutes * 60
    bars, opens = {}, {}
    for t, o, h, l, c, v in rows:
        local = EPOCH + datetime.timedelta(seconds=t, hours=5, minutes=30)
        m = local.hour * 60 + local.minute
        if not edge.OPEN_MIN <= m < 15 * 60 + 30:
            continue
        opens.setdefault(local.date().isoformat(), {})[m] = o
        start = t - (t - (edge.OPEN_MIN - 330) * 60) % step
        b = bars.get(start)
        if b is None:
            bars[start] = [start, o, h, l, c, v]
        else:
            b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
    return [bars[k] for k in sorted(bars)], opens


def load_folder(folder, symbols=None):
    """-> ({sym: chart text}, {sym: minute opens or None}, kind) for every file in the folder."""
    charts, minutes, kinds = {}, {}, set()
    for f in sorted(os.listdir(folder)):
        base = f[:-3] if f.endswith(".gz") else f
        if base.endswith(".csv"):
            sym = base[:-4].replace("%5E", "^")
            opener = gzip.open if f.endswith(".gz") else open
            with opener(os.path.join(folder, f), "rt", encoding="utf-8") as fh:
                rows = read_csv(fh.read(), sym)
            if not rows:
                continue
            if interval_of(rows) < 300:
                five, opens = roll_up(rows)
                minutes[sym] = opens
                kinds.add("1m")
            else:
                five, opens = rows, None
                kinds.add("5m")
            charts[sym] = json.dumps(archive.to_chart(sym, five, "5m"))
        elif base.endswith(".json"):
            sym = base[:-5].replace("%5E", "^")
            charts[sym] = edge.read_offline(folder, sym)
            minutes[sym] = None
            kinds.add("5m")
    if symbols:
        want = set(symbols) | {edge.INDEX, edge.VIX}
        charts = {s: t for s, t in charts.items() if s in want}
    if not charts:
        raise ResearchError("no .csv or .json bars in %s" % folder)
    return charts, minutes, "1m" if kinds == {"1m"} else ("mixed" if len(kinds) > 1 else "5m")


def read_members(path):
    """symbol,from,to CSV -> {sym: [(from, to or None)]}."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    head = [h.strip().lower() for h in rows[0]]
    if not {"symbol", "from"} <= set(head):
        raise ResearchError("%s needs the columns symbol, from, to" % path)
    out = {}
    for r in rows[1:]:
        if not r:
            continue
        d = dict(zip(head, [x.strip() for x in r]))
        out.setdefault(d["symbol"].upper(), []).append((d["from"], d.get("to") or None))
    return out


def member_on(members, sym, day):
    if members is None:
        return True
    return any(f <= day and (t is None or day <= t) for f, t in members.get(sym, []))


# ---------------------------------------------------------------- the test

def parse_rule(text):
    parts = str(text).split()
    if len(parts) not in (4, 5) or parts[1] not in edge.SIDES or parts[2] not in edge.STOPS \
            or parts[0] not in edge.SETUPS or not parts[3].upper().endswith("R"):
        raise ResearchError("a rule is 'SETUP SIDE STOP TARGET [FILTER]' in edge.py's words, got %r" % text)
    filt = parts[4] if len(parts) == 5 else "none"
    for f in filt.split("+"):
        if f not in edge.SINGLE:
            raise ResearchError("unknown filter %r in %r" % (f, text))
    return {"setup": parts[0], "side": parts[1], "stop": parts[2], "target_r": float(parts[3][:-1])}, filt


def folds_of(dates, size):
    ds = sorted(dates)
    out = [ds[i:i + size] for i in range(0, len(ds), size)]
    if len(out) > 1 and len(out[-1]) < size / 2.0:
        tail = out.pop()
        out[-1] = out[-1] + tail
    return out


def summary(ts):
    if not ts:
        return {"trades": 0, "avg_R": None}
    return {"trades": len(ts), "avg_R": round(statistics.mean(t["R"] for t in ts), 3),
            "net": round(sum(t["pnl"] for t in ts), 2)}


def run_research(charts, minutes, hypotheses, cfg=None, members=None, holdout_from=None, final=False):
    c = {"max_loss": 500.0, "max_order": 75000.0, "slippage_ticks": 1, "no_entries_before": "09:30",
         "no_entries_after": "14:45", "flat_by": "15:10", "max_cost_pct": edge.MAX_COST_PCT,
         "slippage_bp": 3.0, "latency_min": 1, "fold_sessions": FOLD_SESSIONS, "bootstrap": edge.BOOTSTRAP}
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    c["first_slot"] = edge.hhmm_slot(c["no_entries_before"], "no_entries_before")
    c["last_entry_slot"] = edge.hhmm_slot(c["no_entries_after"], "no_entries_after")
    c["flat_slot"] = edge.hhmm_slot(c["flat_by"], "flat_by")
    c["target_through"] = 1
    index_days = edge.sessions_of(charts[edge.INDEX], edge.INDEX) if edge.INDEX in charts else {}
    vix_days = edge.sessions_of(charts[edge.VIX], edge.VIX) if edge.VIX in charts else {}
    stocks = {s: t for s, t in charts.items() if s not in (edge.INDEX, edge.VIX)}
    per = {}
    for s, t in stocks.items():
        try:
            per[s] = edge.sessions_of(t, s)
        except edge.EdgeError:
            continue
    if not per:
        raise ResearchError("no usable stock bars")
    dates = set()
    for days in per.values():
        dates |= {d for d, b in days.items() if all(k in b for k in range(c["flat_slot"] + 1))}
    if index_days:
        dates &= set(index_days)
    held_out = sorted(d for d in dates if holdout_from and d >= holdout_from)
    if not final:
        dates -= set(held_out)
    if len(dates) < 20:
        raise ResearchError("only %d full sessions to test on" % len(dates))
    all_days = set(index_days)
    for d in per.values():
        all_days |= set(d)
    c["dates"] = dates
    c["expiry"] = edge.expiry_dates(all_days)
    prepared = {}
    for s, days in per.items():
        sess = edge.prepare(days, index_days, vix_days, c)
        sess = [x for x in sess if member_on(members, s, x["date"])]
        for x in sess:
            x["minutes"] = (minutes.get(s) or {}).get(x["date"]) if minutes.get(s) else None
        prepared[s] = sess
    one_min = any(x.get("minutes") for v in prepared.values() for x in v)
    if not one_min:
        c["entry_delay"] = 1                     # 5-minute data: the next bar's open
        c["latency_min"] = 0
    folds = folds_of(dates, int(c["fold_sessions"]))
    fold_of = {d: i for i, f in enumerate(folds) for d in f}
    n_h = len(hypotheses)
    bar = 0.05 / max(n_h, 1)
    rows = []
    for h in hypotheses:
        rule, filt = parse_rule(h["rule"])
        ts = edge.rerun(prepared, rule, filt, c)
        bl = edge.baseline_trades(ts, prepared, c, rule["target_r"])
        by_fold = [summary([t for t in ts if fold_of.get(t["date"]) == i]) for i in range(len(folds))]
        judged = [f for f in by_fold if f["trades"] >= MIN_FOLD_TRADES]
        up = sum(1 for f in judged if f["avg_R"] > 0)
        pooled = summary(ts)
        bs = edge.bootstrap(ts, c["bootstrap"]) if len(ts) >= 30 and pooled["avg_R"] > 0 else None
        base = summary(bl)
        checks = {
            "positive": bool(pooled["avg_R"] is not None and pooled["avg_R"] > 0),
            "significant": bool(bs and bs["p"] < bar),
            "most_folds": bool(len(judged) >= 3 and up >= 2.0 * len(judged) / 3),
            "beats_baseline": bool(pooled["avg_R"] is not None and base["avg_R"] is not None
                                   and pooled["avg_R"] > base["avg_R"]),
        }
        rows.append({"name": h.get("name") or h["rule"], "rule": h["rule"], "why": h.get("why", ""),
                     "all": pooled, "folds": by_fold, "folds_up": up, "folds_judged": len(judged),
                     "bootstrap": bs, "baseline": base, "checks": checks, "passed": all(checks.values()),
                     "_trades": ts})
    # the selection, walk-forward: before each fold, the best average R so far; trade only it in the fold
    picks = []
    for i in range(1, len(folds)):
        best, best_r = None, None
        for r in rows:
            past = [t for t in r["_trades"] if fold_of.get(t["date"], -1) < i]
            if len(past) >= 30:
                m = statistics.mean(t["R"] for t in past)
                if best_r is None or m > best_r:
                    best, best_r = r, m
        if best is None:
            continue
        got = [t for t in best["_trades"] if fold_of.get(t["date"]) == i]
        picks.append({"fold": i, "picked": best["name"], "past_avg_R": round(best_r, 3), **summary(got),
                      "_trades": got})
    sel = [t for p in picks for t in p["_trades"]]
    doc = {"created_by": "trader-timemachine research.py", "symbols": len(prepared), "sessions": len(dates),
           "from": min(dates), "to": max(dates), "data": "1-minute" if one_min else "5-minute",
           "survivorship": members is None, "hypotheses": n_h, "significance": float("%.3g" % bar),
           "folds": [{"from": f[0], "to": f[-1], "sessions": len(f)} for f in folds],
           "holdout": {"from": holdout_from, "sessions": len(held_out), "included": bool(final)} if holdout_from
           else None,
           "settings": {k: c[k] for k in ("slippage_bp", "latency_min", "slippage_ticks", "max_loss", "max_order",
                                          "fold_sessions", "no_entries_before", "no_entries_after", "flat_by")},
           "rows": [{k: v for k, v in r.items() if k != "_trades"} for r in rows],
           "selection": {"picks": [{k: v for k, v in p.items() if k != "_trades"} for p in picks],
                         "all": summary(sel)}}
    doc["verdict"] = verdict(doc)
    return doc


def verdict(doc):
    passed = [r for r in doc["rows"] if r["passed"]]
    sel = doc["selection"]["all"]
    tail = []
    if doc["survivorship"]:
        tail.append("No --members file: these are today's stocks over the whole period, which flatters long rules.")
    if doc["data"] != "1-minute":
        tail.append("5-minute data: entries at the next bar's open, a 5-minute delay, harsher than a real order "
                    "and coarser.")
    if sel["trades"] and sel["avg_R"] is not None and sel["avg_R"] <= 0:
        tail.append("Picking the best hypothesis so far before each fold LOST (%+.3f R over %d trades): choosing "
                    "winners from the past did not carry forward here." % (sel["avg_R"], sel["trades"]))
    if passed:
        head = ("%d of %d hypotheses passed: after charges and %g bp of slippage they made money by more than luck, "
                "in most folds, and beat the baseline: %s. Next: freeze one (forward.py start) and let the sequential "
                "gate judge it going forward." % (len(passed), doc["hypotheses"], doc["settings"]["slippage_bp"],
                                                  ", ".join(r["name"] for r in passed)))
    else:
        head = ("None of the %d hypotheses passed after charges and %g bp of slippage, walk-forward over %d sessions: "
                "no tested edge here. The index fund is the default." % (doc["hypotheses"],
                                                                          doc["settings"]["slippage_bp"],
                                                                          doc["sessions"]))
    return " ".join([head] + tail)


def report(doc):
    s = doc["settings"]
    L = ["Research: %d hypotheses, %d stocks, %d sessions (%s to %s), %s data, %d folds of about %d sessions." % (
        doc["hypotheses"], doc["symbols"], doc["sessions"], doc["from"], doc["to"], doc["data"], len(doc["folds"]),
        s["fold_sessions"]),
        "Fills: %s, %g bp of slippage on market fills, a tick more, targets traded through; Zerodha's charges; "
        "%s max loss a trade." % ("the open of the minute %d after the signal bar closes" % s["latency_min"]
                                  if doc["data"] == "1-minute" else "the next 5-minute bar's open",
                                  s["slippage_bp"], edge.money(s["max_loss"]))]
    if doc.get("holdout"):
        h = doc["holdout"]
        L.append("Holdout from %s (%d sessions): %s." % (h["from"], h["sessions"], "INCLUDED (--final)"
                                                          if h["included"] else "left out, untouched"))
    L += ["", "%-30s %7s %8s %9s %7s %9s  %s" % ("hypothesis", "trades", "avg R", "baseline", "folds+", "p", "")]
    for r in doc["rows"]:
        a, b = r["all"], r["baseline"]
        L.append("%-30s %7d %8s %9s %7s %9s  %s" % (
            r["name"][:30], a["trades"], "%+.3f" % a["avg_R"] if a["avg_R"] is not None else "n/a",
            "%+.3f" % b["avg_R"] if b["avg_R"] is not None else "n/a",
            "%d/%d" % (r["folds_up"], r["folds_judged"]),
            r["bootstrap"]["p"] if r["bootstrap"] else "-",
            "PASSED" if r["passed"] else "failed: " + ", ".join(k.replace("_", " ") for k, v in r["checks"].items()
                                                                if not v)))
    L += ["", "By fold (average R; a dot under %d trades):" % MIN_FOLD_TRADES,
          "%-30s " % "from" + " ".join("%8s" % f["from"][2:] for f in doc["folds"])]
    for r in doc["rows"]:
        L.append("%-30s " % r["name"][:30] + " ".join(
            "%+8.2f" % f["avg_R"] if f["trades"] >= MIN_FOLD_TRADES else "%8s" % "." for f in r["folds"]))
    sel = doc["selection"]
    if sel["picks"]:
        L += ["", "The selection, walk-forward (before each fold, the best hypothesis so far):"]
        for p in sel["picks"]:
            L.append("  fold from %s: %s (%+.3f R so far) -> %d trades, %s R" % (
                doc["folds"][p["fold"]]["from"], p["picked"], p["past_avg_R"], p["trades"],
                "%+.3f" % p["avg_R"] if p["avg_R"] is not None else "n/a"))
        a = sel["all"]
        L.append("  in all: %d trades, %s R a trade." % (a["trades"], "%+.3f" % a["avg_R"]
                                                        if a["avg_R"] is not None else "n/a"))
    L += ["", doc["verdict"], "A hypothesis passes only with all four: pooled average R above 0, a session-bootstrap p "
          "under %s (0.05 shared across %d), money in two thirds of the folds, and beating its baseline. "
          "A past edge is not a promise. Not financial advice." % (doc["significance"], doc["hypotheses"])]
    return L


# ---------------------------------------------------------------- command line

def holdout_note(path, frm, today):
    doc = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    seen = doc.get(frm) or []
    warn = ("The holdout from %s was already looked at on %s: it is no longer untouched, and a rule chosen after "
            "seeing it is not tested by it." % (frm, ", ".join(seen))) if seen else None
    doc[frm] = seen + [today]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return warn


def main(argv=None):
    ap = argparse.ArgumentParser(description="A few hypotheses, walk-forward, honest fills (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--data", required=True, metavar="DIR", help="<SYMBOL>.csv[.gz] (1- or 5-minute) or .json[.gz]")
    p.add_argument("--symbols", help="only these, comma separated (the index and VIX always)")
    p.add_argument("--members", help="symbol,from,to CSV: point-in-time index membership")
    p.add_argument("--hypotheses", default=HYPOTHESES)
    p.add_argument("--slippage-bp", type=float, default=3.0)
    p.add_argument("--latency-min", type=int, default=1, help="minutes after the signal bar closes (1-minute data)")
    p.add_argument("--fold-sessions", type=int, default=FOLD_SESSIONS)
    p.add_argument("--max-loss", type=float, default=500.0)
    p.add_argument("--holdout-from", metavar="DATE")
    p.add_argument("--final", action="store_true", help="include the holdout (recorded in trader/holdout.json)")
    p.add_argument("--holdout-file", default=HOLDOUT)
    p.add_argument("--bootstrap", type=int, default=edge.BOOTSTRAP)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/research.json", metavar="PATH")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 1
    try:
        with open(a.hypotheses, encoding="utf-8") as fh:
            hyps = json.load(fh)
        if isinstance(hyps, dict):
            hyps = hyps.get("hypotheses", [])
        charts, minutes, _ = load_folder(a.data, [s.strip().upper() for s in a.symbols.split(",")]
                                         if a.symbols else None)
        members = read_members(a.members) if a.members else None
        warn = None
        if a.final and a.holdout_from:
            warn = holdout_note(a.holdout_file, a.holdout_from, datetime.date.today().isoformat())
        doc = run_research(charts, minutes, hyps, {"slippage_bp": a.slippage_bp, "latency_min": a.latency_min,
                                                   "fold_sessions": a.fold_sessions, "max_loss": a.max_loss,
                                                   "bootstrap": a.bootstrap},
                           members, a.holdout_from, a.final)
    except (ResearchError, edge.EdgeError, OSError, ValueError) as e:
        print("research.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    print(json.dumps(doc, indent=1) if a.json else "\n".join(report(doc)))
    if warn:
        print("\n" + warn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
