#!/usr/bin/env python3
"""Bar archive for trader-timemachine: keep the intraday bars Yahoo forgets.

Yahoo keeps 5-minute bars for 60 days and 1-minute bars for about a week. Every intraday test in this pack
(edge.py, the forward test's shadow) is limited to what Yahoo still has. Saved once a day, the bars add up:
after a year there are a year of sessions to test on instead of 60 days. Standard library only.

    python3 archive.py save --watchlist trader/nifty50.txt           # after the close: every finished session
    python3 archive.py save --watchlist trader/nifty50.txt --interval 1m
    python3 archive.py export --to /tmp/bars5                        # one Yahoo-style file a symbol, for
    python3 edge.py run --offline /tmp/bars5 --watchlist trader/nifty50.txt     # edge.py --offline and the rest
    python3 archive.py status

The archive: trader/bars/<interval>/<YYYY-MM-DD>.json.gz, one file per session holding every symbol's bars of
that session ({symbol: [[unix time, open, high, low, close, volume], ...]}). A session's file is written once,
when the session is over (after 15:30 India time), and never rewritten, so each day adds one small file. Nifty
50 (^NSEI) and India VIX (^INDIAVIX) are always included. --offline DIR reads the charts from files instead.
"""

import argparse
import datetime
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge  # noqa: E402  (Yahoo fetch, offline files, the symbol list)

ROOT = "trader/bars"
RANGES = {"5m": "60d", "1m": "7d"}
EXTRA = (edge.INDEX, edge.VIX)
CLOSE_IST = datetime.time(15, 30)


class ArchiveError(Exception):
    pass


def ist_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def bars_by_day(text, sym):
    """Chart JSON -> {IST date: [[t, o, h, l, c, v], ...]} for bars inside 9:15 to 15:30, in time order."""
    try:
        res = json.loads(text)["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise ArchiveError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        return {}
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out = {}
    for i, t in enumerate(r.get("timestamp") or []):
        vals = [(q.get(k) or [None] * (i + 1))[i] if i < len(q.get(k) or []) else None
                for k in ("open", "high", "low", "close", "volume")]
        if t is None or None in vals[:4]:
            continue
        local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t), hours=5, minutes=30)
        m = local.hour * 60 + local.minute
        if not (9 * 60 + 15 <= m < 15 * 60 + 30):
            continue
        out.setdefault(local.date().isoformat(), []).append(
            [int(t)] + [round(float(v), 2) for v in vals[:4]] + [int(vals[4] or 0)])
    for d in out:
        out[d].sort()
    return out


def day_path(root, interval, day):
    return os.path.join(root, interval, "%s.json.gz" % day)


def save(charts, interval, root=ROOT, now=None):
    """charts {sym: chart text}. Writes every finished session not yet in the archive. -> ([dates written],
    [dates already there])."""
    now = now or ist_now()
    per_day = {}
    for sym, text in charts.items():
        try:
            days = bars_by_day(text, sym)
        except ArchiveError:
            continue
        for d, rows in days.items():
            per_day.setdefault(d, {})[sym] = rows
    written, kept = [], []
    today = now.date().isoformat()
    for d in sorted(per_day):
        if d > today or (d == today and now.time() < CLOSE_IST):
            continue                                       # the session is not over: its bars can still change
        p = day_path(root, interval, d)
        if os.path.exists(p):
            kept.append(d)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"interval": interval, "date": d, "bars": per_day[d]}, fh, separators=(",", ":"))
        os.replace(tmp, p)
        written.append(d)
    return written, kept


def days_in(root, interval):
    d = os.path.join(root, interval)
    if not os.path.isdir(d):
        return []
    return sorted(f[:10] for f in os.listdir(d) if f.endswith(".json.gz"))


def read_day(root, interval, day):
    with gzip.open(day_path(root, interval, day), "rt", encoding="utf-8") as fh:
        return json.load(fh)["bars"]


def to_chart(sym, rows, interval):
    """[[t, o, h, l, c, v], ...] -> a Yahoo v8 chart answer, which edge.py and the other tools read."""
    rows = sorted(rows)
    q = {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
         "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}
    return {"chart": {"result": [{"meta": {"symbol": sym, "dataGranularity": interval, "source": "archive.py"},
                                  "timestamp": [r[0] for r in rows], "indicators": {"quote": [q]}}],
                      "error": None}}


def export(root, interval, to, start=None, end=None):
    """Every archived session (from start to end) as <SYMBOL>.json in `to`. -> {sym: bars}."""
    rows = {}
    for d in days_in(root, interval):
        if (start and d < start) or (end and d > end):
            continue
        for sym, rs in read_day(root, interval, d).items():
            rows.setdefault(sym, []).extend(rs)
    os.makedirs(to, exist_ok=True)
    for sym, rs in rows.items():
        with open(os.path.join(to, sym.replace("^", "%5E") + ".json"), "w", encoding="utf-8") as fh:
            json.dump(to_chart(sym, rs, interval), fh, separators=(",", ":"))
    return {s: len(r) for s, r in rows.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keep the intraday bars Yahoo forgets (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("save", "export", "status"):
        p = sub.add_parser(name)
        p.add_argument("--interval", choices=sorted(RANGES), default="5m")
        p.add_argument("--root", default=ROOT)
        if name == "save":
            p.add_argument("--watchlist", default="trader/nifty50.txt")
            p.add_argument("--symbols")
            p.add_argument("--offline", metavar="DIR")
            p.add_argument("--now", help="pretend it is this IST time, YYYY-MM-DD HH:MM")
        if name == "export":
            p.add_argument("--to", required=True)
            p.add_argument("--from", dest="start")
            p.add_argument("--until", dest="end")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "save":
            syms = edge.symbols_from(argparse.Namespace(symbols=a.symbols, watchlist=a.watchlist)) + list(EXTRA)
            charts, failed = {}, []
            for i, s in enumerate(syms):
                try:
                    if a.offline:
                        charts[s] = edge.read_offline(a.offline, s)
                    else:
                        if i:
                            time.sleep(edge.PAUSE)
                        charts[s] = edge.fetch(s, RANGES[a.interval], a.interval)
                except edge.EdgeError:
                    failed.append(s)
            now = datetime.datetime.strptime(a.now, "%Y-%m-%d %H:%M") if a.now else None
            written, kept = save(charts, a.interval, a.root, now)
            print("%s bars: %d session(s) saved%s, %d already archived; %d of %d symbols read%s." % (
                a.interval, len(written), (" (%s to %s)" % (written[0], written[-1])) if written else "", len(kept),
                len(charts), len(syms), (", failed: " + ", ".join(failed)) if failed else ""))
            return 0 if charts else 1
        if a.cmd == "export":
            got = export(a.root, a.interval, a.to, a.start, a.end)
            print("Exported %d symbols (%d bars) to %s." % (len(got), sum(got.values()), a.to))
            return 0
        if a.cmd == "status":
            ds = days_in(a.root, a.interval)
            print("%s archive: %d sessions%s." % (a.interval, len(ds), (", %s to %s" % (ds[0], ds[-1])) if ds else ""))
            return 0
        ap.print_help()
        return 1
    except ArchiveError as e:
        print("archive.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
