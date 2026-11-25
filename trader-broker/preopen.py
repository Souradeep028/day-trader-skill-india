#!/usr/bin/env python3
"""Pre-open snapshots for trader-broker: the prices an order placed in NSE's pre-open auction can see.

NSE's pre-open auction takes orders from 9:00 to 9:08, then fixes one price, the official open. A rule that sells
the stocks that "opened 2 % or more up" cannot know the official open when it orders: it has to decide on the
indicative equilibrium price (IEP) shown while orders are still taken, about 9:07. openclose.py's ten-year test
and the old gap-up shadow decided on the official open itself, which no order can see. This saves what you could
see, so gapshadow.py can decide on it and fill at the official open, as a real pre-open order would.

    python3 preopen.py save FILE                    # a snapshot saved between 9:00 and 9:08 -> trader/preopen/<date>.json
    python3 preopen.py save FILE --at 09:07:30      # when the file has no time of its own
    python3 preopen.py show [--date 2026-11-25]
    python3 preopen.py remind                       # exit 3 when a gap test needs today's snapshot and it is missing

FILE is any of:
  NSE       the JSON behind NSE's pre-open page for the Nifty 50, opened in your browser and saved as a file
            (nseindia.com/api/market-data-pre-open?key=NIFTY, after visiting nseindia.com once)
  Kite      the reply of Kite's get_quote for the stocks (ask Claude at 9:07: "get_quote for the Nifty 50 and save
            it"), {"data": {"NSE:INFY": {"last_price", "ohlc": {"close": previous close}, "timestamp"}}}
  CSV       symbol, price (or iep or last_price), and optionally prev_close and time, one stock a line
NSE's site turns scripts away, so this tool reads a file and never fetches one.

A snapshot counts only when it was taken from 9:00 to 9:08 IST on a trading day (later, the official open is
known: that is the look-ahead this exists to avoid). Each stock's gap is its price over the previous close (the
file's own, else gapshadow.py uses yesterday's close from the daily bars). Standard library only.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import forward  # noqa: E402  (the file handling)

DIR = os.path.join("trader", "preopen")
GAP_FILE = os.path.join("trader", "forward-gap.json")
OPENS = "09:00:00"
CLOSES = "09:08:00"                  # NSE stops taking pre-open orders at 9:08 (random close in the last minute)


class PreopenError(Exception):
    pass


def _num(x):
    try:
        v = float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _sym(s):
    s = str(s or "").strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if not s or not re.match(r"^[A-Z0-9&\-]{1,20}(\.NS)?$", s):
        return None
    return s if s.endswith(".NS") else s + ".NS"


def _when(text):
    """'25-Nov-2026 09:07:59', '2026-11-25 09:07:59', '2026-11-25T09:07:59+05:30' -> (date, 'HH:MM:SS') or None."""
    t = str(text or "").strip().replace("T", " ")
    t = re.sub(r"(\+05:?30|Z)$", "", t).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            d = datetime.datetime.strptime(t.split(".")[0], fmt)
            return d.date().isoformat(), d.strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def parse(text):
    """Any of the three formats -> {"format", "stocks": {SYM.NS: {"price", "prev_close"}}, "when": (date, time) or
    None}. The latest time any row carries is the snapshot's."""
    text = text.lstrip("﻿")
    stocks, times, fmt = {}, [], None
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("data"), list):
        fmt = "nse"
        for row in doc["data"]:
            md = (row or {}).get("metadata") or {}
            po = ((row or {}).get("detail") or {}).get("preOpenMarket") or {}
            sym = _sym(md.get("symbol"))
            price = next((v for v in (_num(md.get("iep")), _num(po.get("IEP")), _num(md.get("lastPrice")),
                                      _num(po.get("finalPrice"))) if v), None)
            if sym and price:
                stocks[sym] = {"price": price, "prev_close": _num(md.get("previousClose")) or _num(po.get("prevClose"))}
            w = _when(po.get("lastUpdateTime"))
            if w:
                times.append(w)
        w = _when(doc.get("timestamp"))
        if w:
            times.append(w)
    elif isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        fmt = "kite"
        for key, q in doc["data"].items():
            q = q or {}
            sym = _sym(key)
            price = _num(q.get("last_price"))
            if sym and price:
                stocks[sym] = {"price": price, "prev_close": _num((q.get("ohlc") or {}).get("close"))}
            w = _when(q.get("timestamp") or q.get("last_trade_time"))
            if w:
                times.append(w)
    elif doc is None:
        fmt = "csv"
        rows = list(csv.reader(io.StringIO(text)))
        head = [h.strip().lower() for h in (rows[0] if rows else [])]
        col = lambda *names: next((head.index(n) for n in names if n in head), None)  # noqa: E731
        si, pi = col("symbol", "sym", "tradingsymbol"), col("price", "iep", "last_price", "ltp")
        ci, ti = col("prev_close", "previous_close", "close"), col("time", "timestamp")
        if si is None or pi is None:
            raise PreopenError("the CSV needs a symbol and a price (or iep) column; it has %s" % ", ".join(head))
        for r in rows[1:]:
            if len(r) <= max(si, pi):
                continue
            sym, price = _sym(r[si]), _num(r[pi])
            if sym and price:
                stocks[sym] = {"price": price, "prev_close": _num(r[ci]) if ci is not None and len(r) > ci else None}
            if ti is not None and len(r) > ti:
                w = _when(r[ti])
                if w:
                    times.append(w)
    if not stocks:
        raise PreopenError("no stock prices in the file (NSE pre-open JSON, a Kite get_quote reply, or a CSV)")
    return {"format": fmt, "stocks": stocks, "when": max(times) if times else None}


def snapshot(text, source, date=None, at=None):
    """The snapshot document. date and at (HH:MM or HH:MM:SS, IST) stand in when the file has no time."""
    got = parse(text)
    when = got["when"]
    if when is None:
        if not at:
            raise PreopenError("the file says nothing about when it was taken: give --at HH:MM:SS (IST), the time "
                               "you saved it")
        when = (date or ist_today(), at if at.count(":") == 2 else at + ":00")
    elif date and when[0] != date:
        raise PreopenError("the file is from %s, not %s" % (when[0], date))
    stocks = {}
    for sym, v in sorted(got["stocks"].items()):
        pc = v.get("prev_close")
        stocks[sym] = {"price": v["price"], "prev_close": pc,
                       "gap_pct": round(100.0 * (v["price"] / pc - 1), 3) if pc else None}
    return {"created_by": "trader-broker preopen.py", "date": when[0], "time": when[1], "source": source,
            "format": got["format"], "stocks": stocks}


def usable(doc):
    """(ok, why): a snapshot counts only when taken while the auction still took orders, 9:00 to 9:08 IST."""
    if not doc:
        return False, "no pre-open snapshot (preopen.py save)"
    t = str(doc.get("time") or "")
    if not t:
        return False, "the snapshot has no time"
    if t > CLOSES:
        return False, "the snapshot was taken at %s, after the auction closed at 9:08: the open was already known" % t
    if t < OPENS:
        return False, "the snapshot was taken at %s, before the auction opened at 9:00: no indicative prices yet" % t
    return True, ""


def path_for(folder, day):
    return os.path.join(folder, "%s.json" % day)


def load(folder, day):
    p = path_for(folder, day)
    if not os.path.exists(p):
        return None
    return forward.read(p, "pre-open snapshot")


def ist_today():
    return (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            + datetime.timedelta(hours=5, minutes=30)).date().isoformat()


def wants_snapshots(gap_file):
    """A gap-up test is frozen here and decides on pre-open snapshots."""
    if not os.path.exists(gap_file):
        return False
    try:
        return (forward.read(gap_file, "gap test") or {}).get("decide_on") == "snapshot"
    except forward.ForwardError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pre-open snapshots: what a pre-open order can see (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("save", help="save a snapshot taken from 9:00 to 9:08")
    p.add_argument("file")
    p.add_argument("--at", help="HH:MM[:SS] IST, when the file has no time of its own")
    p.add_argument("--date", help="YYYY-MM-DD (default: the file's, else today in India)")
    p.add_argument("--dir", default=DIR)
    p = sub.add_parser("show", help="a saved snapshot: the stocks 2 % or more up")
    p.add_argument("--date")
    p.add_argument("--dir", default=DIR)
    p = sub.add_parser("remind", help="exit 3 when the gap test needs today's snapshot and there is none")
    p.add_argument("--date")
    p.add_argument("--dir", default=DIR)
    p.add_argument("--gap-file", default=GAP_FILE)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "save":
            with open(a.file, encoding="utf-8", errors="replace") as fh:
                doc = snapshot(fh.read(), os.path.basename(a.file), a.date, a.at)
            ok, why = usable(doc)
            if not ok:
                raise PreopenError("not saved: %s" % why)
            forward.write(path_for(a.dir, doc["date"]), doc)
            up = sorted(((v["gap_pct"], s) for s, v in doc["stocks"].items() if (v["gap_pct"] or 0) >= 2.0),
                        reverse=True)
            print("Saved %s at %s IST: %d stocks (%s), %d of them 2 %% or more up%s." % (
                doc["date"], doc["time"], len(doc["stocks"]), doc["format"], len(up),
                (": " + ", ".join("%s %+.2f %%" % (s, g) for g, s in up[:5])) if up else ""))
            return 0
        day = a.date or ist_today()
        doc = load(a.dir, day)
        if a.cmd == "remind":
            if not wants_snapshots(a.gap_file):
                print("No gap-up test deciding on snapshots here: nothing to save.")
                return 0
            if usable(doc)[0]:
                print("Today's pre-open snapshot is saved (%s IST)." % doc["time"])
                return 0
            print("%s: save the pre-open snapshot between 9:00 and 9:08 (preopen.py save FILE), or the gap-up test "
                  "skips today." % day)
            return 3
        if doc is None:
            raise PreopenError("no snapshot for %s in %s" % (day, a.dir))
        ok, why = usable(doc)
        print("%s at %s IST (%s, %s): %d stocks.%s" % (doc["date"], doc["time"], doc["format"], doc["source"],
                                                         len(doc["stocks"]), "" if ok else " NOT USABLE: " + why))
        for s, v in sorted(doc["stocks"].items(), key=lambda kv: -(kv[1]["gap_pct"] or -99)):
            if (v["gap_pct"] or 0) >= 2.0:
                print("  %-14s %10.2f  %+.2f %%" % (s, v["price"], v["gap_pct"]))
        return 0
    except (PreopenError, OSError, forward.ForwardError) as e:
        print("preopen.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
