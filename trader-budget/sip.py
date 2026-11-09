#!/usr/bin/env python3
"""The core for trader-budget: a monthly SIP into a Nifty 50 index fund, tracked next to the trading. Standard
library only.

Every test in this pack so far says the same thing: after charges and tax, holding the index beat the
trading rules (edge.py, swing.py, and rotation.py's real funds). So the sensible core is a plain monthly SIP
into a low-cost Nifty 50 fund, with any trading kept small and judged against it.

    python3 sip.py plan --monthly 10000                     # this month's units of NIFTYBEES at the live price
    python3 sip.py add --date 2026-11-11 --amount 9984.60 --units 35   # record what you bought (contract note)
    python3 sip.py status                                   # invested, value, gain, XIRR; and the trading next to it
    python3 sip.py status --price 284.22 --json

plan    the whole units --monthly rupees buys at the fund's price (--price, or Yahoo's last price), and what is
        left over. An ETF trades like a share: buy it in your demat account, delivery (CNC). Zerodha charges
        no brokerage on delivery; check the contract note for the small exchange charges. A direct-plan index
        mutual fund (no demat, no units to round) works as well: record each purchase with add.
add     records one purchase in trader/sip.json: the date, the rupees paid (with charges) and the units.
status  units, money in, value at the price, the gain, and the XIRR (the yearly return that accounts for when
        each rupee went in). With trader-broker's log (--state) it puts the trading's realised P&L after
        charges since the first SIP next to it, so you can see which one is earning its keep.

Nothing here buys anything. Not financial advice.
"""

import argparse
import datetime
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

FUND = "NIFTYBEES.NS"
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"


class SipError(Exception):
    pass


def money(x):
    s = "%.2f" % abs(x)
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(groups)
    return ("-" if x < 0 else "") + "₹" + whole + "." + frac


def read(path, required=False):
    if not os.path.exists(path):
        if required:
            raise SipError("no %s yet: record a purchase with sip.py add" % path)
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError as e:
            raise SipError("%s is not valid JSON: %s" % (path, e))


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def last_price(sym):
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d" % urllib.parse.quote(sym)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            meta = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, IndexError):
        raise SipError("no price for %s from Yahoo: pass --price" % sym)


def xirr(flows):
    """flows [(date, amount)]: money in is negative, the value today positive. The yearly rate r that makes
    the sum of amount / (1 + r) ** (days / 365) zero, by bisection; None when there is no answer."""
    if len(flows) < 2 or not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)

    def npv(r):
        return sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows)
    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def trading_pnl(state_path, since):
    """trader-broker's realised P&L after charges since a date, live and paper apart."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), "trader-broker"))
    try:
        import ticket
    except ImportError:
        return None
    if not os.path.exists(state_path):
        return None
    state = ticket.load_state(state_path)
    out = {}
    for label, paper in (("live", False), ("paper", True)):
        trades, _ = ticket.round_trips(state, since, None, paper=paper)
        if trades:
            out[label] = {"trades": len(trades), "net": round(sum(t["net"] for t in trades), 2)}
    return out


def status(doc, price, today):
    buys = doc.get("buys") or []
    if not buys:
        raise SipError("no purchases recorded yet: sip.py add")
    units = sum(float(b["units"]) for b in buys)
    paid = sum(float(b["amount"]) for b in buys)
    value = units * price
    flows = [(datetime.date.fromisoformat(b["date"]), -float(b["amount"])) for b in buys]
    flows.append((datetime.date.fromisoformat(today), value))
    r = xirr(flows)
    return {"fund": doc.get("fund", FUND), "buys": len(buys), "units": round(units, 4), "invested": round(paid, 2),
            "price": price, "value": round(value, 2), "gain": round(value - paid, 2),
            "gain_pct": round(100.0 * (value / paid - 1), 2) if paid else None,
            "xirr_pct": round(100.0 * r, 2) if r is not None else None, "first": min(b["date"] for b in buys)}


def cmd_plan(a):
    doc = read(a.file) or {}
    fund = (a.fund or doc.get("fund") or FUND).upper()
    price = a.price or last_price(fund)
    units = int(a.monthly // price)
    print("This month: %d units of %s at about %s = %s; %s left over (carry it to next month)."
          % (units, fund, money(price), money(units * price), money(a.monthly - units * price)))
    print("Buy it as a delivery (CNC) order in your demat account, then record it from the contract note:")
    print("  sip.py add --date YYYY-MM-DD --amount <rupees paid with charges> --units %d" % units)
    print("Nothing was bought. Not financial advice.")
    return 0


def cmd_add(a):
    doc = read(a.file) or {"created_by": "trader-budget sip.py", "fund": (a.fund or FUND).upper(), "buys": []}
    try:
        datetime.date.fromisoformat(a.date)
    except ValueError:
        raise SipError("--date wants YYYY-MM-DD")
    if a.amount <= 0 or a.units <= 0:
        raise SipError("--amount and --units must be above 0")
    doc["buys"].append({"date": a.date, "amount": round(a.amount, 2), "units": a.units})
    doc["buys"].sort(key=lambda b: b["date"])
    write(a.file, doc)
    print("Recorded %s of %s on %s: %g units at %s each." % (money(a.amount), doc["fund"], a.date, a.units,
                                                            money(a.amount / a.units)))
    return 0


def cmd_status(a):
    doc = read(a.file, required=True)
    price = a.price or last_price(doc.get("fund", FUND))
    today = a.today or datetime.date.today().isoformat()
    s = status(doc, price, today)
    tr = trading_pnl(a.state, s["first"]) if a.state else None
    if a.json:
        print(json.dumps(dict(s, trading=tr), indent=2))
        return 0
    print("SIP in %s since %s: %d purchases, %g units." % (s["fund"], s["first"], s["buys"], s["units"]))
    print("  Invested %s, worth %s at %s: %s (%+.2f %%)%s." % (
        money(s["invested"]), money(s["value"]), money(price), ("+" if s["gain"] >= 0 else "") + money(s["gain"]),
        s["gain_pct"] or 0, (", XIRR %+.2f %% a year" % s["xirr_pct"]) if s["xirr_pct"] is not None else ""))
    if tr:
        for label in ("live", "paper"):
            if label in tr:
                print("  Trading (%s) since %s: %d round trips, %s after charges." % (
                    label, s["first"], tr[label]["trades"], ("+" if tr[label]["net"] >= 0 else "") + money(tr[label]["net"])))
    print("Before tax. Not financial advice.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="A monthly index-fund SIP, tracked next to the trading (trader-budget).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="this month's units at the fund's price")
    p.add_argument("--monthly", type=float, required=True, help="rupees a month")
    p.add_argument("--price", type=float)
    p.add_argument("--fund")
    p = sub.add_parser("add", help="record a purchase")
    p.add_argument("--date", required=True)
    p.add_argument("--amount", type=float, required=True, help="rupees paid, charges included")
    p.add_argument("--units", type=float, required=True)
    p.add_argument("--fund")
    p = sub.add_parser("status", help="invested, value, gain, XIRR, and the trading next to it")
    p.add_argument("--price", type=float)
    p.add_argument("--today")
    p.add_argument("--state", default="trader/state/broker.json", help="trader-broker's log, for the trading P&L")
    p.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.add_argument("--file", default="trader/sip.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "add": cmd_add, "status": cmd_status}[a.cmd](a)
    except SipError as e:
        print("sip.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
