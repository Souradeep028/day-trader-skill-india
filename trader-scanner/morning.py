#!/usr/bin/env python3
"""The morning routine for the day-trader pack: one command before the 9:15 open. Standard library only.

    python3 morning.py                                   # trader/nifty50.txt, today in India
    python3 morning.py --watchlist trader/banknifty.txt --news 8
    python3 morning.py --skip-universe --no-news         # just the scan and the calendar
    python3 morning.py --now "2026-11-09 08:30" --scan-offline daily.json --news-offline DIR --skip-universe

It runs, in order, each skill's own tool (siblings of this folder), and writes one brief:

  1. the calendar   is the NSE open today (weekends, the 2026 holiday list); Nifty's weekly F&O
                    expiry (Tuesday, or the Monday before a Tuesday holiday)
  2. the universe   scan.py universe: NSE's equity list and price bands -> trader/bands.json. NSE often
                    turns scripts away; then the brief says so and how old the bands on disk are
  3. surveillance   how old trader/surveillance.json (NSE's ASM and GSM lists) is, or that it is missing
  4. the scan       scan.py daily --watchlist ...: daily setups -> trader/watchlist.json
  5. results        trader/events.json: results dates today or on the next session for the scanned list
  6. the news       news.py --tickers <the top hits> --hours 24, headlines new since the last check
  7. the rules      the day's limits from trader/config.json: entry window, flat-by, max loss, trades

The brief goes to trader/morning.md and the terminal. On a day the NSE is closed it says so and stops.
It places nothing and changes no order: it is the list to read before /trader-entry at 9:30.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import scan  # noqa: E402  (this folder's scanner: its calendar and clock)

SCAN = os.path.join(HERE, "scan.py")
NEWS = os.path.join(SKILLS, "trader-news", "news.py")
EXPIRY_WEEKDAY = 1        # Tuesday: NSE's weekly Nifty expiry since 1 September 2025
STALE_DAYS = 7
SPECIAL_SESSIONS = {"2026-11-08": "Diwali Muhurat trading, a short Sunday session"}   # as in trader-risk
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def long_date(d):
    return "%s %d %s %d" % (WEEKDAYS[d.weekday()], d.day, MONTHS[d.month - 1], d.year)


def next_trading_day(d):
    for i in range(1, 15):
        n = d + datetime.timedelta(days=i)
        if scan.trading_day(n)[0]:
            return n
    return None


def expiry_day(d):
    """Nifty's weekly expiry: a trading day with no trading day between it and the next Tuesday."""
    if not scan.trading_day(d)[0]:
        return False
    gap = (EXPIRY_WEEKDAY - d.weekday()) % 7
    return all(not scan.trading_day(d + datetime.timedelta(days=i))[0] for i in range(1, gap + 1))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def file_age_days(path, today, key=None):
    """Days since a file was updated: its own date field when it has one, else its modification time."""
    doc = read_json(path) if key else None
    if doc and doc.get(key):
        try:
            return (today - datetime.date.fromisoformat(str(doc[key])[:10])).days
        except ValueError:
            pass
    if not os.path.exists(path):
        return None
    return (today - datetime.date.fromtimestamp(os.path.getmtime(path))).days


def run(cmd):
    """(exit code, stdout, stderr) of one sibling tool."""
    p = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout, p.stderr


def money(x):
    return "₹" + scan.indian_commas("%.0f" % float(x))


def events_for(doc, symbols, days):
    rows = []
    for e in (doc or {}).get("events", []):
        if (e.get("symbol") in symbols or str(e.get("symbol")).upper() in ("MARKET", "*")) and e.get("date") in days:
            rows.append(e)
    return sorted(rows, key=lambda e: (e["date"], e["symbol"]))


def brief(today, steps, cfg):
    """steps: {"universe": {...}, "surveillance": age, "scan": doc or error, "events": [...], "news": doc or
    error, "expiry": bool, "next": date}. Returns the markdown lines."""
    L = ["# Morning brief: %s" % long_date(today), ""]
    L.append("The NSE is open 9:15 AM to 3:30 PM IST. Your window: entries %s to %s, flat by %s."
             % (cfg.get("no_entries_before") or "09:30", cfg.get("no_entries_after") or "14:45",
                cfg.get("flat_by") or "15:10"))
    if steps.get("expiry"):
        L.append("Today is Nifty's weekly F&O expiry: index moves can be sharper and reverse late.")
    L += ["", "## Lists"]
    u = steps.get("universe") or {}
    if u.get("skipped"):
        L.append("- Universe and price bands: not refreshed (--skip-universe)%s."
                 % ("; trader/bands.json is %d day(s) old" % u["bands_age"] if u.get("bands_age") is not None else ""))
    elif u.get("ok"):
        L.append("- Universe and price bands: refreshed from NSE (%s)." % u.get("summary", "done"))
    else:
        L.append("- Universe and price bands: NSE turned the script away (%s). %s Save EQUITY_L.csv and "
                 "sec_list.csv from a browser and run scan.py universe --offline <folder>."
                 % (u.get("error", "no answer"),
                    "trader/bands.json is %d day(s) old." % u["bands_age"] if u.get("bands_age") is not None
                    else "There is no trader/bands.json: the risk check cannot test price bands."))
    age = steps.get("surveillance")
    if age is None:
        L.append("- ASM/GSM lists: no trader/surveillance.json. Save the CSVs from nseindia.com/reports/asm and "
                 "/reports/gsm, then scan.py surveillance <files>.")
    elif age > STALE_DAYS:
        L.append("- ASM/GSM lists: %d days old; NSE changes them weekly, refresh them (scan.py surveillance)." % age)
    else:
        L.append("- ASM/GSM lists: %d day(s) old." % age)
    L += ["", "## Setups from yesterday's close"]
    s = steps.get("scan")
    if isinstance(s, dict):
        hits = s.get("hits") or []
        L.append("%d of %d charts matched a setup (%s)." % (len(hits), s.get("scanned") or s.get("universe") or 0,
                                                          s.get("as_of") or "latest close"))
        if hits:
            L += ["", "| symbol | setups | close | RSI |", "|---|---|---:|---:|"]
            for h in hits[:15]:
                L.append("| %s | %s | %.2f | %s |" % (h["symbol"], ", ".join(h["setups"]), h["price"],
                                                     h.get("rsi", "")))
            if len(hits) > 15:
                L.append("")
                L.append("And %d more in trader/watchlist.json." % (len(hits) - 15))
    else:
        L.append("The scan did not run: %s" % (s or "no answer"))
    ev = steps.get("events") or []
    L += ["", "## Results, ex-dates and market events"]
    if ev:
        for e in ev:
            kind = e.get("kind") or ("market" if str(e["symbol"]).upper() in ("MARKET", "*") else "results")
            L.append("- %s: %s on %s. %s" % (e["symbol"], e.get("what") or kind, e["date"], {
                "results": "The risk check blocks new entries in it that day and the next session.",
                "ex-date": "The risk check blocks new entries in it that day.",
                "market": "The risk check blocks every new entry that day."}.get(kind, "")))
    else:
        L.append("None on file for these stocks today or next session (trader-analyst: results.py event adds one).")
    n = steps.get("news")
    if n is not None:
        L += ["", "## News since the last check"]
        if isinstance(n, dict):
            items = n.get("items") or []
            important = [i for i in items if i.get("important") or i.get("tags")]
            moving = [m for m in n.get("moves") or [] if m.get("moving")]
            if moving:
                L.append("Moving: " + ", ".join("%s %+.1f %%" % (m["ticker"], m["pct"]) for m in moving) + ".")
            L.append("%d new headline%s, %d tagged." % (len(items), "" if len(items) == 1 else "s", len(important)))
            for i in (important or items)[:8]:
                L.append("- [%s] %s (%s%s)" % (i.get("ticker", ""), i.get("title", "").strip(), i.get("source", ""),
                                               (": " + ", ".join(i["tags"])) if i.get("tags") else ""))
        else:
            L.append("The news check did not run: %s" % n)
    L += ["", "## Today's rules",
          "- Max loss %s a trade with charges, %s a day, %s trades, charges at most %s %% of the risk."
          % (money(cfg.get("max_loss_per_trade_inr", 500)), money(cfg.get("max_daily_loss_inr", 1500)),
             cfg.get("max_trades_per_day", 3), cfg.get("max_charges_pct_of_risk", 15)),
          "- Two losing trades in a row ends the day's entries; two entries a sector at most; %s at most at risk "
          "in open trades on one side; %s a week and %s a month of losses stop new entries."
          % (money(cfg.get("max_same_side_risk_inr", 1000)), money(cfg.get("max_weekly_loss_inr", 3000)),
             money(cfg.get("max_monthly_loss_inr", 6000))),
          "- If a trade is open, run ticket.py reconcile after every fill: no position without its stop.",
          "- Paper trading is %s." % ("ON" if cfg.get("paper", True) else "OFF: orders are live"),
          "", "Next: at 9:30, /trader-entry on the setups worth watching. Nothing has been placed. "
              "This is a list to read, not advice."]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="The morning routine: calendar, lists, scan, results dates, news.")
    ap.add_argument("--watchlist", default="trader/nifty50.txt", help="the stocks to scan (default the Nifty 50)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--out", default="trader/morning.md")
    ap.add_argument("--news", type=int, default=10, help="headlines for the top N setups (0: none)")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--skip-universe", action="store_true", help="do not download NSE's lists")
    ap.add_argument("--now", help="pretend it is this IST time")
    ap.add_argument("--scan-offline", metavar="PATH", help="pass to scan.py daily --offline (tests)")
    ap.add_argument("--universe-offline", metavar="DIR", help="pass to scan.py universe --offline")
    ap.add_argument("--news-offline", metavar="DIR", help="pass to news.py --offline (tests)")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    now_utc = scan.parse_now(a.now)
    today = scan.to_ist(now_utc).date()
    ok, why = scan.trading_day(today)
    if not ok:
        nxt = next_trading_day(today)
        special = SPECIAL_SESSIONS.get(today.isoformat())
        print("No NSE session today, %s (%s). The next one is %s." % (
            long_date(today), ("special session: %s; the pack does not trade it" % special) if special else why,
            long_date(nxt) if nxt else "not in the calendar"))
        return 0
    cfg = read_json(a.config) or {}
    now_arg = ["--now", a.now] if a.now else []
    steps = {"expiry": expiry_day(today), "next": next_trading_day(today)}
    bands_age = file_age_days("trader/bands.json", today, "as_of")
    if a.skip_universe:
        steps["universe"] = {"skipped": True, "bands_age": bands_age}
    else:
        cmd = [SCAN, "universe", "--json"] + now_arg + (["--offline", a.universe_offline] if a.universe_offline else [])
        code, out, err = run(cmd)
        if code == 0:
            try:
                u = json.loads(out)
                steps["universe"] = {"ok": True, "summary": "%s stocks" % "{:,}".format(u.get("count", 0))}
            except ValueError:
                steps["universe"] = {"ok": True}
        else:
            first = (err.strip().splitlines() or ["no answer"])[0].replace("scan.py: ", "")
            steps["universe"] = {"ok": False, "error": first.rstrip("."), "bands_age": bands_age}
    steps["surveillance"] = file_age_days("trader/surveillance.json", today, "updated")
    cmd = [SCAN, "daily", "--watchlist", a.watchlist, "--json", "--quiet"] + now_arg
    if a.scan_offline:
        cmd += ["--offline", a.scan_offline]
    if a.no_write:
        cmd += ["--no-write"]
    code, out, err = run(cmd)
    try:
        steps["scan"] = json.loads(out)
    except ValueError:
        steps["scan"] = (err.strip().splitlines() or ["no answer"])[-1]
    hits = [h["symbol"] for h in (steps["scan"].get("hits") if isinstance(steps["scan"], dict) else []) or []]
    try:
        listed = [s for s in scan.symbols_from_file(a.watchlist)]
    except Exception:
        listed = []
    days = {today.isoformat()} | ({steps["next"].isoformat()} if steps["next"] else set())
    steps["events"] = events_for(read_json("trader/events.json"), set(hits) | set(listed), days)
    if not a.no_news and a.news > 0 and hits:
        cmd = [NEWS, "--tickers"] + hits[:a.news] + ["--hours", "24", "--json"]
        if a.news_offline:
            cmd += ["--offline", a.news_offline]
        if a.now:
            cmd += ["--now", scan.to_ist(now_utc).strftime("%Y-%m-%dT%H:%M:%S+05:30")]
        if a.no_write:
            cmd += ["--dry-run"]
        code, out, err = run(cmd)
        try:
            steps["news"] = json.loads(out)
        except ValueError:
            steps["news"] = (err.strip().splitlines() or ["no answer"])[-1]
    elif not a.no_news and a.news > 0:
        steps["news"] = {"items": [], "moves": []}
    lines = brief(today, steps, cfg)
    text = "\n".join(lines) + "\n"
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
