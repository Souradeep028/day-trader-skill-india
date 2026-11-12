#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # NSE's lists -> trader/universe.txt + trader/bands.json
    python3 scan.py surveillance asm.csv gsm.csv      # NSE's ASM/GSM lists -> trader/surveillance.json
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --watchlist trader/nifty50.txt   # or --symbols INFY.NS,TCS.NS, or --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 09:40 --or-minutes 15    # cut the clock at 9:40 AM IST
    python3 scan.py orb --offline wall5m.json.gz --now "2026-11-05 09:40" --min-price 0

universe  Downloads NSE's equity list (EQUITY_L.csv) and price band list
          (sec_list.csv) from nsearchives.nseindia.com. Keeps every stock in
          the EQ series, as SYMBOL.NS (Yahoo's name for it). BE and BZ are
          trade-for-trade (delivery only, no intraday), SME boards are left
          out too. Writes trader/universe.txt and trader/bands.json: each
          stock's price band (2, 5, 10 or 20 % from the previous close, or
          none for F&O stocks), which trader-risk uses to stop a buy right
          under the upper band. NSE revises bands often: run it each morning.

surveillance  Reads NSE's ASM and GSM lists (Additional and Graded Surveillance
          Measures: stocks NSE watches for unusual moves, with extra margins and
          often no intraday leverage). NSE's report pages turn scripts away, so
          save each CSV from a browser (nseindia.com/reports/asm, the long-term
          and short-term tabs, and nseindia.com/reports/gsm) and pass the files:
          scan.py surveillance asm-lt.csv asm-st.csv gsm.csv. Writes
          trader/surveillance.json; trader-risk blocks new entries in those stocks.

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (5) and regularMarketVolume >=
          --min-volume (1,000,000). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol (1 day of 5-minute bars, 20 a request); a chart goes on only
          if a completed bar after the range closed at or above the highest
          close of the range (a necessary condition). Stage 2: each candidate's
          v8 chart (real highs and lows): range high = the highest high of the
          range bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time. Stage 2 fetches at most --max-confirm (200) charts,
          the most traded candidates first (price x volume so far), and says
          so when it stops short. A chart is usable only when every 5-minute
          bar from 9:15 to the cutoff is there (a missing bar is a halt or no
          trades). Floors: --min-price 5 (last close), --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are IST (UTC+5:30). --now pins the
clock ("2026-11-05 09:40", IST time; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with EQUITY_L.csv and sec_list.csv; daily and orb take a folder of <T>.json
v8 chart files or spark responses, or one file that is a spark response, a
v8 chart, or a bundle {"INFY.NS": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "core")):  # the shared tables: core/tradercore.py
    if os.path.isfile(os.path.join(_p, "tradercore.py")) and _p not in sys.path:
        sys.path.append(_p)
import tradercore  # noqa: E402

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Souradeep028/day-trader-skill-india)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NSE_FILES = ("EQUITY_L.csv", "sec_list.csv")
NSE_URL = "https://nsearchives.nseindia.com/content/equities/"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_BANDS = "trader/bands.json"
BY_HAND = ("\nNSE's site often turns away scripts. Save the two files from a browser instead:\n"
           "  https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv\n"
           "  https://nsearchives.nseindia.com/content/equities/sec_list.csv\n"
           "into one folder, then run: scan.py universe --offline <that folder>")
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

NSE_HOLIDAYS = tradercore.holidays()  # NSE equity segment trading holidays: core/nse.json, the one copy
NSE_EARLY_CLOSE = tradercore.early_close()  # NSE has no scheduled early closes
CALENDAR_YEARS = tradercore.calendar_years()
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(13, 0)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_offset(utc):
    """Hours from UTC for India: always +5.5 (IST, UTC+5:30, has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=ist_offset(utc))


def ist_to_utc(local):
    return local - datetime.timedelta(hours=ist_offset(local))


def ts_to_ist(ts):
    """Unix seconds -> naive IST time. Never uses the machine's time zone."""
    return to_ist(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-11-05 09:40" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in India time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE)


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    s = sym.upper()
    return (s if s.endswith(".NS") else s.replace(".", "-"))


def from_yahoo(sym):
    """Yahoo's name back to ours: an NSE symbol (BAJAJ-AUTO.NS, M&M.NS) stays as it is."""
    s = str(sym).upper()
    return s if s.endswith(".NS") else s.replace("-", ".")


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(urllib.parse.quote(to_yahoo(s), safe="") for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(to_yahoo(sym), safe=""), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (BRK-B -> BRK.B)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), from_yahoo(ysym))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        sym = from_yahoo(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

SERIES_IN = ("EQ",)   # BE and BZ are trade-for-trade: delivery only, no intraday
SERIES_WORDS = {"BE": "trade-for-trade (BE, delivery only)", "BZ": "trade-for-trade (BZ, delivery only)",
                "SM": "SME board", "ST": "SME board", "IV": "InvIT", "RR": "REIT"}
NSE_SYMBOL = re.compile(r"^[A-Z0-9&\-]{1,20}$")


def parse_csv(text, what):
    """NSE archive CSV -> rows as dicts with trimmed keys and values."""
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text.replace("\r", "").lstrip("\ufeff"))))
    if not rows:
        raise ScanError("%s is empty" % what)
    hdr = [h.strip().upper() for h in rows[0]]
    return [dict(zip(hdr, [c.strip() for c in r])) for r in rows[1:] if any(c.strip() for c in r)]


def build_universe(texts):
    """{"EQUITY_L.csv": text, "sec_list.csv": text} -> (kept [(SYM.NS, name)], counts, bands).

    kept: every EQ-series stock in NSE's equity list. bands: {SYM.NS: 2 | 5 | 10 | 20 | None}
    from NSE's price band list, None for "No Band" (F&O stocks: a dynamic band instead)."""
    for f in NSE_FILES:
        if f not in texts:
            raise ScanError("missing %s" % f)
    eq = parse_csv(texts["EQUITY_L.csv"], "EQUITY_L.csv")
    if not eq or "SYMBOL" not in eq[0] or "SERIES" not in eq[0]:
        raise ScanError("EQUITY_L.csv does not look like NSE's equity list (no SYMBOL and SERIES columns)")
    kept, counts = {}, {"rows": len(eq)}
    for r in eq:
        sym, series = r.get("SYMBOL", "").upper(), r.get("SERIES", "").upper()
        if series not in SERIES_IN:
            why = SERIES_WORDS.get(series, "series %s" % (series or "blank"))
        elif not NSE_SYMBOL.match(sym):
            why = "not a plain symbol"
        else:
            why = "keep"
            kept[sym + ".NS"] = " ".join(r.get("NAME OF COMPANY", "").split())
        counts[why] = counts.get(why, 0) + 1
    bands = {}
    sl = parse_csv(texts["sec_list.csv"], "sec_list.csv")
    if sl and not {"SYMBOL", "SERIES", "BAND"} <= set(sl[0]):
        raise ScanError("sec_list.csv does not look like NSE's price band list (no Symbol, Series, Band)")
    for r in sl:
        if r.get("SERIES", "").upper() not in SERIES_IN:
            continue
        sym = r.get("SYMBOL", "").upper() + ".NS"
        band = r.get("BAND", "")
        try:
            bands[sym] = float(band)
        except ValueError:
            bands[sym] = None
    return sorted(kept.items()), counts, bands


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most dollars traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST time),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day)
    close_t = s[1] if s else CLOSE
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = ts_to_ist(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < OPEN or et.time() >= close_t:
            continue  # the pre-open auction, after hours, the 15:30 closing print
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[et] = {"start": et, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 09:40."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 09:40." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


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
    return "₹" + indian_commas("{:.2f}".format(x))


# ---------------------------------------------------------------- commands

def parse_surveillance(text, kind):
    """One of NSE's ASM or GSM report CSVs (saved from nseindia.com/reports/asm or /gsm) -> {SYM.NS: label}.
    Finds the SYMBOL column wherever the header row is, and a stage column if there is one."""
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text.replace("\r", "").lstrip("\ufeff"))))
    head = next((i for i, r in enumerate(rows) if any(c.strip().upper() == "SYMBOL" for c in r)), None)
    if head is None:
        raise ScanError("no SYMBOL column in the %s file: save the CSV from NSE's %s report page" % (kind, kind))
    hdr = [c.strip().upper() for c in rows[head]]
    si = hdr.index("SYMBOL")
    stage = next((i for i, h in enumerate(hdr) if "STAGE" in h), None)
    sub = next((i for i, h in enumerate(hdr) if h in ("SURVEILLANCE", "SURV DESC", "ASM TYPE", "LIST")), None)
    out = {}
    for r in rows[head + 1:]:
        if len(r) <= si or not r[si].strip():
            continue
        sym = r[si].strip().upper()
        if not re.match(r"^[A-Z0-9&\-]{1,20}$", sym):
            continue
        label = kind
        if sub is not None and len(r) > sub and r[sub].strip():
            label += " " + r[sub].strip()
        if stage is not None and len(r) > stage and r[stage].strip():
            st = r[stage].strip()
            label += " " + (st if st.lower().startswith("stage") else "Stage " + st)
        out[sym + ".NS"] = label
    return out


def cmd_surveillance(a):
    syms = {}
    files = []
    for path in a.files:
        kind = a.kind or ("GSM" if "gsm" in os.path.basename(path).lower() else "ASM")
        with open(path, encoding="utf-8", errors="replace") as fh:
            got = parse_surveillance(fh.read(), kind)
        syms.update(got)
        files.append({"file": os.path.basename(path), "list": kind, "stocks": len(got)})
    now = to_ist(parse_now(a.now))
    doc = {"created_by": "trader-scanner", "updated": now.date().isoformat(), "files": files,
           "note": "Stocks on NSE's Additional or Graded Surveillance Measure lists. trader-risk blocks new "
                   "entries in them. NSE changes the lists every week: save the CSVs from "
                   "nseindia.com/reports/asm and nseindia.com/reports/gsm and run scan.py surveillance again.",
           "symbols": dict(sorted(syms.items()))}
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        for f in files:
            print("%s: %d stock%s on the %s list." % (f["file"], f["stocks"], "" if f["stocks"] == 1 else "s",
                                                       f["list"]))
        print("%d stocks in all%s." % (len(syms), "" if a.no_write else ", written to %s" % a.out))
    return 0


def cmd_universe(a):
    t0 = time.time()
    texts = {}
    for k, fname in enumerate(NSE_FILES):
        if a.offline:
            p = os.path.join(a.offline, fname)
            if not os.path.exists(p):
                raise ScanError("no %s in %s" % (fname, a.offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        else:
            if k and a.pause:
                SLEEP(a.pause)
            try:
                texts[fname] = http_get(NSE_URL + fname, timeout=60)
            except urllib.error.HTTPError as e:
                raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s.%s" % (e.code, fname, BY_HAND))
            except (urllib.error.URLError, OSError) as e:
                raise ScanError("could not reach nsearchives.nseindia.com: %s.%s" % (getattr(e, "reason", e), BY_HAND))
    kept, counts, bands = build_universe(texts)
    stamp = parse_now(a.now).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
        write_json(a.bands, {"created_by": "trader-scanner", "as_of": stamp,
                             "source": NSE_URL + "sec_list.csv",
                             "note": "Price band in percent from the previous close; null = no band (F&O stocks "
                                     "have a dynamic band instead). NSE revises bands often: run scan.py universe "
                                     "each morning.",
                             "bands": {s: bands.get(s) for s, _ in kept if s in bands}})
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    by_band = {}
    for s, _ in kept:
        b = bands.get(s, "missing") if s in bands else "missing"
        by_band[b] = by_band.get(b, 0) + 1
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "dropped": dropped,
                          "bands": {("none" if k is None else str(k)): v for k, v in by_band.items()},
                          "out": None if a.no_write else a.out, "bands_out": None if a.no_write else a.bands,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE equity list: %s rows." % "{:,}".format(counts.get("rows", 0)))
        print("Kept %s stocks in the EQ series (the ones you can trade intraday)." % "{:,}".format(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        parts = []
        for k in (2.0, 5.0, 10.0, 20.0):
            if by_band.get(k):
                parts.append("%s at %g%%" % ("{:,}".format(by_band[k]), k))
        if by_band.get(None):
            parts.append("%s with no band (F&O stocks)" % "{:,}".format(by_band[None]))
        if by_band.get("missing"):
            parts.append("%s not in the band list" % "{:,}".format(by_band["missing"]))
        print("Price bands: " + ", ".join(parts) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line) and %s (for trader-risk)." % (a.out, a.bands))
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (₹%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           "{:,}".format(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-14s %11s  RSI %5.1f  %s" % (h["symbol"], money(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-14s %11s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def _p(x):
    return indian_commas("{:.2f}".format(x))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's lists -> trader/universe.txt and trader/bands.json")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv and sec_list.csv")
    p.add_argument("--bands", default=DEFAULT_BANDS, help="where the price bands go (default %s)" % DEFAULT_BANDS)
    p.add_argument("--now", help="pretend it is this IST time")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("surveillance", help="NSE's ASM and GSM CSVs, saved from a browser -> trader/surveillance.json")
    p.add_argument("files", nargs="+", metavar="CSV", help="the CSVs from nseindia.com/reports/asm and /reports/gsm")
    p.add_argument("--kind", choices=("ASM", "GSM"), help="the list (default: GSM if the file name says gsm, else ASM)")
    p.add_argument("--out", default="trader/surveillance.json")
    p.add_argument("--now", help="pretend it is this IST time")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated symbols (INFY.NS,TCS.NS) instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-11-05 09:40" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 5.0, 1000000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 5.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 14:45)")
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "surveillance": cmd_surveillance, "daily": cmd_daily,
                "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
