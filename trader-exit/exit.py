#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. A long by default;
--side sell (or a short entry.json) plans an intraday short: the stop above the
entry, the target below. It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: stop 1149.80, target 1171.20
    python3 exit.py plan --symbol INFY.NS --entry 1160.50 --or-high 1154.70 --or-low 1144.90
    python3 exit.py plan --stop atr:1.5 --offline INFY.NS.json  # ATR needs the session's bars
    python3 exit.py plan --symbol TCS.NS --entry 3012.40 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Prices land on the NSE tick: 0.01 under 250, 0.05 from 250, 0.10 from 1,000,
0.50 from 5,000, 1.00 from 10,000 and 5.00 from 20,000 (NSE circular CMTR/67133,
in force since 15 April 2025). NSE reviews each stock's tick every month, so a
stock that just crossed 1,000 or 5,000 can keep its old tick for weeks: the
plan uses the tick trader-entry saw the stock trade at (entry.json "tick"),
else --tick X, else the price rule.

Stops (a long's stop rounds DOWN to the tick, a short's UP: away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low (a long)
  or-high    the opening-range high (a short)
  price:X    your price
  pct:X      X percent under the entry (over it, for a short)
  atr:K      K times the 14-bar ATR under the entry (over it), on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
                               (entry - R x risk for a short, risk = stop - entry)
  price:X                      your price

Refused, with the reason: a stop on the wrong side of the entry (at or above
it for a long, at or below it for a short), a target on the wrong side, a risk
under 0.05% of the price (too tight to survive the spread), an entry.json that
has not triggered (unless you give --entry).

flat_by (default 15:10, or "flat_by" in trader/config.json): out by then, win
or lose, and before your broker squares off intraday (MIS) positions itself
(Zerodha: 3:20 to 3:25 PM, for a fee of 50 rupees plus GST).
"min_reward_risk" in the config (default 1.0) only warns here; trader-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "core")):  # the shared tables: core/tradercore.py
    if os.path.isfile(os.path.join(_p, "tradercore.py")) and _p not in sys.path:
        sys.path.append(_p)
import tradercore  # noqa: E402

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:10", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
TICKS, TOP_TICK = tradercore.ticks(Decimal)  # NSE tick sizes: core/nse.json
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

NSE_HOLIDAYS = tradercore.holidays()  # NSE equity segment trading holidays: core/nse.json, the one copy
NSE_EARLY_CLOSE = tradercore.early_close()  # NSE has no scheduled early closes
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(13, 0)


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_offset(utc):
    """Hours from UTC for India: always +5.5 (IST, UTC+5:30, has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=ist_offset(utc))


def ist_to_utc(local):
    return local - datetime.timedelta(hours=ist_offset(local))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is IST time."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-11-05 09:40" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an IST time like 15:15, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def session(d):
    """(open_time, close_time) in India time, or None if closed all day."""
    if d.weekday() >= 5 or d.isoformat() in NSE_HOLIDAYS:
        return None
    return OPEN, (EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE)


# ---------------------------------------------------------------- money math (Decimal, to the cent)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("$₹").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def cents(x):
    """Round half up to the cent."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_cents(x):
    """A long's stop: round down, away from the entry."""
    return D(x).quantize(CENT, rounding=ROUND_FLOOR)


def tick_for(price):
    """The NSE tick size for a price (CMTR/67133, from 15 April 2025)."""
    p = D(price)
    for below, tick in TICKS:
        if p < below:
            return tick
    return TOP_TICK


def floor_tick(x, tick):
    """Round down to a multiple of the tick (a long's stop, away from the entry)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(CENT)


def ceil_tick(x, tick):
    """Round up to a multiple of the tick (a short's stop, away from the entry)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_CEILING) * tick).quantize(CENT)


def round_tick(x, tick):
    """Round half up to a multiple of the tick (a target)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick).quantize(CENT)


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
    return "₹%s" % indian_commas(format(D(x).quantize(CENT), "f"))


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    if t in ("or-high", "or_high", "high"):
        return "or_high", None
    m = re.match(r"^(price|pct|atr):\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, or-high, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=None, side="buy"):
    """The stop, the target and the risk, on the NSE tick. Raises PlanError with the reason.
    side "sell": a short, with the stop above the entry and the target below it."""
    long = side != "sell"
    under = "under" if long else "over"
    e = cents(entry)
    if e <= 0:
        raise PlanError("the entry price must be above ₹0")
    tick = D(tick) if tick else tick_for(e)
    if tick <= 0:
        raise PlanError("the tick size must be above 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low", "or_high"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = cents(or_high), cents(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = ₹%s" % (money(oh), money(ol), num(raw))
        elif stop_rule == "or_low":
            raw, label = ol, "opening-range low"
        else:
            raw, label = oh, "opening-range high"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100) if long else e * (1 + stop_param / 100)
        label = "%s%% %s the entry" % (num(stop_param), under)
        detail = "%s x (1 %s %s%%) = ₹%s" % (money(e), "-" if long else "+", num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a if long else e + stop_param * a
        label = "%s x ATR(%d) %s the entry" % (num(stop_param), atr_bars, under)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = floor_tick(raw, tick) if long else ceil_tick(raw, tick)
    if stop != D(raw):
        what = "rounded %s to the %s tick" % ("down" if long else "up", num(tick))
        detail = (detail + ", " + what if detail else "₹%s %s" % (num(raw), what))
    if long and stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if not long and stop <= e:
        raise PlanError("the stop %s (%s) is at or below the entry %s: a short's stop goes above the entry%s"
                        % (money(stop), label, money(e), " (use or-high or or-mid)" if stop_rule == "or_low" else ""))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % money(stop))
    risk = e - stop if long else stop - e
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s %s the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), under, money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)), money(e)))
    if target_rule == "r":
        target = round_tick(e + target_param * risk if long else e - target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = round_tick(target_param, tick)
        t_label = None
    if long and target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    if not long and (target >= e or target <= 0):
        raise PlanError("the target %s is at or above the entry %s: a short's target goes below it"
                        % (money(target), money(e)))
    rr = (((target - e) if long else (e - target)) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    s = sym.upper()
    ysym = (s if s.endswith(".NS") else s.replace(".", "-"))
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, ysym + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_ist(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym, safe=""), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None):
    """Closed, on-grid bars of one IST session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_ist(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_ist(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). The config's flat-by moves up with a 1:00 p.m. early close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = session(day) if day else None
    if s:
        close_dt = datetime.datetime.combine(day, s[1])
        at = datetime.datetime.combine(day, t)
        if at >= close_dt:
            if explicit or s[1] == CLOSE:
                raise PlanError("%s %s is at or after the %s close on %s"
                                % ("--flat-by" if explicit else "flat_by in the config", hm(t), ampm(s[1]), day))
            moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
            if moved.time() <= OPEN:
                raise PlanError("flat_by %s in the config is at or after the close" % hm(t))
            t = moved.time()
            note = "moved up for the 1:00 p.m. early close (%s)" % NSE_EARLY_CLOSE.get(day.isoformat(), "")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    side = "sell" if a.side in ("sell", "short") else "buy"
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags", "tick": None,
                "side": side}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        dside = str(doc.get("side") or "buy").lower()
        if dside not in ("buy", "long", "sell", "short"):
            raise PlanError("%s has side %r: buy or sell" % (path, doc.get("side")))
        if a.side not in (None, "buy", "long") and dside in ("buy", "long"):
            raise PlanError("%s is a buy but you asked for --side %s: watch for the short with "
                            "/trader-entry --side sell first" % (path, a.side))
        side = "sell" if dside in ("sell", "short") else "buy"
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path, "tick": doc.get("tick"),
                "side": side}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_ist(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, a.tick or info.get("tick"), info["side"])
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(cents(info["or_high"])), "low": float(cents(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": info["side"], "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "tick": float(c["tick"]),
            "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = to_ist(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    short_ = plan.get("side") == "sell"
    setup = ({"orb": "opening-range breakdown", "level": "close below a level"} if short_ else
             {"orb": "opening-range breakout", "level": "close above a level"}).get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s %s, %s, %s on %s bars" % (
        plan["symbol"], "short" if short_ else "long", day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_ist(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:15 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", choices=["buy", "long", "sell", "short"],
                   help="buy (default) or sell for a short; from entry.json it follows the entry's side")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--tick", type=float, help="the stock's NSE tick size (default: from the price)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-05 09:40"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
