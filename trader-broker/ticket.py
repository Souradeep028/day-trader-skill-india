#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places a real order. It does
its work around the one moment that matters, your yes:

  show        renders each proposed order as a plain-English ticket plus the
              exact Zerodha Kite place_order calls Claude would make (the MIS
              entry, then the SL-M stop-loss and the LIMIT target once the
              entry fills). A bracket shows the max loss at the stop with
              charges. An order is READY only if trader-risk passed THIS exact
              order today (matched by fingerprint) and nobody has decided on it.
  approve     records your explicit yes for ONE order, with your reply word for
              word. It refuses "yes to all", "sure", "ok", a yes that names a
              different stock or amount, and any order that is not READY.
  decline     records a no.
  paper       PAPER account ("paper": true, the default): fills an approved
              order itself at the live price (--price, or a fresh price in
              trader/prices.json) and keeps the bracket's stop-loss and target
              as open paper orders. No broker is involved.
  paper-check fills open paper stop-losses and targets the price has reached
              (an SL-M fills at the price, which can be worse than the stop;
              a target fills at its limit) and prints the day's P&L.
  result      LIVE account: records what happened after the Allow once / Deny
              card: placed (with Kite's order id), denied, or failed.
  fill        records a fill in trader/portfolio.json (cash, shares, charges).
  flatten     builds a sell ticket that closes a symbol's open position by
              flat_by. It still goes through trader-risk and your yes.
  log         prints today's decisions and the realised P&L after charges.
  journal     every closed trade (paper, live or both): win rate, average R, P&L
              after charges, the longest run of losses, by day and by stock.
  tax         a financial year's intraday round trips as a worksheet: gross P&L,
              charges by type, net, and the turnover the tax-audit limits use.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py paper --index 1 --price 1160.50
    python3 ticket.py paper-check --price INFY.NS=1164.80
    python3 ticket.py result --index 1 --status placed --broker-id 251105000123456
    python3 ticket.py fill --symbol INFY.NS --side buy --qty 41 --price 1160.50
    python3 ticket.py flatten --symbol INFY.NS --qty 41
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
IST dates; --now "2026-11-05 09:40" pins the clock. Charges are Zerodha's
equity intraday rates on the NSE; "charges" in trader/config.json overrides.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "notional", "type", "time_in_force",
                "limit_price", "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                "stop_loss_limit_price", "advanced_instructions")
TIF_WORDS = {"day": "good for today", "ioc": "immediate or cancel"}
PRODUCT_WORDS = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)"}
KITE_TYPES = {"market": "MARKET", "limit": "LIMIT", "stop": "SL-M", "stop_limit": "SL"}
CHARGES = {  # Zerodha, equity intraday on the NSE: zerodha.com/charges, read 2026-11-08
    "brokerage_pct": 0.03, "brokerage_max": 20.0, "stt_sell_pct": 0.025, "exchange_pct": 0.00307,
    "sebi_per_crore": 10.0, "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}
FRESH_MINUTES = 15
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-11-05 09:40\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-10-19T10:15:00+05:30" (IST wall clock with its offset)."""
    return "%s+05:30" % to_ist(now_utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def order_charges(side, value, rates=None):
    """Charges on one executed intraday order of this value (rupees), each to the paisa.
    Same function in trader-budget, trader-risk and trader-timemachine."""
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


def tradingsymbol(sym):
    """INFY.NS -> INFY: Kite names NSE stocks without Yahoo's suffix."""
    s = str(sym or "").strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def _kite_price(v):
    return round(float(v), 2)


def kite_call(order, side, typ, qty, price=None, trigger=None):
    """One Kite place_order call. Kite takes numbers as numbers."""
    out = {"variety": "regular", "exchange": str(order.get("exchange") or "NSE").upper(),
           "tradingsymbol": tradingsymbol(order.get("symbol")), "transaction_type": side.upper(),
           "quantity": int(round(float(qty))), "product": str(order.get("product") or "MIS").upper(),
           "order_type": KITE_TYPES.get(typ, typ.upper()),
           "validity": str(order.get("time_in_force") or "day").upper()}
    if price is not None:
        out["price"] = _kite_price(price)
    if trigger is not None:
        out["trigger_price"] = _kite_price(trigger)
    out["tag"] = "dts" + fingerprint(order)[:8]
    return out


def tool_input(order):
    """The Kite calls for this order, in the order Claude makes them:
    [{"step", "when", "tool", "input"}]. A bracket is three orders: Kite has no
    bracket for equity, so the stop-loss and the target go in after the entry
    fills, and the one left open is canceled when the other fills.
    "why" and "plan" are ours and are never sent."""
    side = str(order.get("side", "buy")).lower()
    typ = str(order.get("type", "market")).lower()
    qty = order.get("qty")
    first = kite_call(order, side, typ, qty,
                      order.get("limit_price") if typ in ("limit", "stop_limit") else None,
                      order.get("stop_price") if typ in ("stop", "stop_limit") else None)
    calls = [{"step": "entry" if side == "buy" else "order", "when": "now", "tool": "place_order", "input": first}]
    if is_bracket(order):
        out = "sell" if side == "buy" else "buy"
        if order.get("stop_loss_stop_price") is not None:
            if order.get("stop_loss_limit_price") is not None:
                leg = kite_call(order, out, "stop_limit", qty, order["stop_loss_limit_price"],
                                order["stop_loss_stop_price"])
            else:
                leg = kite_call(order, out, "stop", qty, None, order["stop_loss_stop_price"])
            calls.append({"step": "stop-loss", "when": "after the entry fills", "tool": "place_order", "input": leg})
        if order.get("take_profit_limit_price") is not None:
            calls.append({"step": "target", "when": "after the entry fills", "tool": "place_order",
                          "input": kite_call(order, out, "limit", qty, order["take_profit_limit_price"])})
    return calls


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


def _money(v):
    f = float(v)
    s = indian_commas("{:.2f}".format(f))
    return "₹" + (s[:-3] if s.endswith(".00") else s)


def _p(v):
    return "%.2f" % float(v)


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order):
    """(rupees, stop) lost if the stop fills exactly at the stop, charges included, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    ch = order_charges("buy", q * entry)["total"] + order_charges("sell", q * stop)["total"]
    return round(q * int(round(risk * 100)) / 100.0 + ch, 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    prod = str(order.get("product") or "MIS").upper()
    tif = "%s, %s" % (PRODUCT_WORDS.get(prod, prod), tif)
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        parts = ["%s %s %s, %s, %s" % (side, _str_num(order.get("qty")), sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += " (limit %s)" % _p(order["stop_loss_limit_price"])
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("target %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s with charges if the stop fills at %s (it can fill worse in a fast "
                         "market)" % (_money(loss), _p(stop)))
        return parts
    if order.get("notional") is not None:
        what = "%s of %s" % (_money(order["notional"]), sym)
    else:
        q = _str_num(order.get("qty"))
        what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    if typ == "limit":
        how = "limit order at %s" % _money(order.get("limit_price"))
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"))
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_money(order.get("stop_price")), _money(order.get("limit_price")))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order):
    """BUY 41 INFY.NS, market, intraday (MIS), good for today / stop-loss 1149.80 / target 1181.90 /
    max loss about ₹489 with charges ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("notional", "qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price",
              "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&.\-]{0,19}", raw.upper()))
    tokens |= {t[:-3] for t in tokens if t.endswith(".NS")}
    others = {s.upper() for s in batch_symbols} - {sym}
    others |= {o[:-3] for o in others if o.endswith(".NS")}
    others -= {sym[:-3] if sym.endswith(".NS") else sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the order to be placed" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def broker_name(cfg):
    b = str(cfg.get("broker", "zerodha")).lower()
    return {"zerodha": "Zerodha Kite", "kite": "Zerodha Kite"}.get(b, b)


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    paper = cfg.get("paper", True)
    acct = "PAPER account (filled here, no broker)" if paper else "LIVE ACCOUNT, REAL MONEY, %s" % broker_name(cfg)
    lines = ["ORDER %d of %d, %s" % (idx, total, acct)]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    calls = tool_input(order)
    if paper:
        lines.append("  On your yes, ticket.py paper fills it at the live price. For a live account Claude")
        lines.append("  would make these Kite place_order calls:")
    else:
        lines.append("  Claude will call the Kite connector's place_order with exactly:")
    for c in calls:
        lines.append("    %s (%s): %s" % (c["step"], c["when"], json.dumps(c["input"])))
    if is_bracket(order):
        lines.append("  Kite has no bracket for stocks: the stop-loss and the target go in once the entry fills,")
        lines.append("  and when one of them fills the other must be canceled (cancel_order).")
    if ready:
        lines.append("  Reply \"yes\" to %s this one order, or \"no\" to skip it."
                     % ("paper trade" if paper else "place"))
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "zerodha"), "tool": "place_order",
            "calls": tool_input(order), "why": order.get("why", ""),
            "bracket": is_bracket(order), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell). The
    order's charges come out of cash too."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    charges = order_charges(side, cost, cfg.get("charges"))["total"]
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost - charges, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost - charges, 2)
    pf["charges_paid"] = round(float(pf.get("charges_paid", 0)) + charges, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price, cfg=None, paper=False, note=None):
    """A fill in the log, with its charges, for the day's P&L."""
    state = load_state(state_path)
    ch = order_charges(side, float(qty) * float(price), (cfg or {}).get("charges"))
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": _str_num(float(qty)), "price": float(price), "charges": ch["total"],
           "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price))}
    if paper:
        rec["paper"] = True
    if note:
        rec["note"] = note
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)
    return rec


def day_pnl(state, today):
    """Realised P&L today after charges, per symbol: [(symbol, bought, sold, pnl or None, charges)].
    pnl is None while shares are still open."""
    per = {}
    for r in state.get("orders", []):
        if r.get("status") != "filled" or rec_day(r) != today:
            continue
        d = per.setdefault(str(r.get("symbol", "")).upper(), {"bq": 0.0, "bv": 0.0, "sq": 0.0, "sv": 0.0, "ch": 0.0})
        q, p = float(r.get("qty") or 0), float(r.get("price") or 0)
        if r.get("side") == "buy":
            d["bq"] += q
            d["bv"] += q * p
        else:
            d["sq"] += q
            d["sv"] += q * p
        d["ch"] += float(r.get("charges") or 0)
    out = []
    for sym, d in sorted(per.items()):
        closed = abs(d["bq"] - d["sq"]) < 1e-9
        out.append((sym, d["bq"], d["sq"], round(d["sv"] - d["bv"] - d["ch"], 2) if closed else None,
                    round(d["ch"], 2)))
    return out


# ---------------------------------------------------------------- journal and tax

def fy_bounds(label):
    """'FY27' -> ('2026-04-01', '2027-03-31'): India's financial year runs April to March."""
    m = re.match(r"^FY ?(\d{2}|\d{4})$", str(label).strip().upper())
    if not m:
        raise TicketError("--fy wants a financial year like FY27 (April 2026 to March 2027)")
    end = int(m.group(1)) % 100 + 2000
    return "%d-04-01" % (end - 1), "%d-03-31" % end


def round_trips(state, date_from=None, date_to=None, paper=None, rates=None):
    """The day's fills per symbol, paired into round trips (bought and sold back to flat that day).
    Returns (trades, open_positions). A trade: date, symbol, qty, buy and sell value, charges by type,
    gross and net P&L, the entry's planned risk (from the stop recorded with its yes) and R."""
    fills, stops = {}, {}
    for r in state.get("orders", []):
        d = rec_day(r)
        if (date_from and d < date_from) or (date_to and d > date_to):
            continue
        if r.get("status") in ("approved", "placed") and str(r.get("side")) == "buy" and _f(r.get("stop")):
            stops[(d, str(r.get("symbol", "")).upper())] = _f(r["stop"])
        if r.get("status") != "filled":
            continue
        if paper is not None and bool(r.get("paper")) != paper:
            continue
        fills.setdefault((d, str(r.get("symbol", "")).upper()), []).append(r)
    trades, still_open = [], []
    for (d, sym), rs in sorted(fills.items()):
        pos, cur = 0.0, None
        for r in rs:
            q, px, side = float(r.get("qty") or 0), float(r.get("price") or 0), r.get("side")
            if cur is None:
                cur = {"date": d, "symbol": sym, "bought": 0.0, "sold": 0.0, "buy_value": 0.0, "sell_value": 0.0,
                       "charges": {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0,
                                   "gst": 0.0, "total": 0.0}, "paper": bool(r.get("paper"))}
            ch = order_charges(side, q * px, rates)
            for k in cur["charges"]:
                cur["charges"][k] = round(cur["charges"][k] + ch[k], 2)
            if side == "buy":
                cur["bought"] += q
                cur["buy_value"] += q * px
                pos += q
            else:
                cur["sold"] += q
                cur["sell_value"] += q * px
                pos -= q
            if abs(pos) < 1e-9:
                cur["qty"] = cur["bought"]
                cur["gross"] = round(cur["sell_value"] - cur["buy_value"], 2)
                cur["net"] = round(cur["gross"] - cur["charges"]["total"], 2)
                stop = stops.get((d, sym))
                avg_buy = cur["buy_value"] / cur["bought"] if cur["bought"] else 0
                cur["risk"] = round(cur["qty"] * (avg_buy - stop), 2) if stop and avg_buy > stop else None
                cur["R"] = round(cur["net"] / cur["risk"], 3) if cur["risk"] else None
                cur["buy_value"], cur["sell_value"] = round(cur["buy_value"], 2), round(cur["sell_value"], 2)
                trades.append(cur)
                cur = None
        if cur is not None:
            still_open.append({"date": d, "symbol": sym, "qty": round(pos, 6)})
    return trades, still_open


def journal_summary(trades):
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    streak = worst = 0
    for n in nets:
        streak = streak + 1 if n <= 0 else 0
        worst = max(worst, streak)
    rs = [t["R"] for t in trades if t["R"] is not None]
    by_day, by_sym = {}, {}
    for t in trades:
        by_day[t["date"]] = round(by_day.get(t["date"], 0) + t["net"], 2)
        by_sym[t["symbol"]] = round(by_sym.get(t["symbol"], 0) + t["net"], 2)
    return {"trades": len(trades), "wins": len(wins), "win_pct": round(100.0 * len(wins) / len(trades), 1),
            "net": round(sum(nets), 2), "gross": round(sum(t["gross"] for t in trades), 2),
            "charges": round(sum(t["charges"]["total"] for t in trades), 2),
            "avg_R": round(sum(rs) / len(rs), 3) if rs else None, "best": max(nets), "worst": min(nets),
            "longest_losing_streak": worst, "by_day": by_day, "by_symbol": by_sym}


def _signed(x):
    return "%s%s" % ("-" if x < 0 else "+", _money(abs(x)))


def cmd_journal(a):
    cfg = read_json(a.config, "config", required=False) or {}
    state = load_state(a.state)
    paper = True if a.paper else False if a.live else None
    trades, still_open = round_trips(state, a.date_from, a.date_to, paper, cfg.get("charges"))
    s = journal_summary(trades)
    if a.json:
        print(json.dumps({"summary": s, "trades": trades, "open": still_open}, indent=2))
        return 0 if trades else 1
    if not trades:
        print("No closed trades%s in %s." % (" in that range" if a.date_from or a.date_to else "", a.state))
        return 1
    print("%-10s %-14s %5s %12s %12s %10s %10s %7s" % ("date", "symbol", "qty", "bought", "sold", "charges", "net",
                                                         "R"))
    for t in trades:
        print("%-10s %-14s %5s %12s %12s %10s %10s %7s%s" % (
            t["date"], t["symbol"], _str_num(t["qty"]), _money(t["buy_value"]), _money(t["sell_value"]),
            _money(t["charges"]["total"]), _signed(t["net"]), ("%+.2f" % t["R"]) if t["R"] is not None else "-",
            "  paper" if t["paper"] else ""))
    print("")
    print("%d trades, %d won (%.1f %%). Net %s after %s of charges (%s before). Average R %s." % (
        s["trades"], s["wins"], s["win_pct"], _signed(s["net"]), _money(s["charges"]), _signed(s["gross"]),
        ("%+.2f" % s["avg_R"]) if s["avg_R"] is not None else "n/a (no stop recorded)"))
    print("Best %s, worst %s, longest run of losing trades %d." % (_signed(s["best"]), _signed(s["worst"]),
                                                                  s["longest_losing_streak"]))
    for o in still_open:
        print("Still open: %s %s on %s (not counted)." % (_str_num(o["qty"]), o["symbol"], o["date"]))
    if s["trades"] < 30:
        print("Under 30 trades says little either way: keep the journal going before judging the setup.")
    print("Charges here are worked from Zerodha's rates; your contract notes are the final word.")
    return 0


def cmd_tax(a):
    cfg = read_json(a.config, "config", required=False) or {}
    start, end = fy_bounds(a.fy)
    state = load_state(a.state)
    trades, still_open = round_trips(state, start, end, True if a.include_paper == "only" else
                                     None if a.include_paper == "yes" else False, cfg.get("charges"))
    gross = round(sum(t["gross"] for t in trades), 2)
    ch = {k: round(sum(t["charges"][k] for t in trades), 2) for k in ("brokerage", "stt", "exchange", "sebi",
                                                                      "stamp", "gst", "total")}
    turnover = round(sum(abs(t["gross"]) for t in trades), 2)
    doc = {"fy": a.fy.upper().replace(" ", ""), "from": start, "to": end, "trades": len(trades), "gross": gross,
           "charges": ch, "net": round(gross - ch["total"], 2), "turnover": turnover,
           "paper": a.include_paper, "open": still_open}
    if a.csv:
        import csv
        d = os.path.dirname(a.csv)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "symbol", "qty", "buy_value", "sell_value", "gross_pnl", "brokerage", "stt",
                        "exchange", "sebi", "stamp", "gst", "charges", "net_pnl", "paper"])
            for t in trades:
                c = t["charges"]
                w.writerow([t["date"], t["symbol"], _str_num(t["qty"]), "%.2f" % t["buy_value"], "%.2f" % t["sell_value"],
                            "%.2f" % t["gross"], c["brokerage"], c["stt"], c["exchange"], c["sebi"], c["stamp"],
                            c["gst"], c["total"], "%.2f" % t["net"], "yes" if t["paper"] else "no"])
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("%s (%s to %s), intraday equity: speculative business income." % (doc["fy"], start, end))
    if a.include_paper != "no":
        print("PAPER trades are included: they are not real income.")
    print("  %d round trips. Gross P&L %s." % (len(trades), _signed(gross)))
    print("  Charges %s: brokerage %s, STT %s, exchange %s, SEBI %s, stamp duty %s, GST %s." % (
        _money(ch["total"]), _money(ch["brokerage"]), _money(ch["stt"]), _money(ch["exchange"]), _money(ch["sebi"]),
        _money(ch["stamp"]), _money(ch["gst"])))
    print("  Net after charges %s." % _signed(doc["net"]))
    print("  Turnover for the tax-audit limits (each trade's profit or loss, added up without signs): %s."
          % _money(turnover))
    for o in still_open:
        print("  Still open: %s %s on %s (not counted)." % (_str_num(o["qty"]), o["symbol"], o["date"]))
    if a.csv:
        print("Wrote %s, one row per trade." % a.csv)
    print("A worksheet, not tax advice. Speculative losses only set off against speculative income. File with "
          "ITR-3; check it against your broker's tax P&L (Zerodha Console) and ask a chartered accountant.")
    return 0


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-05")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "zerodha")}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    if cfg.get("paper", True):
        print("Paper account: fill it with ticket.py paper --index %d --price <live price>" % a.index)
        print("(or run trader-alerts' alerts.py quote %s --write first and leave out --price)." % rec["symbol"])
        return 0
    calls = tool_input(order)
    print("Now call the Kite connector's place_order with exactly:")
    print(json.dumps(calls[0]["input"], indent=2))
    for c in calls[1:]:
        print("When the entry fills, the %s: place_order %s" % (c["step"], json.dumps(c["input"])))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
          "placed|denied|failed --broker-id <Kite order id>" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and is_bracket(order):
            print("Once it fills, place the stop-loss and the target (ticket.py show lists both calls). When one")
            print("fills, cancel the other with cancel_order, and record that sell the same way.")
    return 0


def _fresh_price(path, sym, now):
    """A price for sym from trader/prices.json if it is under FRESH_MINUTES old, else None."""
    doc = read_json(path, "prices", required=False) or {}
    p = _f((doc.get("prices") or {}).get(sym))
    if not p:
        return None
    det = (doc.get("detail") or {}).get(sym) or {}
    when = None
    for key in (det.get("time"), det.get("fetched"), doc.get("as_of")):
        if key:
            try:
                dt = datetime.datetime.fromisoformat(str(key).replace("Z", "+00:00"))
                when = (dt - dt.utcoffset()).replace(tzinfo=None) if dt.tzinfo else dt
                break
            except ValueError:
                continue
    if when is None or not -60 <= (now - when).total_seconds() <= FRESH_MINUTES * 60:
        return None
    return p


def _parse_prices(items):
    """["INFY.NS=1164.80", ...] -> {SYM: price}."""
    out = {}
    for it in items or []:
        if "=" not in it:
            raise TicketError("--price wants SYMBOL=PRICE, like INFY.NS=1164.80")
        k, v = it.split("=", 1)
        if _f(v) is None or _f(v) <= 0:
            raise TicketError("--price %s is not a price" % it)
        out[k.strip().upper()] = _f(v)
    return out


def cmd_paper(a):
    orders, cfg, risk, state, today, now = _load(a)
    if not cfg.get("paper", True):
        raise TicketError("this is a LIVE account (\"paper\": false): orders go through the Kite connector, "
                          "not ticket.py paper")
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today: ticket.py approve comes first" % a.index)
    sym = str(order.get("symbol", "")).upper()
    price = _f(a.price) if a.price else _fresh_price(a.prices, sym, now)
    if not price or price <= 0:
        raise TicketError("no live price for %s: pass --price, or run alerts.py quote %s --write (a price under "
                          "%d minutes old)" % (sym, sym, FRESH_MINUTES))
    side = str(order.get("side", "buy")).lower()
    typ = str(order.get("type", "market")).lower()
    if typ == "limit":
        lim = _f(order.get("limit_price"))
        if (side == "buy" and price > lim + 0.005) or (side == "sell" and price < lim - 0.005):
            print("Not filled: %s is at %s, %s your limit %s. Run ticket.py paper again when it gets there."
                  % (sym, _p(price), "above" if side == "buy" else "below", _p(lim)))
            return 1
        price = lim
    elif typ in ("stop", "stop_limit"):
        raise TicketError("paper trading fills market and limit orders; a stop order waits for its trigger")
    qty = float(order.get("qty"))
    if side == "sell":
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        held = sum(_f(p.get("shares")) or 0 for p in pf.get("positions", [])
                   if str(p.get("ticker", "")).upper() == sym)
        if held + 1e-9 < qty:
            raise TicketError("the paper account holds %s %s, not %s" % (_str_num(held), sym, _str_num(qty)))
    paper_id = "PAPER-%s-%d" % (fp[:8], len(state.get("orders", [])) + 1)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": "placed", "paper": True,
           "broker_order_id": paper_id}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    state["orders"].append(new)
    if side == "sell":
        for leg in state.get("paper_legs", []):
            if leg.get("status") == "open" and leg.get("symbol") == sym:
                leg["status"] = "canceled"
                leg["closed_at"] = ist_iso(now)
    if is_bracket(order) and side == "buy":
        state.setdefault("paper_legs", []).append({
            "symbol": sym, "qty": _str_num(qty), "entry": price, "stop": _f(order.get("stop_loss_stop_price")),
            "target": _f(order.get("take_profit_limit_price")), "date": today, "opened_at": ist_iso(now),
            "fingerprint": fp, "status": "open",
            "flat_by": (order.get("plan") or {}).get("flat_by") or cfg.get("flat_by") or "15:10"})
    write_json(a.state, state)
    apply_fill(a.portfolio, cfg, sym, side, qty, price, today)
    filled = log_fill(a.state, now, today, sym, side, qty, price, cfg, paper=True, note=paper_id)
    print("PAPER %s %s %s at %s (%s charges). Order id %s." % (side.upper(), _str_num(qty), sym, _p(price),
                                                              _money(filled["charges"]), paper_id))
    if is_bracket(order) and side == "buy":
        print("Open paper orders: stop-loss %s and target %s. ticket.py paper-check fills whichever the price "
              "reaches first." % (_p(order["stop_loss_stop_price"]), _p(order["take_profit_limit_price"])))
    return 0


def cmd_paper_check(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    state = load_state(a.state)
    legs = [l for l in state.get("paper_legs", []) if l.get("status") == "open"]
    given = _parse_prices(a.price)
    if not legs:
        print("No open paper orders.")
    for leg in legs:
        sym = leg["symbol"]
        p = given.get(sym) or _fresh_price(a.prices, sym, now)
        if not p:
            print("%s: no live price (pass --price %s=P or run alerts.py check); stop %s, target %s still open"
                  % (sym, sym, _p(leg["stop"]), _p(leg["target"])))
            continue
        hit = None
        if leg.get("stop") and p <= leg["stop"] + 1e-9:
            hit, fill = "stop-loss", p
        elif leg.get("target") and p >= leg["target"] - 1e-9:
            hit, fill = "target", leg["target"]
        if not hit:
            line = "%s %s: between the stop %s and the target %s, still open" % (sym, _p(p), _p(leg["stop"]),
                                                                                 _p(leg["target"]))
            flat = leg.get("flat_by")
            if flat and leg.get("date") == today and to_ist(now).strftime("%H:%M") >= flat:
                line += ". It is past flat_by %s: ticket.py flatten --symbol %s" % (flat, sym)
            print(line)
            continue
        leg["status"] = "filled"
        leg["closed_at"] = ist_iso(now)
        leg["exit"] = fill
        leg["exit_reason"] = hit
        write_json(a.state, state)
        apply_fill(a.portfolio, cfg, sym, "sell", float(leg["qty"]), fill, today)
        rec = log_fill(a.state, now, today, sym, "sell", float(leg["qty"]), fill, cfg, paper=True,
                       note="paper %s" % hit)
        state = load_state(a.state)
        print("PAPER %s filled: SELL %s %s at %s (%s charges)%s; the %s is canceled."
              % (hit, leg["qty"], sym, _p(fill), _money(rec["charges"]),
                 " (under the stop %s: an SL-M fills at the market)" % _p(leg["stop"])
                 if hit == "stop-loss" and fill < leg["stop"] - 0.005 else "",
                 "target" if hit == "stop-loss" else "stop-loss"))
    for sym, bq, sq, pnl, ch in day_pnl(load_state(a.state), today):
        if pnl is None:
            print("%s: %s bought, %s sold, still open (charges so far %s)" % (sym, _str_num(bq), _str_num(sq),
                                                                             _money(ch)))
        else:
            print("%s: closed, P&L after %s charges %s%s" % (sym, _money(ch), "-" if pnl < 0 else "+",
                                                             _money(abs(pnl))))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-05")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    rec = log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, cfg)
    print("Recorded %s %s %s at %s on %s in %s (about %s charges). Cash now %s."
          % (a.side, a.qty, a.symbol.upper(), _money(a.price), date, a.portfolio, _money(rec["charges"]),
             _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    source = "the broker, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 41")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) "
                              "and pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:10"
    order = {"symbol": sym, "exchange": "NSE", "product": str(cfg.get("product") or "MIS").upper(), "side": "sell",
             "qty": _str_num(qty), "type": "market", "time_in_force": "day",
             "why": "flat by %s IST: closes the %s day trade before the close (qty from %s)"
             % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the broker's positions before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if cfg.get("paper", True):
        print("  3. ticket.py paper --index 1: it sells at the live price and cancels the open paper stop-loss")
        print("     and target.")
    else:
        print("  3. Cancel the open stop-loss and target first, or they could sell the same shares twice:")
        print("     get_orders, then cancel_order for each open one (Needs approval). Then place_order with")
        print("     the ticket. Zerodha squares off what is still open at about 3:20 PM for 50 rupees + GST.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    if not a.all:
        for sym, bq, sq, pnl, ch in day_pnl(state, today):
            if pnl is None:
                print("%s: still open (%s bought, %s sold)" % (sym, _str_num(bq), _str_num(sq)))
            else:
                print("%s: P&L after %s charges %s%s" % (sym, _money(ch), "-" if pnl < 0 else "+", _money(abs(pnl))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-05 09:40\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("paper", help="paper account: fill an approved order at the live price")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--price", help="the live price (default: a fresh price in trader/prices.json)")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("paper-check", help="fill open paper stop-losses and targets the price has reached")
    p.add_argument("--price", action="append", metavar="SYM=PRICE", help="a live price (repeatable)")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("result", help="live account: record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_open_position (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("journal", help="closed trades: win rate, average R, P&L after charges")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="paper trades only")
    g.add_argument("--live", action="store_true", help="live trades only")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("tax", help="a financial year's intraday P&L, charges and turnover (a worksheet)")
    p.add_argument("--fy", required=True, help="FY27 = April 2026 to March 2027")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--include-paper", choices=["no", "yes", "only"], default="no")
    p.add_argument("--csv", metavar="PATH", help="also write one row per trade")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "paper": cmd_paper, "paper-check": cmd_paper_check,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log, "journal": cmd_journal,
                "tax": cmd_tax}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
