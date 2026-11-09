#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-05 09:40"        # pin the clock (IST time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, no whole-share qty (the NSE takes no
                rupee-amount orders), a limit order without a limit price,
                unknown type or validity, extended hours (the NSE has none);
                a bracket that is not day validity or misses a leg
  asset         futures and options (like INFY26NOVFUT, NIFTY26N2026000CE)
                unless "allow_options": true; anything that is not an NSE
                stock or ETF symbol like INFY.NS
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  cash          the buy needs more than your cash after the buffer, times
                intraday_leverage (1 unless "allow_margin": true; at most 5)
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) PLUS the round trip's charges over
                max_loss_per_trade_inr. The entry is the limit price for a limit
                order, else the HIGHER of the plan's entry and a fresh price
                (under 15 minutes old), so a stale plan cannot understate the risk
  reward:risk   the win at the target after charges / the loss at the stop with
                charges, under min_reward_risk
  charges       the round trip's charges at the stop over max_charges_pct_of_risk
                (15) % of the loss on price: a stop too close for the charges
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST
                time), or the market is closed (weekend, NSE holiday, outside
                9:15 to the 15:30 close)
  cool-off      max_losing_streak (2) losing trades in a row today, after charges
  sector        max_entries_per_sector (2) entries in one NSE industry today
                (trader/sectors.json, from NSE's Nifty 500 list), this batch included
  results       (skip_results_days, on) the stock's results day in trader/events.json, or
                the trading day after it (results out after the close move the next open)
  expiry        (skip_expiry_days, off: a warning instead) Nifty's weekly F&O expiry,
                Tuesday or the trading day before a Tuesday holiday
  surveillance  (block_surveillance, on) the stock is on NSE's ASM or GSM list in
                trader/surveillance.json (trader-scanner: scan.py surveillance)
  band          the entry is within 1% of the stock's upper price band (NSE
                circuit limit: 2, 5, 10 or 20% from the previous close, from
                trader/bands.json, written by trader-scanner's universe): at the
                band, sellers vanish and the stock can lock

Charges are Zerodha's equity intraday rates on the NSE (see trader-budget);
"charges" in the config overrides them. A stop below the lower band, and a
product other than MIS (intraday), are warnings.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "intraday_leverage": 1, "charges": {}, "max_losing_streak": 2, "max_entries_per_sector": 2,
    "max_charges_pct_of_risk": 15, "skip_results_days": True, "skip_expiry_days": False,
    "block_surveillance": True,
}
EXPIRY_WEEKDAY = 1         # Tuesday: NSE's weekly Nifty expiry since 1 September 2025
STALE_SURVEILLANCE_DAYS = 7
ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "notional", "type", "time_in_force",
                "limit_price", "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit"}
TIFS = {"day", "ioc"}
DERIV = re.compile(r"^[A-Z0-9&\-]+\d{2}[A-Z]{3}(\d+(\.\d+)?(CE|PE)|FUT)$|^[A-Z0-9&\-]+\d{2}[0-9OND]\d{2}\d+(CE|PE)$")
TICKER = re.compile(r"^[A-Z0-9&\-]{1,20}\.NS$")
CHARGES = {  # Zerodha, equity intraday on the NSE: zerodha.com/charges, read 2026-11-08
    "brokerage_pct": 0.03, "brokerage_max": 20.0, "stt_sell_pct": 0.025, "exchange_pct": 0.00307,
    "sebi_per_crore": 10.0, "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}
MAX_LEVERAGE = 5.0
BAND_MARGIN_PCT = 1.0
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

NSE_HOLIDAYS = {  # NSE equity segment trading holidays, NSE circular for 2026
    "2026-01-15": "Municipal Corporation elections in Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_EARLY_CLOSE = {}  # NSE has no scheduled early closes
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(13, 0)


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (India, IST)

def ist_offset(utc):
    """Hours from UTC for India: always +5.5 (IST, UTC+5:30, has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=ist_offset(utc))


def ist_to_utc(local):
    return local - datetime.timedelta(hours=ist_offset(local))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-11-05 09:40\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:45\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _shift(t, hours):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(hours=hours)
    return dt.time()


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (IST time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    close = EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE
    if t < OPEN or now_ist.time() >= close:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if close == EARLY and after is not None:
        after = _shift(after, 3)
        warns.append("early close at 1:00 p.m. today (%s): no new entries after %s"
                     % (NSE_EARLY_CLOSE[d.isoformat()], after.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                          t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                         t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def order_charges(side, value, rates=None):
    """Charges on one executed intraday order of this value (rupees), each to the paisa.
    Same function in trader-budget, trader-broker and trader-timemachine."""
    r = dict(CHARGES)
    r.update(rates or {})
    v = float(value)
    if v <= 0:
        return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0, "gst": 0.0, "total": 0.0}
    out = {"brokerage": round(min(v * r["brokerage_pct"] / 100.0, r["brokerage_max"]), 2),
           "stt": round(v * r["stt_sell_pct"] / 100.0, 2) if side == "sell" else 0.0,
           "exchange": round(v * r["exchange_pct"] / 100.0, 2),
           "sebi": round(v * r["sebi_per_crore"] / 1e7, 2),
           "stamp": round(v * r["stamp_buy_pct"] / 100.0, 2) if side == "buy" else 0.0}
    out["gst"] = round((out["brokerage"] + out["exchange"] + out["sebi"]) * r["gst_pct"] / 100.0, 2)
    out["total"] = round(sum(out.values()), 2)
    return out


def round_trip(qty, buy_price, sell_price, rates=None):
    """Charges for buying qty at buy_price and selling them at sell_price (a short reverses the sides)."""
    return round(order_charges("buy", qty * buy_price, rates)["total"]
                 + order_charges("sell", qty * sell_price, rates)["total"], 2)


def inr_keys(cfg):
    """Old configs said max_order_usd and so on: read them as the _inr keys."""
    out = dict(cfg)
    for k, v in cfg.items():
        if isinstance(k, str) and k.endswith("_usd"):
            out.setdefault(k[:-4] + "_inr", v)
            del out[k]
    return out


def indian_commas(text):
    """"1234567.89" or "1,234,567.89" -> "12,34,567.89" (Indian lakh and crore grouping)."""
    s = str(text).replace(",", "")
    sign = "-" if s.startswith("-") else ""
    whole, dot, frac = s.lstrip("-").partition(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(groups)
    return sign + whole + dot + frac


def money(x):
    return "₹" + indian_commas("{:.2f}".format(float(x))).replace(".00", "")


def _cent(x):
    """A price to the cent, half up (366.475 -> 366.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


# ---------------------------------------------------------------- the check

def losing_streak(state, today, rates=None):
    """Losing trades in a row at the end of today: each symbol's fills paired back to flat, P&L after
    charges, in the order they closed. (paper and live alike: a loss is a loss)"""
    per, closed = {}, []
    for r in (state or {}).get("orders", []):
        if r.get("status") != "filled" or rec_day(r) != today:
            continue
        sym = str(r.get("symbol", "")).upper()
        d = per.setdefault(sym, {"pos": 0.0, "cash": 0.0})
        q, px, side = _f(r.get("qty")) or 0, _f(r.get("price")) or 0, r.get("side")
        d["cash"] += (-1 if side == "buy" else 1) * q * px - order_charges(side, q * px, rates)["total"]
        d["pos"] += q if side == "buy" else -q
        if abs(d["pos"]) < 1e-9:
            closed.append(round(d["cash"], 2))
            per[sym] = {"pos": 0.0, "cash": 0.0}
    n = 0
    for pnl in reversed(closed):
        if pnl > 0:
            break
        n += 1
    return n, closed


def expiry_day(d):
    """True when d is Nifty's weekly F&O expiry: a trading day with no trading day between it and the next
    expiry weekday (Tuesday). So the Tuesday itself, or the Monday when the Tuesday is a holiday."""
    if not trading_day(d)[0]:
        return False
    gap = (EXPIRY_WEEKDAY - d.weekday()) % 7
    return all(not trading_day(d + datetime.timedelta(days=i))[0] for i in range(1, gap + 1))


def previous_trading_day(d):
    d -= datetime.timedelta(days=1)
    for _ in range(10):
        if trading_day(d)[0]:
            return d
        d -= datetime.timedelta(days=1)
    return d


def load_events(doc):
    """trader/events.json -> {SYM: [(date, what)]}. {"events": [{"symbol", "date", "what"}]} or {SYM: [dates]}."""
    out = {}
    if not doc:
        return out
    rows = doc.get("events") if isinstance(doc, dict) and "events" in doc else None
    if rows is None and isinstance(doc, dict):
        rows = [{"symbol": k, "date": d} for k, v in doc.items() for d in (v if isinstance(v, list) else [v])]
    for r in rows or []:
        if isinstance(r, dict) and r.get("symbol") and re.match(r"^\d{4}-\d{2}-\d{2}$", str(r.get("date", ""))):
            out.setdefault(str(r["symbol"]).upper(), []).append((str(r["date"]), r.get("what") or "results"))
    return out


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          bands=None, prev_close=None, sectors=None, events=None, surveillance=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    bands        {SYM: band percent, or None for no band}, from trader/bands.json
    prev_close   {SYM: previous close}, from trader/prices.json
    sectors      {SYM: industry}, from trader/sectors.json
    events       {SYM: [(date, what)]}, from trader/events.json (results dates)
    surveillance {"symbols": {SYM: list}, "updated": date}, from trader/surveillance.json"""
    c = dict(DEFAULTS)
    c.update(inr_keys(cfg or {}))
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_ist, c)
    streak, closed_today = losing_streak(state, today, c.get("charges"))
    sectors = {str(k).upper(): v for k, v in (sectors or {}).items()}
    sector_count = {}
    for r in entries_today:
        sec = sectors.get(str(r.get("symbol", "")).upper())
        if sec:
            sector_count[sec] = sector_count.get(sec, 0) + 1
    if c.get("max_entries_per_sector") and not sectors:
        top_warn.append("no trader/sectors.json: the sector cap (max_entries_per_sector) is off")
    day_warn = []
    today_d = datetime.date.fromisoformat(today)
    day_before = previous_trading_day(today_d).isoformat()
    events = {str(k).upper(): v for k, v in (events or {}).items()}
    surv = (surveillance or {}).get("symbols") or {}
    surv = {str(k).upper(): v for k, v in surv.items()}
    if c.get("block_surveillance") and surveillance is None:
        day_warn.append("no trader/surveillance.json: NSE's ASM and GSM lists are not checked "
                        "(trader-scanner: scan.py surveillance)")
    elif surveillance and surveillance.get("updated"):
        try:
            age = (today_d - datetime.date.fromisoformat(str(surveillance["updated"])[:10])).days
        except ValueError:
            age = None
        if age is not None and age > STALE_SURVEILLANCE_DAYS:
            day_warn.append("trader/surveillance.json is %d days old: NSE changes the ASM and GSM lists every "
                            "week, refresh it (scan.py surveillance)" % age)
    is_expiry = expiry_day(today_d)
    if is_expiry and not c.get("skip_expiry_days"):
        day_warn.append("today is Nifty's weekly F&O expiry: index moves can be sharper and reverse late "
                        "(\"skip_expiry_days\": true blocks new entries on expiry days)")
    rates = c.get("charges") or {}
    bands = {str(k).upper(): v for k, v in (bands or {}).items()}
    prev_close = {str(k).upper(): _f(v) for k, v in (prev_close or {}).items() if _f(v)}
    lev = _f(c.get("intraday_leverage")) or 1.0
    if lev > 1 and not c.get("allow_margin"):
        top_warn.append("intraday_leverage %s is ignored while \"allow_margin\" is false: no borrowing" % _num(lev))
        lev = 1.0
    lev = max(1.0, min(lev, MAX_LEVERAGE))
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)
    top_warn.extend(day_warn)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if notional is not None:
            reasons.append("format: the NSE takes whole shares, not a rupee amount: give qty, not notional")
        elif qty is None:
            reasons.append("format: no qty")
        elif _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
            reasons.append("format: the NSE takes whole shares only, got qty %r" % qty)
        exch = str(o.get("exchange") or "NSE").strip().upper()
        if exch != "NSE":
            reasons.append("format: this pack trades on the NSE, not %s" % exch)
        product = str(o.get("product") or "MIS").strip().upper()
        if product not in ("MIS", "CNC"):
            reasons.append("format: product must be MIS (intraday) or CNC (delivery), got %r" % o.get("product"))
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours"):
            reasons.append("format: the NSE has no extended-hours session")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if tif != "day":
                reasons.append("format: a bracket order needs time_in_force day, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (oc or "advanced_instructions"))

        # asset
        bare = sym[:-3] if sym.endswith(".NS") else sym
        if sym and DERIV.match(bare.replace(" ", "")):
            if not c.get("allow_options"):
                reasons.append("asset: %s is a futures or options contract; F&O is off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("F&O order: options can lose 100%% of what you pay, futures more than you put in")
        elif sym and ("/" in sym or not TICKER.match(sym)):
            reasons.append("asset: %s does not look like an NSE stock or ETF symbol (like INFY.NS); "
                           "this pack trades NSE stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            need = value / lev
            after_cash = cash - need
            if after_cash < -CENT:
                later.append("cash: needs %s%s but only %s cash; that would be margin (borrowed money)"
                             % (money(need), (" (%s at %sx leverage)" % (money(value), _num(lev))) if lev > 1 else "",
                                money(cash)))
            elif after_cash < buffer_inr - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            if lev > 1 and value > cash + CENT:
                warns.append("leverage: %s of this %s position is borrowed (%sx intraday): losses are on the full "
                             "amount" % (money(value - need), money(value), _num(lev)))
            if product != "MIS":
                warns.append("product %s is delivery, not intraday: delivery STT is 0.1%% on both sides and "
                             "nothing squares it off for you" % product)
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, band
        max_loss = risk_ps = rr = stop = target = charges = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    price_loss = round(shares * risk_ps, 2)
                    charges = (round_trip(shares, ref, stop, rates) if long
                               else round_trip(shares, stop, ref, rates))
                    max_loss = round(price_loss + charges, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, plus %s charges = %s, over "
                                     "your %s max loss per trade" % (_num(shares), _p(risk_ps), money(price_loss),
                                                                     money(charges), money(max_loss), money(cap)))
                    cost_cap = _f(c.get("max_charges_pct_of_risk"))
                    cost_pct = round(100.0 * charges / price_loss, 1) if price_loss > 0 else 0.0
                    if cost_cap and cost_pct > cost_cap + 0.05:
                        later.append("charges: %s at the stop is %.1f%% of the %s you risk on price, over your %s%% "
                                     "limit (max_charges_pct_of_risk): the stop %s is too close for the charges; "
                                     "widen it to about %s a share or more"
                                     % (money(charges), cost_pct, money(price_loss), _num(cost_cap), _p(stop),
                                        _p(math.ceil(risk_ps * cost_pct / cost_cap * 100) / 100.0)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            t_charges = (round_trip(shares, ref, target, rates) if long
                                         else round_trip(shares, target, ref, rates))
                            gain = round(shares * abs(target - ref) - t_charges, 2)
                            rr = round(gain / max_loss, 2) if max_loss > 0 else 0.0
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f after charges (win %s at the target %s, lose %s at "
                                             "the stop %s) is under your minimum %s"
                                             % (rr, money(gain), _p(target), money(max_loss), _p(stop),
                                                _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            cap_s = int(c.get("max_losing_streak") or 0)
            if cap_s and streak >= cap_s:
                later.append("cool-off: %d losing trades in a row today (%s), your limit is %d: no new entries "
                             "today" % (streak, ", ".join(("-" if x < 0 else "") + money(abs(x)) for x in closed_today[-streak:]),
                                         cap_s))
            cap_sec = int(c.get("max_entries_per_sector") or 0)
            sec = sectors.get(sym)
            if cap_sec and sec and sector_count.get(sec, 0) >= cap_sec:
                later.append("sector: %d %s entries today already, your limit is %d a sector: one move in the "
                             "sector would hit them all" % (sector_count[sec], sec, cap_sec))
            elif sectors and not sec:
                warns.append("%s is not in trader/sectors.json: its sector is unknown" % sym)
            if c.get("skip_results_days"):
                for d, what in events.get(sym, []):
                    if d == today:
                        later.append("results: %s has %s today (trader/events.json): the price can gap or swing on "
                                     "the numbers, so no new entries in it today" % (sym, what))
                    elif d == day_before:
                        later.append("results: %s had %s on %s, the last trading day (trader/events.json): today "
                                     "is the first session to react to numbers out after the close" % (sym, what, d))
            if c.get("skip_expiry_days") and is_expiry:
                later.append("expiry: today is Nifty's weekly F&O expiry and \"skip_expiry_days\" is on")
            if c.get("block_surveillance") and sym in surv:
                later.append("surveillance: %s is on NSE's %s list (trader/surveillance.json): extra margins, "
                             "often no intraday leverage, and sharp moves. No new entries" % (sym, surv[sym]))
            band = bands.get(sym, "unknown") if bands else "unknown"
            pc = prev_close.get(sym)
            if band not in ("unknown", None) and _f(band) and pc and ref:
                upper, lower = pc * (1 + _f(band) / 100.0), pc * (1 - _f(band) / 100.0)
                if long and ref >= upper * (1 - BAND_MARGIN_PCT / 100.0) - CENT:
                    later.append("band: %s at %s is within %s%% of its upper price band %s (%s%% over the previous "
                                 "close %s): at the band sellers vanish and it can lock"
                                 % (sym, _p(ref), _num(BAND_MARGIN_PCT), _p(upper), _num(band), _p(pc)))
                if long and stop and stop < lower - CENT:
                    warns.append("the stop %s is under the lower price band %s: if %s locks at the band, the stop "
                                 "cannot fill until it reopens" % (_p(stop), _p(lower), sym))
            elif bands and sym not in bands:
                warns.append("%s is not in trader/bands.json: its price band is unknown (run scan.py universe)" % sym)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s with %s charges%s, reward:risk after charges %s%s"
                            % (money(max_loss), money(charges or 0), (" (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                if sectors.get(sym):
                    sector_count[sectors[sym]] = sector_count.get(sectors[sym], 0) + 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges": charges, "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "leverage": lev,
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prev_close(pdoc):
    """prices.json detail[SYM].prev_close -> {SYM: previous close}."""
    out = {}
    for s, d in ((pdoc or {}).get("detail") or {}).items():
        if isinstance(d, dict) and _f(d.get("prev_close")):
            out[str(s).upper()] = _f(d["prev_close"])
    return out


def load_bands(doc):
    """bands.json {"bands": {SYM: 2 | 5 | 10 | 20 | null}} -> {SYM: band or None}."""
    if not isinstance(doc, dict):
        return {}
    bands = doc.get("bands", doc)
    out = {}
    for s, b in (bands if isinstance(bands, dict) else {}).items():
        out[str(s).upper()] = _f(b)
    return out


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--bands", default="trader/bands.json", help="NSE price bands (trader-scanner: scan.py universe)")
    ap.add_argument("--sectors", default="trader/sectors.json", help="each stock's NSE industry, for the sector cap")
    ap.add_argument("--events", default="trader/events.json", help="results dates (trader-analyst: results.py event)")
    ap.add_argument("--surveillance", default="trader/surveillance.json",
                    help="NSE's ASM and GSM lists (trader-scanner: scan.py surveillance)")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-05 09:40\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-05")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        bands = load_bands(read_json(a.bands, "price bands", required=False))
        sdoc = read_json(a.sectors, "sectors", required=False) or {}
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, bands, load_prev_close(pdoc),
                    sdoc.get("sectors", sdoc) if isinstance(sdoc, dict) else {},
                    load_events(read_json(a.events, "events", required=False)),
                    read_json(a.surveillance, "surveillance lists", required=False))
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
