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


def raw_rows(folder):
    """{sym: [[epoch, o, h, l, c, v]]} for every .csv or .json file in the folder, as stored."""
    out = {}
    for f in sorted(os.listdir(folder)):
        base = f[:-3] if f.endswith(".gz") else f
        if base.endswith(".csv"):
            sym = base[:-4].replace("%5E", "^")
            opener = gzip.open if f.endswith(".gz") else open
            with opener(os.path.join(folder, f), "rt", encoding="utf-8") as fh:
                out[sym] = read_csv(fh.read(), sym)
        elif base.endswith(".json"):
            sym = base[:-5].replace("%5E", "^")
            r = json.loads(edge.read_offline(folder, sym))["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            rows = []
            for i, t in enumerate(r.get("timestamp") or []):
                vals = [q[k][i] for k in ("open", "high", "low", "close")]
                if t is None or None in vals:
                    continue
                rows.append([int(t)] + [float(x) for x in vals] + [q.get("volume", [0] * (i + 1))[i] or 0])
            out[sym] = rows
    return out


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
            or parts[0] not in edge.SETUPS + edge.EXTRA_SETUPS or not parts[3].upper().endswith("R"):
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


def run_research(charts, minutes, hypotheses, cfg=None, members=None, holdout_from=None, final=False, exclude=None):
    """exclude: {(sym, date)} sessions the data checker refused (check_data)."""
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
        sess = [x for x in sess if member_on(members, s, x["date"]) and (s, x["date"]) not in (exclude or ())]
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
        # break-even slippage: R falls about linearly with the bp, so the line through 0 bp and the tested bp
        r0 = summary(edge.rerun(prepared, rule, filt, dict(c, slippage_bp=0.0)))["avg_R"]
        r1, bp1 = pooled["avg_R"], float(c["slippage_bp"])
        if r0 is None or r0 <= 0:
            be = None                                          # loses even with no slippage
        elif r1 is not None and bp1 > 0 and r1 < r0:
            be = round(bp1 * r0 / (r0 - r1), 1)
        else:
            be = None
        rows.append({"name": h.get("name") or h["rule"], "rule": h["rule"], "why": h.get("why", ""),
                     "avg_R_at_0bp": r0, "break_even_bp": be,
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
           "survivorship": members is None, "hypotheses": n_h, "excluded_sessions": len(exclude or ()), "significance": float("%.3g" % bar),
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
    if doc.get("excluded_sessions"):
        L.append("Left out: %d stock-sessions the data check refused (check)." % doc["excluded_sessions"])
    L += ["", "%-30s %7s %8s %9s %9s %7s %9s  %s" % ("hypothesis", "trades", "avg R", "baseline", "break-even",
                                                   "folds+", "p", "")]
    for r in doc["rows"]:
        a, b = r["all"], r["baseline"]
        be = r.get("break_even_bp")
        L.append("%-30s %7d %8s %9s %9s %7s %9s  %s" % (
            r["name"][:30], a["trades"], "%+.3f" % a["avg_R"] if a["avg_R"] is not None else "n/a",
            "%+.3f" % b["avg_R"] if b["avg_R"] is not None else "n/a",
            ("%.1f bp" % be) if be is not None else ("none" if (r.get("avg_R_at_0bp") or 0) <= 0 else "> tested"),
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
          "under %s (0.05 shared across %d), money in two thirds of the folds, and beating its baseline. Break-even: "
          "the slippage at which it stops making money (none: it loses even at 0 bp); one under your measured "
          "slippage is not worth trading. "
          "A past edge is not a promise. Not financial advice." % (doc["significance"], doc["hypotheses"])]
    return L


# ---------------------------------------------------------------- the data check

SPLIT_RATIOS = (0.5, 1 / 3.0, 0.25, 0.2, 0.1, 2.0, 3.0, 5.0, 10.0)


def check_data(rows_by_sym, members=None, holidays=None):
    """Faults in the bars before any test: -> {"symbols": {sym: {...}}, "bad": {(sym, date): reason}}.
    Refused sessions: an overnight jump that looks like an unadjusted split or bonus (the price halves, or
    a fifth), bad prints (a bar 8 % away from the bars on both sides), prices that do not add up (high under
    low, zero or less), and sessions with a fifth of their bars missing. Reported only: duplicate times, bars
    outside 9:15 to 15:30, weekend and known-holiday sessions, and member stocks with no file."""
    holidays = holidays if holidays is not None else set()
    out, bad = {}, {}
    for sym, rows in sorted(rows_by_sym.items()):
        info = {"bars": len(rows), "duplicates": 0, "outside": 0, "sessions": 0, "short_sessions": 0,
                "split_like": [], "bad_prints": 0, "broken": 0, "closed_days": []}
        seen, by_day = set(), {}
        step = interval_of(rows) if rows else 300
        for r in rows:
            if r[0] in seen:
                info["duplicates"] += 1
                continue
            seen.add(r[0])
            local = EPOCH + datetime.timedelta(seconds=r[0], hours=5, minutes=30)
            m = local.hour * 60 + local.minute
            if not edge.OPEN_MIN <= m < 15 * 60 + 30:
                info["outside"] += 1
                continue
            by_day.setdefault(local.date().isoformat(), []).append(r)
        per_day = 375 * 60 / float(step) if step else 75
        days = sorted(by_day)
        info["sessions"] = len(days)
        prev_close = None
        for d in days:
            rs_ = by_day[d]
            wd = datetime.date.fromisoformat(d).weekday()
            if wd >= 5 or d in holidays:
                info["closed_days"].append(d)
            if len(rs_) < 0.8 * per_day:
                info["short_sessions"] += 1
                bad.setdefault((sym, d), "%d of about %d bars" % (len(rs_), per_day))
            for r in rs_:
                if min(r[1:5]) <= 0 or r[2] < r[3] or not r[3] <= r[1] <= r[2] or not r[3] <= r[4] <= r[2]:
                    info["broken"] += 1
                    bad.setdefault((sym, d), "a bar whose prices do not add up")
            for a, b, c in zip(rs_, rs_[1:], rs_[2:]):
                ref = (a[4] + c[1]) / 2.0
                if ref > 0 and max(abs(b[2] / ref - 1), abs(b[3] / ref - 1)) > 0.08 \
                        and abs(c[1] / a[4] - 1) < 0.02:
                    info["bad_prints"] += 1
                    bad.setdefault((sym, d), "a bad print at %s" % (EPOCH + datetime.timedelta(
                        seconds=b[0], hours=5, minutes=30)).strftime("%H:%M"))
            if prev_close:
                jump = rs_[0][1] / prev_close
                if abs(jump - 1) > 0.25:
                    near = min(SPLIT_RATIOS, key=lambda x: abs(jump / x - 1))
                    if abs(jump / near - 1) < 0.08:
                        info["split_like"].append("%s (x%.2f)" % (d, jump))
                        bad[(sym, d)] = ("opened at %.2f x the last close: an unadjusted split or bonus? adjust "
                                         "the prices before %s" % (jump, d))
            prev_close = rs_[-1][4]
        out[sym] = info
    missing = sorted(s for s in (members or {}) if s not in rows_by_sym)
    return {"symbols": out, "bad": bad, "missing_members": missing}


def check_lines(chk):
    L = ["Data check: %d files, %d stock-sessions refused." % (len(chk["symbols"]), len(chk["bad"]))]
    for sym, i in chk["symbols"].items():
        notes = []
        for k, label in (("duplicates", "duplicate times"), ("outside", "bars outside 9:15-15:30"),
                         ("short_sessions", "short sessions"), ("bad_prints", "bad prints"),
                         ("broken", "broken bars")):
            if i[k]:
                notes.append("%d %s" % (i[k], label))
        if i["split_like"]:
            notes.append("split-like jumps on " + ", ".join(i["split_like"][:5]))
        if i["closed_days"]:
            notes.append("%d sessions on a weekend or NSE holiday (%s)" % (len(i["closed_days"]),
                                                                          ", ".join(i["closed_days"][:3])))
        L.append("  %-14s %6d sessions  %s" % (sym, i["sessions"], "; ".join(notes) if notes else "clean"))
    if chk["missing_members"]:
        L.append("Members with no file: %s." % ", ".join(chk["missing_members"][:20]))
    return L


# ---------------------------------------------------------------- the power test

def synthetic(n_stocks, n_sessions, drift_bp, seed=1, start="2023-01-02", vol_bp=15.0):
    """5-minute charts of made-up stocks with ONE planted edge: once a bar closes below the first 30 minutes'
    range low, every later bar of that day drifts drift_bp basis points down. Otherwise a random walk with
    vol_bp a bar (about 1.3 % a day), on a 0.05 tick. -> {sym: chart text}, and the index flat."""
    import random
    rng = random.Random(seed)
    day = datetime.date.fromisoformat(start)
    dates = []
    while len(dates) < n_sessions:
        if day.weekday() < 5:
            dates.append(day)
        day += datetime.timedelta(days=1)
    charts = {}
    for i in range(n_stocks):
        p0 = 1000.0 * (1 + 0.5 * rng.random())
        px, rows = p0, []
        for d in dates:
            base = int((datetime.datetime.combine(d, datetime.time(9, 15)) - EPOCH).total_seconds()) - 330 * 60
            # the overnight move, pulled back a little toward the start: without it the planted drift walks the
            # prices down for years and the edge changes size with them (the test needs it the same throughout)
            px *= (1 + rng.gauss(0, 0.006)) * (p0 / px) ** 0.05
            lo, broken = None, False
            for k in range(75):
                o = px
                c = o * (1 + rng.gauss(0, vol_bp / 1e4) - (drift_bp / 1e4 if broken else 0.0))
                h = max(o, c) * (1 + abs(rng.gauss(0, vol_bp / 2e4)))
                l = min(o, c) * (1 - abs(rng.gauss(0, vol_bp / 2e4)))
                o, h, l, c = (round(round(x / 0.05) * 0.05, 2) for x in (o, h, l, c))
                h, l = max(h, o, c), min(l, o, c)
                rows.append([base + 300 * k, o, h, l, c, 1000 + rng.randrange(1000)])
                if k < 6:
                    lo = l if lo is None else min(lo, l)
                elif c < lo:
                    broken = True
                px = c
        charts["S%02d.NS" % i] = json.dumps(archive.to_chart("S%02d.NS" % i, rows, "5m"))
    return charts


POWER_HYPOTHESES = [
    {"name": "planted: orb30 short", "rule": "orb30 short wide 2R low_cost", "why": "the planted edge"},
    {"name": "null: orb30 long", "rule": "orb30 long wide 2R low_cost", "why": "no edge planted"},
    {"name": "null: pdb long", "rule": "pdb long wide 2R low_cost", "why": "no edge planted"},
]


def power(n_stocks=10, n_sessions=250, drift_bp=1.0, reps=20, fold_sessions=60, slippage_bp=3.0, bootstrap=300,
          seed=1):
    """Can the harness find a planted edge, and does it pass edges that are not there? The planted rule's
    true average R is measured on one sample 8 times larger; then `reps` fresh samples of this size are tested."""
    cfg = {"fold_sessions": fold_sessions, "slippage_bp": slippage_bp, "bootstrap": bootstrap}
    big = run_research(synthetic(n_stocks * 8, n_sessions, drift_bp, seed=seed + 999), {}, POWER_HYPOTHESES[:1],
                       dict(cfg, bootstrap=50))
    true_r = big["rows"][0]["all"]["avg_R"]
    planted = nulls = picks = runs = 0
    for i in range(reps):
        doc = run_research(synthetic(n_stocks, n_sessions, drift_bp, seed=seed + i), {}, POWER_HYPOTHESES, cfg)
        runs += 1
        planted += doc["rows"][0]["passed"]
        nulls += sum(r["passed"] for r in doc["rows"][1:])
        ps = doc["selection"]["picks"]
        picks += sum(1 for p in ps if p["picked"] == POWER_HYPOTHESES[0]["name"]) / float(len(ps)) if ps else 0
    trades = doc["rows"][0]["all"]["trades"]
    return {"stocks": n_stocks, "sessions": n_sessions, "drift_bp": drift_bp, "reps": runs,
            "planted_true_avg_R": true_r, "planted_trades_a_run": trades,
            "planted_pass_pct": round(100.0 * planted / runs, 1),
            "null_pass_pct": round(100.0 * nulls / (runs * (len(POWER_HYPOTHESES) - 1)), 1),
            "selection_picks_planted_pct": round(100.0 * picks / runs, 1)}


def power_lines(p):
    return ["Power test: %d made-up stocks x %d sessions, %d runs. The planted rule makes %s R a trade after charges "
            "and slippage (measured on a sample 8 times larger), about %d trades a run." % (
                p["stocks"], p["sessions"], p["reps"], "%+.3f" % p["planted_true_avg_R"]
                if p["planted_true_avg_R"] is not None else "n/a", p["planted_trades_a_run"]),
            "  planted edge passed: %.0f %% of runs (the test's power)" % p["planted_pass_pct"],
            "  rules with no edge passed: %.1f %% (false alarms; the bar is 5 %% shared across the rules)"
            % p["null_pass_pct"],
            "  the walk-forward selection picked the planted rule in %.0f %% of folds" % p["selection_picks_planted_pct"],
            "Under about 80 % power, a real edge this size is likely to be missed with this much data: buy more "
            "sessions or stocks, or test fewer hypotheses."]


# ---------------------------------------------------------------- NSE's daily files (bhavcopy)

def read_bhavcopy(text):
    """One NSE equity bhavcopy (the old format, SYMBOL/SERIES/OPEN..., or the UDiFF one since July 2024,
    TckrSymb/SctySrs/OpnPric...) -> {(symbol, series): (date, open, high, low, close, prev_close, volume)}.
    NSE's OPEN is the pre-open auction price; its PREVCLOSE is adjusted for the day's corporate actions."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = [h.strip().upper() for h in rows[0]]
    old = "SYMBOL" in head
    names = (("SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP") if old else
             ("TCKRSYMB", "SCTYSRS", "OPNPRIC", "HGHPRIC", "LWPRIC", "CLSPRIC", "PRVSCLSGPRIC", "TTLTRADGVOL",
              "TRADDT"))
    if not all(n in head for n in names):
        raise ResearchError("not an NSE equity bhavcopy: the header is %s" % ", ".join(head[:12]))
    ix = [head.index(n) for n in names]
    out = {}
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        sym, ser, o, h, l, c, pc, v, dt = (r[i].strip() for i in ix)
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d%m%Y"):
            try:
                d = datetime.datetime.strptime(dt.title() if fmt == "%d-%b-%Y" else dt, fmt).date().isoformat()
                break
            except ValueError:
                continue
        else:
            raise ResearchError("cannot read the bhavcopy date %r" % dt)
        vals = [_num(x) for x in (o, h, l, c, pc)]
        if None in vals:
            continue
        out[(sym.upper(), ser.upper())] = (d,) + tuple(vals) + (_num(v) or 0,)
    return out


def bhavcopy_charts(folder, series=("EQ",)):
    """Every bhavcopy in a folder -> {SYM.NS: daily chart text}, back-adjusted for splits, bonuses and other
    corporate actions with NSE's own adjusted previous close: where a day's PREVCLOSE differs from the last
    CLOSE, every earlier bar is scaled by their ratio. -> (charts, {sym: [(date, factor)]})."""
    bars = {}
    for f in sorted(os.listdir(folder)):
        base = f[:-3] if f.endswith(".gz") else f
        if not base.lower().endswith(".csv"):
            continue
        opener = gzip.open if f.endswith(".gz") else open
        with opener(os.path.join(folder, f), "rt", encoding="utf-8", errors="replace") as fh:
            for (sym, ser), row in read_bhavcopy(fh.read()).items():
                if ser in series:
                    bars.setdefault(sym, {})[row[0]] = row
    charts, actions = {}, {}
    for sym, days in bars.items():
        ds = sorted(days)
        factor, adj = 1.0, {}
        for i in range(len(ds) - 1, -1, -1):              # newest first: each action scales everything before it
            d, o, h, l, c, pc, v = days[ds[i]]
            adj[d] = (o * factor, h * factor, l * factor, c * factor, v / factor if factor else v)
            if i and pc:
                last_close = days[ds[i - 1]][4]
                ratio = pc / last_close if last_close else 1.0
                if abs(ratio - 1) > 0.005:
                    factor *= ratio
                    actions.setdefault(sym + ".NS", []).append((d, round(ratio, 4)))
        rows = []
        for d in ds:
            t = int((datetime.datetime.combine(datetime.date.fromisoformat(d), datetime.time(9, 15)) - EPOCH)
                    .total_seconds()) - 330 * 60
            o, h, l, c, v = adj[d]
            rows.append([t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v)])
        charts[sym + ".NS"] = json.dumps(archive.to_chart(sym + ".NS", rows, "1d"))
    return charts, actions


# ---------------------------------------------------------------- the research plan

PLAN = os.path.join("trader", "research-plan.json")


def freeze_plan(path, hypotheses, settings, today):
    """Write the plan down before the data is seen: the hypotheses themselves, the fills, the folds, the
    holdout and the code that will judge them."""
    import hashlib
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "trader-broker"))
    import forward
    body = json.dumps(hypotheses, sort_keys=True).encode("utf-8")
    doc = {"created_by": "trader-timemachine research.py plan", "frozen": today, "hypotheses": hypotheses,
           "hypotheses_sha": hashlib.sha256(body).hexdigest()[:12], "settings": settings,
           "code": forward.code_version()}
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return doc


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


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="A few hypotheses, walk-forward, honest fills (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="test the hypotheses walk-forward")
    p.add_argument("--data", required=True, metavar="DIR", help="<SYMBOL>.csv[.gz] (1- or 5-minute) or .json[.gz]")
    p.add_argument("--symbols", help="only these, comma separated (the index and VIX always)")
    p.add_argument("--members", help="symbol,from,to CSV: point-in-time index membership")
    p.add_argument("--hypotheses", default=HYPOTHESES)
    p.add_argument("--plan", help="a frozen plan (research.py plan): its hypotheses and settings, nothing else")
    p.add_argument("--slippage-bp", type=float, default=3.0)
    p.add_argument("--latency-min", type=int, default=1, help="minutes after the signal bar closes (1-minute data)")
    p.add_argument("--fold-sessions", type=int, default=FOLD_SESSIONS)
    p.add_argument("--max-loss", type=float, default=500.0)
    p.add_argument("--holdout-from", metavar="DATE")
    p.add_argument("--final", action="store_true", help="include the holdout (recorded in trader/holdout.json)")
    p.add_argument("--holdout-file", default=HOLDOUT)
    p.add_argument("--no-check", action="store_true", help="test every session, even those the data check refuses")
    p.add_argument("--bootstrap", type=int, default=edge.BOOTSTRAP)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/research.json", metavar="PATH")
    p = sub.add_parser("check", help="find the data's faults before any test")
    p.add_argument("--data", required=True, metavar="DIR")
    p.add_argument("--members")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("power", help="can the test find a planted edge? (made-up data, no download)")
    p.add_argument("--stocks", type=int, default=10)
    p.add_argument("--sessions", type=int, default=250)
    p.add_argument("--drift-bp", type=float, default=1.0, help="the planted drift a bar after a breakdown")
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--fold-sessions", type=int, default=FOLD_SESSIONS)
    p.add_argument("--slippage-bp", type=float, default=3.0)
    p.add_argument("--bootstrap", type=int, default=300)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("bhavcopy", help="NSE's daily files -> adjusted daily charts for openclose.py --offline")
    p.add_argument("--data", required=True, metavar="DIR", help="NSE equity bhavcopy CSVs, old or UDiFF format")
    p.add_argument("--out", required=True, metavar="DIR")
    p.add_argument("--series", default="EQ")
    p = sub.add_parser("plan", help="freeze the hypotheses and settings before seeing the data")
    p.add_argument("--hypotheses", default=HYPOTHESES)
    p.add_argument("--slippage-bp", type=float, default=3.0)
    p.add_argument("--latency-min", type=int, default=1)
    p.add_argument("--fold-sessions", type=int, default=FOLD_SESSIONS)
    p.add_argument("--holdout-from", metavar="DATE")
    p.add_argument("--file", default=PLAN)
    p.add_argument("--replace", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd not in ("run", "check", "power", "bhavcopy", "plan"):
        ap.print_help()
        return 1
    today = datetime.date.today().isoformat()
    try:
        if a.cmd == "check":
            members = read_members(a.members) if a.members else None
            chk = check_data(raw_rows(a.data), members, set(edge.tradercore.holidays()))
            if a.json:
                print(json.dumps({"symbols": chk["symbols"], "missing_members": chk["missing_members"],
                                  "bad": ["%s %s: %s" % (k[0], k[1], v) for k, v in sorted(chk["bad"].items())]},
                                 indent=1))
            else:
                print("\n".join(check_lines(chk)))
                for (sym, d), why in sorted(chk["bad"].items())[:40]:
                    print("  refused %s %s: %s" % (sym, d, why))
            return 0
        if a.cmd == "power":
            res = power(a.stocks, a.sessions, a.drift_bp, a.reps, a.fold_sessions, a.slippage_bp, a.bootstrap)
            print(json.dumps(res, indent=1) if a.json else "\n".join(power_lines(res)))
            return 0
        if a.cmd == "bhavcopy":
            charts, actions = bhavcopy_charts(a.data, tuple(x.strip().upper() for x in a.series.split(",")))
            os.makedirs(a.out, exist_ok=True)
            for sym, text in charts.items():
                with open(os.path.join(a.out, sym + ".json"), "w", encoding="utf-8") as fh:
                    fh.write(text)
            print("Wrote %d daily charts to %s, adjusted for %d corporate actions (NSE's own previous closes). "
                  "Next: openclose.py run --offline %s --auction" % (len(charts), a.out,
                                                                     sum(len(v) for v in actions.values()), a.out))
            return 0
        if a.cmd == "plan":
            if os.path.exists(a.file) and not a.replace:
                raise ResearchError("a plan is already frozen in %s: --replace starts a new one (and says so)" % a.file)
            with open(a.hypotheses, encoding="utf-8") as fh:
                hyps = json.load(fh)
            for h in hyps:
                parse_rule(h["rule"])
            doc = freeze_plan(a.file, hyps, {"slippage_bp": a.slippage_bp, "latency_min": a.latency_min,
                                             "fold_sessions": a.fold_sessions, "holdout_from": a.holdout_from}, today)
            print("Frozen on %s: %d hypotheses (%s), %g bp, folds of %d sessions, holdout from %s, code %s. Run with "
                  "--plan %s; nothing in it can change without a new, dated plan." % (
                      today, len(hyps), doc["hypotheses_sha"], a.slippage_bp, a.fold_sessions,
                      a.holdout_from or "none", doc["code"], a.file))
            return 0
        notes = []
        if a.plan:
            with open(a.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
            hyps, st = plan["hypotheses"], plan["settings"]
            a.slippage_bp, a.latency_min, a.fold_sessions = st["slippage_bp"], st["latency_min"], st["fold_sessions"]
            a.holdout_from = st.get("holdout_from")
            notes.append("Plan frozen %s (%s): its hypotheses and settings." % (plan["frozen"], plan["hypotheses_sha"]))
        else:
            with open(a.hypotheses, encoding="utf-8") as fh:
                hyps = json.load(fh)
            if isinstance(hyps, dict):
                hyps = hyps.get("hypotheses", [])
            notes.append("No frozen plan (research.py plan): fine for exploring, but a result is only a test if the "
                         "hypotheses were written down before the data was seen.")
        charts, minutes, _ = load_folder(a.data, [s.strip().upper() for s in a.symbols.split(",")]
                                         if a.symbols else None)
        members = read_members(a.members) if a.members else None
        exclude = None if a.no_check else set(check_data(raw_rows(a.data), members)["bad"])
        warn = None
        if a.final and a.holdout_from:
            warn = holdout_note(a.holdout_file, a.holdout_from, today)
        doc = run_research(charts, minutes, hyps, {"slippage_bp": a.slippage_bp, "latency_min": a.latency_min,
                                                   "fold_sessions": a.fold_sessions, "max_loss": a.max_loss,
                                                   "bootstrap": a.bootstrap},
                           members, a.holdout_from, a.final, exclude)
    except (ResearchError, edge.EdgeError, OSError, ValueError, KeyError) as e:
        print("research.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        _write(a.write, doc)
    print(json.dumps(doc, indent=1) if a.json else "\n".join(report(doc) + [""] + notes))
    if warn:
        print("\n" + warn)
    return 0

if __name__ == "__main__":
    sys.exit(main())
