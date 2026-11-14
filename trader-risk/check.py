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
  short         selling more than you hold, unless "allow_short": true; with it on,
                a short must be intraday (MIS), and its margin counts against cash

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
  live          on a live account ("paper": false): no PASSED forward test (trader-broker's
                forward.py, trader/forward.json) on the shadow replay AND your paper
                trades, both within 30 days; or the live stop (a 20-trade live block
                under the backtest's low end, or the live trades 5 R under their best).
                Locked in the code (LIVE_NEEDS_FORWARD_PASS), not the config. Live size
                ramps 25 %, 50 %, 100 % of the max loss over good 20-trade blocks
  held limits   the day's first check records the limits (trader/state/limits.json);
                a limit loosened later that day keeps the morning's stricter value
                until the next day, with a warning. Tightening applies at once
  window        before no_entries_before or after no_entries_after (IST
                time), or the market is closed (weekend, NSE holiday, outside
                9:15 to the 15:30 close)
  drawdown      while the account (the budget plus every closed trade, after charges) is
                drawdown_cut_pct (5) % or more under its peak, the max loss per trade is
                drawdown_size_factor (0.5) of it, until it is back within 5 %
  cool-off      max_losing_streak (2) losing trades in a row today, after charges
  weekly loss   the loss realised since Monday plus this trade's max loss over
                max_weekly_loss_inr (3,000); monthly the same from the 1st
                (max_monthly_loss_inr, 6,000)
  direction     the max losses of today's still-open entries on the same side
                (longs, or shorts) plus this one over max_same_side_risk_inr
                (1,000): three longs in three sectors still fall together
  sector        max_entries_per_sector (2) entries in one NSE industry today
                (trader/sectors.json, from NSE's Nifty 500 list), this batch included
  market event  (skip_market_event_days, on) a MARKET event today in trader/events.json
                (RBI policy, the Union Budget, election results): no entries at all
  ex-date       (skip_ex_dates, on) the stock goes ex-dividend, ex-split or ex-bonus today
  index         (max_index_move_pct, 3) Nifty 50 (^NSEI in trader/prices.json) has moved
                3 % or more from yesterday's close; 10 % is the market-wide circuit breaker
  special       a special session on a closed day (Diwali Muhurat trading): not traded
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
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "core")):  # the shared tables: core/tradercore.py
    if os.path.isfile(os.path.join(_p, "tradercore.py")) and _p not in sys.path:
        sys.path.append(_p)
import tradercore  # noqa: E402

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "intraday_leverage": 1, "charges": {}, "max_losing_streak": 2, "max_entries_per_sector": 2,
    "max_charges_pct_of_risk": 15, "skip_results_days": True, "skip_expiry_days": False,
    "max_weekly_loss_inr": 3000, "max_monthly_loss_inr": 6000, "max_same_side_risk_inr": 1000,
    "skip_ex_dates": True, "skip_market_event_days": True, "max_index_move_pct": 3,
    "drawdown_cut_pct": 5, "drawdown_size_factor": 0.5,
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
CHARGES = tradercore.charges("zerodha")  # equity intraday on the NSE: core/brokers/zerodha.json
MAX_LEVERAGE = 5.0
BAND_MARGIN_PCT = 1.0
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

NSE_HOLIDAYS = tradercore.holidays()  # NSE equity segment trading holidays: core/nse.json, the one copy
NSE_EARLY_CLOSE = tradercore.early_close()  # NSE has no scheduled early closes
NSE_SPECIAL_SESSIONS = tradercore.special_sessions()  # one-off sessions on closed days: the pack does not trade them
INDEX = "^NSEI"
MWCB_PCT = 10.0            # a 10 % move in Nifty or the Sensex halts all trading (market-wide circuit breaker)
CALENDAR_YEARS = tradercore.calendar_years()
CHARGES_READ = tradercore.charges_read("zerodha")  # when the charges were last checked against zerodha.com/charges
# The live lock. Deliberately a constant in the code, not a config key: a bad afternoon should not be able to
# switch it off. Live entries need trader-broker's forward test to have PASSED, on the shadow replay AND on
# your own paper trades, with both verdicts no older than FORWARD_FRESH_DAYS.
LIVE_NEEDS_FORWARD_PASS = True
FORWARD_FRESH_DAYS = 30
RAMP = (0.25, 0.5, 1.0)       # live size after 0, 1 and 2+ good 20-trade blocks
RAMP_BLOCK = 20
LIVE_STOP_R = 5.0             # live trading stops when the live trades fall this many R from their best
# The day's limits hold all day: a limit loosened after the first check of the day waits for the next day.
# "low": a lower value is stricter (0 or none is off, the loosest); "high": higher is stricter;
# True / False: that value is the stricter one.
LIMIT_RULES = {
    "max_loss_per_trade_inr": "low", "max_daily_loss_inr": "low", "max_weekly_loss_inr": "low",
    "max_monthly_loss_inr": "low", "max_same_side_risk_inr": "low", "max_trades_per_day": "low",
    "max_orders_per_day": "low", "max_losing_streak": "low", "max_entries_per_sector": "low",
    "max_charges_pct_of_risk": "low", "max_order_inr": "low", "max_position_pct": "low",
    "max_index_move_pct": "low", "intraday_leverage": "low", "drawdown_cut_pct": "low",
    "drawdown_size_factor": "low", "min_reward_risk": "high", "cash_buffer_pct": "high",
    "require_stop": True, "paper": True, "skip_results_days": True, "skip_ex_dates": True,
    "skip_market_event_days": True, "block_surveillance": True, "skip_expiry_days": True,
    "allow_short": False, "allow_margin": False, "allow_options": False,
}
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


def calendar_unknown(d):
    """Why a new entry on day d is refused for want of a holiday list, or None. Fails closed: a day in a year
    the pack has no NSE holiday list for may be a holiday that the weekday test calls a session."""
    if d.year in CALENDAR_YEARS:
        return None
    return ("the NSE holiday list covers %s only, so %s may be a holiday: no new entries until %d's list is "
            "added (nseindia.com, Resources, Exchange communication, Holidays: add it to \"holidays\" in "
            "core/nse.json, and %d to \"calendar_years\")"
            % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat(), d.year, d.year))


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (IST time). Empty when the window is off, except a day the
    holiday list does not cover, which is refused either way."""
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    unknown = calendar_unknown(d)
    if unknown:
        return ["window: " + unknown], []
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    ok, why = trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        special = NSE_SPECIAL_SESSIONS.get(d.isoformat())
        if special:
            return ["window: today is a special session (%s): short hours and thin books, the pack does not "
                    "trade it" % special], warns
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


def closed_trades(state, date_from, date_to, rates=None):
    """[(date, symbol, net P&L after charges)] for every round trip closed between the two dates: each day's
    fills per symbol paired back to flat (paper and live alike)."""
    per, out = {}, []
    for r in (state or {}).get("orders", []):
        d = rec_day(r)
        if r.get("status") != "filled" or not date_from <= d <= date_to:
            continue
        sym = str(r.get("symbol", "")).upper()
        x = per.setdefault((d, sym), {"pos": 0.0, "cash": 0.0})
        q, px, side = _f(r.get("qty")) or 0, _f(r.get("price")) or 0, r.get("side")
        x["cash"] += (-1 if side == "buy" else 1) * q * px - order_charges(side, q * px, rates)["total"]
        x["pos"] += q if side == "buy" else -q
        if abs(x["pos"]) < 1e-9:
            out.append((d, sym, round(x["cash"], 2)))
            per[(d, sym)] = {"pos": 0.0, "cash": 0.0}
    return out


def open_today(state, today):
    """{SYM: net shares} still open from today's fills."""
    pos = {}
    for r in (state or {}).get("orders", []):
        if r.get("status") != "filled" or rec_day(r) != today:
            continue
        sym = str(r.get("symbol", "")).upper()
        q = _f(r.get("qty")) or 0
        pos[sym] = pos.get(sym, 0) + (q if r.get("side") == "buy" else -q)
    return {k: v for k, v in pos.items() if abs(v) > 1e-9}


def drawdown(state, start_equity, rates=None):
    """(peak, equity, percent under the peak) from every round trip closed so far, in order, after charges,
    starting from start_equity (the budget). Each day's fills per symbol are paired back to flat."""
    if not start_equity:
        return None
    per, eq = {}, float(start_equity)
    peak = eq
    rows = sorted((r for r in (state or {}).get("orders", []) if r.get("status") == "filled"),
                  key=lambda r: (str(r.get("date") or str(r.get("at", ""))[:10]), str(r.get("at", ""))))
    for r in rows:
        key = (str(r.get("date") or str(r.get("at", ""))[:10]), str(r.get("symbol", "")).upper())
        x = per.setdefault(key, {"pos": 0.0, "cash": 0.0})
        q, px, side = _f(r.get("qty")) or 0, _f(r.get("price")) or 0, r.get("side")
        x["cash"] += (-1 if side == "buy" else 1) * q * px - order_charges(side, q * px, rates)["total"]
        x["pos"] += q if side == "buy" else -q
        if abs(x["pos"]) < 1e-9:
            eq += x["cash"]
            peak = max(peak, eq)
            per[key] = {"pos": 0.0, "cash": 0.0}
    return round(peak, 2), round(eq, 2), round(100.0 * (peak - eq) / peak, 2) if peak > 0 else 0.0


def max_loss_now(c, dd):
    """The max loss per trade after the drawdown rule: drawdown_size_factor (0.5) of it while the account is
    drawdown_cut_pct (5) % or more under its peak. Returns (max loss, the note, or None)."""
    base = _f(c.get("max_loss_per_trade_inr"))
    cut, factor = _f(c.get("drawdown_cut_pct")), _f(c.get("drawdown_size_factor"))
    if base and cut and factor and dd and dd[2] >= cut - 1e-9:
        return round(base * factor, 2), ("the account is %.1f%% under its peak (%s, now %s), at or past your %s%% "
                                         "drawdown_cut_pct: the max loss per trade is cut to %s until it is back "
                                         "within %s%%" % (dd[2], money(dd[0]), money(dd[1]), _num(cut),
                                                          money(round(base * factor, 2)), _num(cut)))
    return base, None


def limits_snapshot(c):
    keys = list(LIMIT_RULES) + ["no_entries_before", "no_entries_after", "blocked"]
    return {k: c.get(k) for k in keys}


def hold_morning_limits(c, morning):
    """The stricter of the morning's limits and today's config, key by key. Returns (values, warnings)."""
    eff, warns = {}, []
    for k, rule in LIMIT_RULES.items():
        if k not in morning:
            continue
        m, now_ = morning[k], c.get(k)
        if rule == "low":
            mv, nv = _f(m) or float("inf"), _f(now_) or float("inf")
            if nv > mv:
                eff[k] = m
        elif rule == "high":
            if (_f(now_) or 0) < (_f(m) or 0):
                eff[k] = m
        else:
            if bool(m) == rule and bool(now_) != rule:
                eff[k] = m
    for k, later in (("no_entries_before", True), ("no_entries_after", False)):
        if k not in morning:
            continue
        try:
            mv, nv = hhmm(morning[k], k), hhmm(c.get(k), k)
        except RiskError:
            continue
        if mv is not None and (nv is None or (nv < mv if later else nv > mv)):
            eff[k] = morning[k]
    gone = {str(x).upper() for x in morning.get("blocked") or []} - {str(x).upper() for x in c.get("blocked") or []}
    if gone:
        eff["blocked"] = sorted({str(x).upper() for x in c.get("blocked") or []} | gone)
    for k, v in sorted(eff.items()):
        warns.append("%s was loosened today (to %s); this morning's %s holds until tomorrow: limits only get "
                     "looser overnight" % (k, json.dumps(c.get(k)), json.dumps(v)))
    return eff, warns


def live_gate(forward, today):
    """(open, why). Live entries need a PASSED forward test on the shadow replay and on paper, both fresh."""
    if not forward:
        return False, "no forward test (trader/forward.json): freeze a rule with forward.py start and prove it"
    v = forward.get("verdicts") or {}
    bad = []
    d_today = datetime.date.fromisoformat(today)
    for kind in ("shadow", "paper"):
        x = v.get(kind) or {}
        if x.get("state") != "passed":
            bad.append("%s %s" % (kind, x.get("state") or "not run"))
        elif x.get("gate") != "sequential":
            bad.append("%s passed the old 40-trade gate, which is too small to tell a small edge from luck: "
                       "freeze a new test (forward.py start) and pass the sequential gate" % kind)
        elif (d_today - datetime.date.fromisoformat(x["date"])).days > FORWARD_FRESH_DAYS:
            bad.append("%s passed on %s, over %d days ago: run forward.py status again" % (kind, x["date"],
                                                                                          FORWARD_FRESH_DAYS))
    if bad:
        return False, "the forward test has not passed (%s)" % ", ".join(bad)
    return True, ""


def live_ramp(forward, state, today, rates=None):
    """Live size and the live stop. Live trades since the forward test passed, in 20-trade blocks: each block
    whose average R (after charges, over the max loss approved for it) stays at or above the backtest's low end
    moves the size up a step, 25 % -> 50 % -> 100 %. A block under it, or the live trades falling LIVE_STOP_R from
    their best, stops live entries."""
    v = (forward or {}).get("verdicts") or {}
    since = max([x.get("passed_on") or "" for x in (v.get("shadow") or {}, v.get("paper") or {})] or [""])
    low = ((forward or {}).get("expected") or {}).get("low")
    low = 0.0 if low is None else max(low, 0.0)
    planned = {}
    for r in (state or {}).get("orders", []):
        if r.get("status") == "approved" and r.get("kind", "entry") == "entry" and _f(r.get("max_loss")):
            planned[(rec_day(r), str(r.get("symbol", "")).upper())] = _f(r["max_loss"])
    live_state = {"orders": [r for r in (state or {}).get("orders", []) if not r.get("paper")]}
    rs = []
    for d, sym, net in closed_trades(live_state, since or "0000", today, rates):
        ml = planned.get((d, sym))
        if ml:
            rs.append(net / ml)
    stage, stop = 0, None
    for b in range(len(rs) // RAMP_BLOCK):
        block = rs[b * RAMP_BLOCK:(b + 1) * RAMP_BLOCK]
        avg = sum(block) / len(block)
        if avg < low - 1e-9:
            stop = ("live trades %d to %d averaged %+.2f R, under the backtest's low end %+.2f R"
                    % (b * RAMP_BLOCK + 1, (b + 1) * RAMP_BLOCK, avg, low))
            break
        stage += 1
    cum = peak = worst = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    if not stop and worst >= LIVE_STOP_R - 1e-9:
        stop = "the live trades fell %.1f R from their best, past %.0f R" % (worst, LIVE_STOP_R)
    return {"trades": len(rs), "stage": stage, "factor": RAMP[min(stage, len(RAMP) - 1)], "stop": stop,
            "since": since}


def staleness(d):
    """Warnings for the dates this pack has to be told: next year's NSE holidays (published in December) and
    the broker's charges (checked once a year)."""
    out = []
    if d.month == 12 and d.year + 1 not in CALENDAR_YEARS:
        out.append("NSE publishes %d's trading holidays in December: add them to \"holidays\" in core/nse.json "
                   "and %d to \"calendar_years\" before 1 January"
                   % (d.year + 1, d.year + 1))
    age = (d - datetime.date.fromisoformat(CHARGES_READ)).days
    if age > 365:
        out.append("the charges were read from zerodha.com/charges on %s, %d days ago: check them, and the "
                   "\"charges\" in trader/config.json" % (CHARGES_READ, age))
    return out


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


EX_WORDS = re.compile(r"\bex[- ]|dividend|split|bonus|rights|demerger", re.I)


def event_kind(sym, what, kind=None):
    if kind in ("results", "ex-date", "market"):
        return kind
    if str(sym).upper() in ("MARKET", "*"):
        return "market"
    return "ex-date" if EX_WORDS.search(str(what or "")) else "results"


def load_events(doc):
    """trader/events.json -> {SYM: [(date, what, kind)]}; the whole market's events under "MARKET".
    {"events": [{"symbol", "date", "what", "kind"}]} or {SYM: [dates]}. kind: results (the default),
    ex-date (dividend, split, bonus: told from the words when not given) or market (symbol MARKET)."""
    out = {}
    if not doc:
        return out
    rows = doc.get("events") if isinstance(doc, dict) and "events" in doc else None
    if rows is None and isinstance(doc, dict):
        rows = [{"symbol": k, "date": d} for k, v in doc.items() for d in (v if isinstance(v, list) else [v])]
    for r in rows or []:
        if isinstance(r, dict) and r.get("symbol") and re.match(r"^\d{4}-\d{2}-\d{2}$", str(r.get("date", ""))):
            sym = str(r["symbol"]).upper()
            sym = "MARKET" if sym == "*" else sym
            what = r.get("what") or ("market event" if sym == "MARKET" else "results")
            out.setdefault(sym, []).append((str(r["date"]), what, event_kind(sym, what, r.get("kind"))))
    return out


STOP_FILE = os.path.join("trader", "STOP")   # the stop switch: while this file exists, no new entries


def stop_reason(path=None):
    """The stop switch (core/trader.py stop): a file whose presence blocks every new entry, paper or live, until it
    is removed (trader.py resume). Exits are never blocked. None when the switch is off."""
    path = path or STOP_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            why = " ".join(fh.read().split())[:200]
    except OSError:
        why = ""
    return "stop switch: %s is there%s: no new entries until it is removed (trader.py resume)" % (
        path, (" (%s)" % why) if why else "")


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          bands=None, prev_close=None, sectors=None, events=None, surveillance=None, forward=None, morning=None,
          stop=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    bands        {SYM: band percent, or None for no band}, from trader/bands.json
    prev_close   {SYM: previous close}, from trader/prices.json
    sectors      {SYM: industry}, from trader/sectors.json
    events       {SYM: [(date, what)]}, from trader/events.json (results dates)
    surveillance {"symbols": {SYM: list}, "updated": date}, from trader/surveillance.json
    forward      trader-broker's forward test (trader/forward.json): its verdicts unlock live trading
    morning      {"date", "limits"}: the limits at the day's first check (trader/state/limits.json)
    stop         the stop switch's reason; None reads trader/STOP (stop_reason), False ignores it"""
    c = dict(DEFAULTS)
    c.update(inr_keys(cfg or {}))
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    held_warns = []
    if morning and morning.get("date") == (today or to_ist(now_utc).date().isoformat()):
        eff, held_warns = hold_morning_limits(c, morning.get("limits") or {})
        c.update(eff)
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
    halt = stop_reason() if stop is None else (stop or None)
    if halt:
        win_reasons = [halt] + list(win_reasons)
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
    for w in staleness(today_d):
        day_warn.append(w)
    rates0 = c.get("charges") or {}
    week_from = (today_d - datetime.timedelta(days=today_d.weekday())).isoformat()
    month_from = today_d.replace(day=1).isoformat()
    recent = closed_trades(state, min(week_from, month_from), today, rates0)
    week_net = round(sum(t[2] for t in recent if t[0] >= week_from), 2)
    month_net = round(sum(t[2] for t in recent if t[0] >= month_from), 2)
    still_open = open_today(state, today)
    dd = drawdown(state, _f(c.get("budget")) or (acct["cash"] if portfolio is None else None), rates0)
    loss_cap, dd_note = max_loss_now(c, dd)
    if dd_note:
        day_warn.append("drawdown: " + dd_note)
    side_risk = {"buy": 0.0, "sell": 0.0}
    for r in entries_today:
        sym_r = str(r.get("symbol", "")).upper()
        if sym_r in still_open and _f(r.get("max_loss")):
            side_risk[str(r.get("side", "buy")).lower()] = side_risk.get(str(r.get("side", "buy")).lower(), 0) \
                + _f(r["max_loss"])
    if is_expiry and not c.get("skip_expiry_days"):
        day_warn.append("today is Nifty's weekly F&O expiry: index moves can be sharper and reverse late "
                        "(\"skip_expiry_days\": true blocks new entries on expiry days)")
    index_reason = None
    cap_idx = _f(c.get("max_index_move_pct"))
    n_px = prices.get(INDEX)
    n_pc = {str(k).upper(): _f(v) for k, v in (prev_close or {}).items()}.get(INDEX)
    if cap_idx and n_px and n_pc:
        stale = price_times is not None and (price_times.get(INDEX) is None
                                             or (now_utc - price_times[INDEX]).total_seconds() > FRESH_MINUTES * 60)
        move = 100.0 * (n_px / n_pc - 1)
        if stale:
            day_warn.append("the Nifty 50 price in trader/prices.json is older than %d minutes: the index-move "
                            "check is off (alerts.py quote ^NSEI --write)" % FRESH_MINUTES)
        elif abs(move) >= cap_idx - 1e-9:
            index_reason = ("index: Nifty 50 is %+.2f%% from yesterday's close (%s to %s), past your %s%% limit: a "
                            "day this big is a panic or a squeeze, not a setup%s"
                            % (move, _p(n_pc), _p(n_px), _num(cap_idx),
                               "; at %d%% the market-wide circuit breaker halts all trading" % MWCB_PCT
                               if abs(move) < MWCB_PCT else "; the market-wide circuit breaker level is reached: "
                               "trading halts, and stops cannot fill during the halt"))
    elif cap_idx and prices and INDEX not in prices:
        day_warn.append("no Nifty 50 price in trader/prices.json: the index-move check is off "
                        "(alerts.py quote ^NSEI INFY.NS --write)")
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
    live = not c.get("paper", True)
    if live:
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(held_warns)
    gate_open, gate_why = (True, "")
    ramp = None
    if live and LIVE_NEEDS_FORWARD_PASS:
        gate_open, gate_why = live_gate(forward, today)
        if gate_open:
            ramp = live_ramp(forward, state, today, c.get("charges"))
            if ramp["stop"]:
                gate_open, gate_why = False, "live stop: %s. Back to paper: re-run the forward test" % ramp["stop"]
            elif ramp["factor"] < 1:
                top_warn.append("live ramp: %d live trades since the forward test passed (%s), so %d %% size: the max "
                                "loss per trade is %s until the next good block of %d"
                                % (ramp["trades"], ramp["since"], int(ramp["factor"] * 100),
                                   money((_f(c.get("max_loss_per_trade_inr")) or 0) * ramp["factor"]), RAMP_BLOCK))
    if ramp and ramp["factor"] < 1 and loss_cap:
        loss_cap = round(min(loss_cap, (_f(c.get("max_loss_per_trade_inr")) or 0) * ramp["factor"]), 2)
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
                # the worse fill for the risk: the higher price for a buy, the lower for a sale
                ref, ref_from = (min if side == "sell" else max)(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and abs(live - plan_entry) > CENT:
                    info.append("%s last %s is %s the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), "below" if side == "sell" else "above", _p(plan_entry), _p(ref)))
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
        shorting = False
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                shorting = q > held + 1e-9
            elif value is not None:
                shorting = value > held_value + CENT
            else:
                shorting = held <= 0
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
        if value is not None and (side == "buy" or shorting) and kind == "entry":
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
            if product != "MIS" and not shorting:
                warns.append("product %s is delivery, not intraday: delivery STT is 0.1%% on both sides and "
                             "nothing squares it off for you" % product)
            pos_after = abs(held_value) + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if shorting:
            if not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))
            elif product != "MIS":
                reasons.append("short: a short sale on the NSE cash market must be intraday (MIS); a %s short "
                               "cannot be carried overnight and the exchange auctions what is not delivered"
                               % product)

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
                    cap = loss_cap
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, plus %s charges = %s, over "
                                     "your %s max loss per trade%s" % (_num(shares), _p(risk_ps), money(price_loss),
                                                                       money(charges), money(max_loss), money(cap),
                                                                       " (cut for the drawdown)" if dd_note else ""))
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
            for label, net, cap_key, since, when in (
                    ("weekly loss", week_net, "max_weekly_loss_inr", week_from, "next Monday"),
                    ("monthly loss", month_net, "max_monthly_loss_inr", month_from, "next month")):
                cap_l = _f(c.get(cap_key))
                if cap_l and max_loss is not None and max(-net, 0) + max_loss > cap_l + CENT:
                    later.append("%s: %s realised since %s, plus %s at risk on this one, is over your %s %s "
                                 "limit: no new entries until %s"
                                 % (label, ("-" if net < 0 else "+") + money(abs(net)), since, money(max_loss),
                                    money(cap_l), label.split()[0], when))
            cap_side = _f(c.get("max_same_side_risk_inr"))
            if cap_side and max_loss is not None:
                used_side = side_risk.get(side, 0.0)
                if used_side + max_loss > cap_side + CENT:
                    later.append("direction: %s already at risk in open %s today, plus %s = %s, over your %s "
                                 "same-direction limit: one market move would hit them all"
                                 % (money(used_side), "longs" if side == "buy" else "shorts", money(max_loss),
                                    money(used_side + max_loss), money(cap_side)))
            cap_sec = int(c.get("max_entries_per_sector") or 0)
            sec = sectors.get(sym)
            if cap_sec and sec and sector_count.get(sec, 0) >= cap_sec:
                later.append("sector: %d %s entries today already, your limit is %d a sector: one move in the "
                             "sector would hit them all" % (sector_count[sec], sec, cap_sec))
            elif sectors and not sec:
                warns.append("%s is not in trader/sectors.json: its sector is unknown" % sym)
            if not gate_open:
                later.append("live: %s. Live entries are locked; paper trading is open" % gate_why)
            if index_reason:
                later.append(index_reason)
            for d, what, ekind in events.get("MARKET", []):
                if ekind == "market" and d == today and c.get("skip_market_event_days"):
                    later.append("market event: %s today (trader/events.json): the whole market can swing on it, "
                                 "so no new entries today" % what)
            for d, what, ekind in events.get(sym, []):
                if ekind == "ex-date" and d == today and c.get("skip_ex_dates"):
                    later.append("ex-date: %s goes %s today (trader/events.json): the open drops by it, which "
                                 "breaks the gap and price-band arithmetic, so no new entries in it today" % (sym, what))
            if c.get("skip_results_days"):
                for d, what, ekind in events.get(sym, []):
                    if ekind != "results":
                        continue
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
                if not long and ref <= lower * (1 + BAND_MARGIN_PCT / 100.0) + CENT:
                    later.append("band: %s at %s is within %s%% of its lower price band %s (%s%% under the previous "
                                 "close %s): at the band buyers vanish and it can lock"
                                 % (sym, _p(ref), _num(BAND_MARGIN_PCT), _p(lower), _num(band), _p(pc)))
                if long and stop and stop < lower - CENT:
                    warns.append("the stop %s is under the lower price band %s: if %s locks at the band, the stop "
                                 "cannot fill until it reopens" % (_p(stop), _p(lower), sym))
                if not long and stop and stop > upper + CENT:
                    warns.append("the stop %s is over the upper price band %s: if %s locks at the band, the stop "
                                 "cannot fill and the short cannot be bought back until it reopens"
                                 % (_p(stop), _p(upper), sym))
            elif bands and sym not in bands:
                warns.append("%s is not in trader/bands.json: its price band is unknown (run scan.py universe)" % sym)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s with %s charges%s, reward:risk after charges %s%s"
                            % (money(max_loss), money(charges or 0), (" (limit %s)" % money(loss_cap))
                               if loss_cap else "",
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
                side_risk[side] = side_risk.get(side, 0.0) + (max_loss or 0.0)
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif shorting:
                if value is not None:
                    acct["cash"] -= value            # the margin the short ties up, value / leverage at most
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
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
    ap.add_argument("--forward", default="trader/forward.json",
                    help="trader-broker's forward test: its verdicts unlock live trading")
    ap.add_argument("--limits-state", default="trader/state/limits.json",
                    help="the day's first limits: loosening waits for the next day")
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
        today = a.today or to_ist(now).date().isoformat()
        morning = read_json(a.limits_state, "the day's limits", required=False)
        if not morning or morning.get("date") != today:
            c0 = dict(DEFAULTS)
            c0.update(inr_keys(cfg or {}))
            morning = {"date": today, "limits": limits_snapshot(c0)}
            if not a.no_write:
                d = os.path.dirname(a.limits_state)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(a.limits_state, "w", encoding="utf-8") as fh:
                    json.dump(morning, fh, indent=2)
                    fh.write("\n")
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, bands, load_prev_close(pdoc),
                    sdoc.get("sectors", sdoc) if isinstance(sdoc, dict) else {},
                    load_events(read_json(a.events, "events", required=False)),
                    read_json(a.surveillance, "surveillance lists", required=False),
                    read_json(a.forward, "forward test", required=False), morning)
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
