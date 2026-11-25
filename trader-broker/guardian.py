#!/usr/bin/env python3
"""The live guardian for trader-broker: keeps every live intraday position protected at Zerodha, by itself.
Standard library only.

    python3 guardian.py login --request-token TOKEN     # once a day: KITE_API_KEY and KITE_API_SECRET in the env
    python3 guardian.py run                             # DRY RUN: reads Kite every 5 seconds, prints what it would do
    python3 guardian.py run --arm                       # live: does it, 9:15 to 15:30 IST, then exits
    python3 guardian.py once --arm                      # one pass
    python3 guardian.py plan --positions pos.json --broker-orders ord.json --now "2026-11-05 10:05"   # offline

Why. Kite has no bracket order for stocks, so a live entry's stop-loss and target are separate orders placed AFTER
the entry fills, each behind its own Allow once card. Until then the position is unprotected, and when the target
fills nothing cancels the stop, which can later open the opposite position. The guardian closes that gap without
anyone at the keyboard.

What it may do. It can only REDUCE or PROTECT what is already open. Every action is decided from one snapshot of
Kite's positions and orders, then checked again against a FRESH snapshot just before it is sent, and dropped if it
no longer holds:
  protect     an open MIS position whose stop-losses cover fewer shares than it holds gets an SL-M on the closing
              side for the shortfall, at the stop you approved (trader/state/broker.json). A position the pack did
              not plan gets one at your max loss per trade from its average price (guardian_unplanned "max_loss",
              the default; "alert" only alerts). Never more stop shares than the position holds.
  restore     a stop moved further from the price than the approved stop is moved back to it (only ever tighter).
  through     the price is already past the approved stop and the stops do not cover the position: the closing
              orders are cancelled and the position is closed at market. A position the pack did not plan is
              never closed this way, only alerted.
  trim        stops or targets for more shares than are held (the extra would open the opposite position): the
              pack's own (tag dts...) are cancelled or cut down to the position.
  orphan      a stop or target of the pack's (tag dts...) still open on a symbol that is now flat: cancelled. An
              entry order still waiting is left alone.
  flat-by     at your flat_by (15:10): every pending MIS order is cancelled and every MIS position closed at market.
It never opens or adds to a position, never places an order on a symbol it does not hold, never touches delivery
(CNC) positions or another exchange, and never widens a stop. After 2 rejected orders on a symbol it stops trying
there and alerts. A timed-out order is never re-sent blind: the next pass finds it by its tag (dtsg...).

Arming. Without --arm nothing is sent: each pass prints the actions it would take. --arm also needs "paper": false
in trader/config.json. The stop switch (trader/STOP) does not stop the guardian: it only ever gets you out.
Kite credentials: KITE_API_KEY and KITE_ACCESS_TOKEN in the environment, or trader/state/kite_session.json written by
`login` (readable by you only). Kite's access token expires every morning; the API secret is never stored. Typed
into a terminal or a file, never into the chat.

Alerts go to NOTIFY_URL (as the scheduler's), the actions to trader/state/guardian-log.jsonl, the heartbeat to
trader/state/guardian.json. Check SEBI's and Zerodha's rules for orders sent through the API (a registered static IP,
market protection on market orders) at developers.kite.trade before arming it. Not financial advice.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "trader-risk"), os.path.join(ROOT, "core")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)
sys.path.insert(0, HERE)
import ticket  # noqa: E402
import check  # noqa: E402

TAG = "dtsg"                      # the guardian's own orders; the pack's are all "dts..."
POLL = 5                          # seconds between passes
ROUNDS = 3                        # decide and act up to this many times a pass (a cancel, then the close)
MAX_REJECTS = 2                   # rejected guardian orders on one symbol today before it only alerts
FAIL_ALERT = 3                    # consecutive failed reads of Kite before an alert
BASE = "https://api.kite.trade"
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
STATE_DIR = os.path.join("trader", "state")
SESSION = os.path.join(STATE_DIR, "kite_session.json")
HEARTBEAT = os.path.join(STATE_DIR, "guardian.json")
LOG = os.path.join(STATE_DIR, "guardian-log.jsonl")
CENT = 0.005


class GuardianError(Exception):
    pass


class KiteError(Exception):
    def __init__(self, message, kind=None, status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


# ---------------------------------------------------------------- the book: Kite's view, in one shape

def book(positions_doc, orders_doc):
    """Kite's positions and orders -> (positions, orders, others).
    positions {SYM.NS: {"qty", "avg", "ltp"}} for NSE MIS (intraday); others: [SYM.NS (product)] it leaves alone;
    orders [{"id", "symbol", "side", "type", "product", "qty", "pending", "status", "trigger", "price", "tag"}]."""
    pos, others = {}, []
    for p in ticket._rows(positions_doc, "net", "data"):
        q = ticket._f(p.get("quantity"))
        if not q:
            continue
        sym = str(p.get("tradingsymbol", "")).upper() + ".NS"
        prod = str(p.get("product") or "MIS").upper()
        if str(p.get("exchange", "NSE")).upper() != "NSE" or prod != "MIS":
            others.append("%s (%s %s)" % (sym, p.get("exchange", "NSE"), prod))
            continue
        x = pos.setdefault(sym, {"qty": 0.0, "avg": None, "ltp": None})
        x["qty"] += q
        x["avg"] = ticket._f(p.get("average_price")) or x["avg"]
        x["ltp"] = ticket._f(p.get("last_price")) or x["ltp"]
    orders = []
    for o in ticket._rows(orders_doc, "data", "orders"):
        if str(o.get("exchange", "NSE")).upper() != "NSE":
            continue
        q = ticket._f(o.get("quantity")) or 0
        orders.append({"id": str(o.get("order_id") or o.get("id") or "?"),
                       "symbol": str(o.get("tradingsymbol", "")).upper() + ".NS",
                       "side": str(o.get("transaction_type", "")).lower(),
                       "type": str(o.get("order_type", "")).upper(),
                       "product": str(o.get("product") or "MIS").upper(), "qty": q,
                       "pending": ticket._f(o.get("pending_quantity")) if o.get("pending_quantity") is not None else q,
                       "status": str(o.get("status", "")).upper(), "trigger": ticket._f(o.get("trigger_price")),
                       "price": ticket._f(o.get("price")), "tag": str(o.get("tag") or "")})
    return pos, orders, others


def pending(orders, sym=None):
    return [o for o in orders if o["status"] in ticket.OPEN_STATUSES and o["product"] == "MIS"
            and (sym is None or o["symbol"] == sym)]


def closing_market(o):
    """The guardian's own market close: never cancelled by the guardian, and counted as covering its shares."""
    return o["tag"].startswith(TAG) and o["type"] == "MARKET"


def plans_today(state, today):
    """{SYM: {"side", "stop", "qty", "tag"}}: today's approved live entries, the stop you said yes to."""
    out = {}
    for r in (state or {}).get("orders", []):
        if (r.get("status") == "approved" and ticket.rec_day(r) == today and r.get("kind", "entry") == "entry"
                and not r.get("paper")):
            out[str(r.get("symbol", "")).upper()] = {
                "side": str(r.get("side", "")).lower(), "stop": ticket._f(r.get("stop")), "qty": ticket._f(r.get("qty")),
                "tag": "dts" + str(r.get("fingerprint", ""))[:8], "fingerprint": str(r.get("fingerprint", ""))}
    return out


def guard_tag(plan):
    return (TAG + plan["fingerprint"][:8]) if plan and plan.get("fingerprint") else TAG + "manual"


def tick_away(x, long, tick=None):
    """A stop on the NSE tick, away from the price: down for a long's stop, up for a short's."""
    tick = tick or tick_for(x)
    n = math.floor(x / tick + 1e-6) if long else math.ceil(x / tick - 1e-6)
    return round(n * tick, 2)


def tick_for(price):
    t, top = ticket.tradercore.ticks()
    for below, size in t:
        if price < below:
            return size
    return top


# ---------------------------------------------------------------- decide: one snapshot -> actions

def decide(pos, orders, plans, now_ist, cfg):
    """Pure. Returns (actions, alerts). Each action: {"do": place|cancel|modify, "symbol", "why", "phase", ...}."""
    acts, alerts = [], []
    flat_by = check.hhmm(cfg.get("flat_by") or "15:10", "flat_by")
    t = now_ist.time().replace(second=0, microsecond=0)
    unplanned = str(cfg.get("guardian_unplanned") or "max_loss").lower()
    max_loss = ticket._f(cfg.get("max_loss_per_trade_inr")) or 500.0

    if flat_by and t >= flat_by:
        live = [o for o in pending(orders) if not closing_market(o)]
        for o in live:
            acts.append({"do": "cancel", "symbol": o["symbol"], "order_id": o["id"], "phase": "flat-by",
                         "why": "flat-by %s: cancel %s %s %s" % (flat_by.strftime("%H:%M"), o["type"], o["side"].upper(),
                                                                  ticket._str_num(o["pending"]))})
        for sym, p in sorted(pos.items()):
            if any(o["symbol"] == sym for o in live):
                continue                  # its orders are being cancelled: close it on the next round
            left = abs(p["qty"]) - sum(o["pending"] for o in pending(orders, sym) if closing_market(o))
            if left > 1e-9:
                acts.append(close_at_market(sym, left if p["qty"] > 0 else -left, plans.get(sym), "flat-by",
                                            "flat-by %s: close %s" % (flat_by.strftime("%H:%M"), describe(sym, p["qty"]))))
        return acts, alerts

    rejected = {}
    for o in orders:
        if o["tag"].startswith(TAG) and o["status"] == "REJECTED":
            rejected[o["symbol"]] = rejected.get(o["symbol"], 0) + 1

    for sym in sorted(set(pos) | {o["symbol"] for o in pending(orders)}):
        q = pos.get(sym, {}).get("qty", 0.0)
        plan = plans.get(sym)
        live = pending(orders, sym)
        if abs(q) < 1e-9:
            for o in live:
                entry = plan and o["side"] == plan["side"]
                if o["tag"].startswith("dts") and not entry:
                    acts.append({"do": "cancel", "symbol": sym, "order_id": o["id"], "phase": "orphan",
                                 "plan_side": plan["side"] if plan else None,
                                 "why": "orphan: %s %s %s still open on a flat %s; it would open a new position"
                                        % (o["type"], o["side"].upper(), ticket._str_num(o["pending"]), sym)})
                elif o["type"] in ticket.STOP_TYPES and not entry:
                    alerts.append("%s is flat but stop order %s (not the pack's) is still open: check it" % (sym, o["id"]))
            continue
        p = pos[sym]
        long = q > 0
        closing = "sell" if long else "buy"
        stops = [o for o in live if o["type"] in ticket.STOP_TYPES and o["side"] == closing]
        others = [o for o in live if o["type"] not in ticket.STOP_TYPES and o["side"] == closing
                  and not closing_market(o)]
        closing_now = sum(o["pending"] for o in live if closing_market(o) and o["side"] == closing)
        cover = sum(o["pending"] for o in stops)
        ltp = p.get("ltp")
        if plan and plan.get("stop") and plan["side"] == ("buy" if long else "sell"):
            want, planned = plan["stop"], True
        else:
            planned = False
            want = None
            if plan:
                alerts.append("%s: the position is %s but today's approved entry was a %s: managed as unplanned"
                              % (sym, "long" if long else "short", plan["side"]))
                plan = None
            if unplanned == "max_loss" and p.get("avg"):
                want = tick_away(p["avg"] - max_loss / abs(q) if long else p["avg"] + max_loss / abs(q), long)
                if want <= 0:
                    want = None
            if not want:
                alerts.append("%s: %s with no approved stop%s" % (sym, describe(sym, q),
                                                                   "" if cover >= abs(q) else ", and it is unprotected"))
        through = bool(want and ltp and ((long and ltp <= want + CENT) or (not long and ltp >= want - CENT)))
        inside = all(o["trigger"] and want and ((long and o["trigger"] >= want - CENT) or
                                                (not long and o["trigger"] <= want + CENT)) for o in stops)
        if through and not (cover >= abs(q) - 1e-9 and inside):
            if not planned:
                alerts.append("%s: %s is at %s, past its max-loss level %s, and not fully covered: close it yourself "
                              "(the guardian never closes a position the pack did not plan, before the flat-by)"
                              % (sym, describe(sym, q), ticket._p(ltp), ticket._p(want)))
                continue
            for o in stops + others:
                acts.append({"do": "cancel", "symbol": sym, "order_id": o["id"], "phase": "close",
                             "why": "through the stop: %s is at %s, the stop is %s; cancel %s %s first"
                                    % (sym, ticket._p(ltp), ticket._p(want), o["type"], o["id"])})
            left = abs(q) - closing_now
            if not stops and not others and left > 1e-9:
                acts.append(close_at_market(sym, left if long else -left, plan, "close",
                                            "through the stop: %s is at %s, past the approved stop %s: close %s at "
                                            "market" % (sym, ticket._p(ltp), ticket._p(want), describe(sym, q))))
            continue
        if through:
            continue                      # the stops cover it at or inside the plan: the exchange triggers them
        # restore a stop moved away from the approved one (only ever tighter)
        if planned:
            for o in stops:
                if o["trigger"] and ((long and o["trigger"] < want - CENT) or (not long and o["trigger"] > want + CENT)):
                    acts.append({"do": "modify", "symbol": sym, "order_id": o["id"], "trigger": want, "phase": "restore",
                                 "why": "stop moved away: %s triggers at %s, the approved stop is %s: move it back"
                                        % (o["id"], ticket._p(o["trigger"]), ticket._p(want))})
        # trim the pack's stops and targets to the position
        for group, word in ((stops, "stops"), (others, "targets")):
            total = sum(o["pending"] for o in group)
            extra = total - abs(q)
            if extra <= 1e-9:
                continue
            mine = sorted((o for o in group if o["tag"].startswith("dts")), key=lambda o: o["id"], reverse=True)
            for o in mine:
                if extra <= 1e-9:
                    break
                if o["pending"] <= extra + 1e-9:
                    acts.append({"do": "cancel", "symbol": sym, "order_id": o["id"], "phase": "trim",
                                 "why": "%s for %s shares on a %s-share position: cancel %s"
                                        % (word, ticket._str_num(total), ticket._str_num(abs(q)), o["id"])})
                    extra -= o["pending"]
                else:
                    acts.append({"do": "modify", "symbol": sym, "order_id": o["id"], "qty": o["pending"] - extra,
                                 "phase": "trim", "why": "%s for %s shares on a %s-share position: cut %s to %s"
                                 % (word, ticket._str_num(total), ticket._str_num(abs(q)), o["id"],
                                    ticket._str_num(o["pending"] - extra))})
                    extra = 0
            if extra > 1e-9:
                alerts.append("%s: %s for %s shares more than the %s held, not the pack's orders: cancel the extra "
                              "yourself" % (sym, word, ticket._str_num(extra), ticket._str_num(abs(q))))
        # protect the shortfall (a market close already on its way covers its shares)
        short = abs(q) - cover - closing_now
        if short > 1e-9 and want:
            if rejected.get(sym, 0) >= MAX_REJECTS:
                alerts.append("%s: %d stop orders rejected today; the guardian has stopped trying. %s is "
                              "UNPROTECTED for %s shares: act now" % (sym, rejected[sym], describe(sym, q),
                                                                     ticket._str_num(short)))
                continue
            acts.append({"do": "place", "symbol": sym, "side": closing, "order_type": "SL-M", "qty": short,
                         "trigger": want, "tag": guard_tag(plan), "phase": "protect",
                         "why": "unprotected: %s, stops cover %s; SL-M %s %s at %s (%s)"
                                % (describe(sym, q), ticket._str_num(cover), closing.upper(), ticket._str_num(short),
                                   ticket._p(want), "the approved stop" if planned else "your max loss per trade")})
    return acts, alerts


def close_at_market(sym, q, plan, phase, why):
    return {"do": "place", "symbol": sym, "side": "sell" if q > 0 else "buy", "order_type": "MARKET", "qty": abs(q),
            "trigger": None, "tag": guard_tag(plan), "phase": phase, "why": why}


def describe(sym, q):
    return "%s %s %s" % ("long" if q > 0 else "short", ticket._str_num(abs(q)), sym)


# ---------------------------------------------------------------- validate: the action against a FRESH snapshot

def validate(a, pos, orders):
    """None when the action still only reduces or protects in this fresh snapshot, else why not. The last line of
    defence: whatever decide() got wrong, nothing here can open, add to or unprotect a position."""
    sym = a["symbol"]
    if not str(sym).endswith(".NS"):
        return "not an NSE symbol"
    q = pos.get(sym, {}).get("qty", 0.0)
    ltp = pos.get(sym, {}).get("ltp")
    live = pending(orders, sym)
    closing = "sell" if q > 0 else "buy"
    stop_cover = sum(o["pending"] for o in live if o["type"] in ticket.STOP_TYPES and o["side"] == closing)
    market_cover = sum(o["pending"] for o in live if closing_market(o) and o["side"] == closing)
    all_cover = sum(o["pending"] for o in live if o["side"] == closing)
    if a["do"] == "place":
        if abs(q) < 1e-9:
            return "%s is flat now" % sym
        if a["side"] != closing:
            return "a %s would add to the %s position" % (a["side"], "long" if q > 0 else "short")
        qty = a["qty"]
        if qty < 1 or abs(qty - round(qty)) > 1e-9:
            return "qty %r is not a whole number of shares" % qty
        if a["order_type"] == "SL-M":
            if qty > abs(q) - stop_cover - market_cover + 1e-9:
                return "stops and closes would cover %s of a %s-share position" % (
                    ticket._str_num(stop_cover + market_cover + qty), ticket._str_num(abs(q)))
            trig = a.get("trigger")
            if not trig or trig <= 0:
                return "no trigger price"
            if ltp and ((q > 0 and trig >= ltp - CENT) or (q < 0 and trig <= ltp + CENT)):
                return "the trigger %s is on the wrong side of the last price %s" % (ticket._p(trig), ticket._p(ltp))
            return None
        if a["order_type"] == "MARKET":
            if qty > abs(q) - all_cover + 1e-9:
                return "closing orders for %s shares are still open on a %s-share position" % (
                    ticket._str_num(all_cover), ticket._str_num(abs(q)))
            return None
        return "order type %s is not one the guardian places" % a["order_type"]
    o = next((x for x in orders if x["id"] == a["order_id"]), None)
    if not o or o not in live:
        return "order %s is no longer pending" % a["order_id"]
    if a["do"] == "cancel":
        if closing_market(o):
            return "the guardian's own market close is never cancelled"
        if a["phase"] in ("flat-by", "close"):
            return None
        if abs(q) < 1e-9:
            if a["phase"] != "orphan" or not o["tag"].startswith("dts") or o["side"] == a.get("plan_side"):
                return "not an orphan of the pack's"
            return None
        if o["side"] != closing:
            return "cancelling an order on the opening side is not the guardian's to do before the flat-by"
        if o["type"] in ticket.STOP_TYPES and stop_cover - o["pending"] < abs(q) - 1e-9:
            return "cancelling it would leave the position under-protected"
        if o["type"] not in ticket.STOP_TYPES and not o["tag"].startswith("dts"):
            return "not one of the pack's orders"
        return None
    if a["do"] == "modify":
        if abs(q) < 1e-9 or o["side"] != closing:
            return "not a closing order of an open position"
        if a.get("qty") is not None:
            new = a["qty"]
            cover = stop_cover if o["type"] in ticket.STOP_TYPES else all_cover - stop_cover
            if new < 1 or new > o["pending"] - 1e-9:
                return "a modify may only cut the quantity"
            if o["type"] in ticket.STOP_TYPES and cover - o["pending"] + new < abs(q) - 1e-9:
                return "cutting it would leave the position under-protected"
        if a.get("trigger") is not None:
            new, old = a["trigger"], o["trigger"]
            if o["type"] not in ticket.STOP_TYPES or old is None:
                return "not a stop"
            if (q > 0 and new <= old + CENT) or (q < 0 and new >= old - CENT):
                return "a modify may only move a stop closer"
            if ltp and ((q > 0 and new >= ltp - CENT) or (q < 0 and new <= ltp + CENT)):
                return "the trigger %s is on the wrong side of the last price %s" % (ticket._p(new), ticket._p(ltp))
        return None
    return "unknown action %r" % a["do"]


# ---------------------------------------------------------------- Kite Connect (v3), standard library only

class Kite:
    def __init__(self, api_key, access_token, base=BASE, timeout=7):
        self.api_key, self.token, self.base, self.timeout = api_key, access_token, base.rstrip("/"), timeout

    def _req(self, method, path, data=None, auth=True):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"X-Kite-Version": "3", "User-Agent": "day-trader-guardian"}
        if auth:
            headers["Authorization"] = "token %s:%s" % (self.api_key, self.token)
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                doc = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read().decode("utf-8"))
            except ValueError:
                doc = {}
            raise KiteError(doc.get("message") or str(e), doc.get("error_type"), e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise KiteError("no answer from Kite: %s" % e, "NetworkException")
        if doc.get("status") != "success":
            raise KiteError(doc.get("message") or "Kite said no", doc.get("error_type"))
        return doc.get("data")

    def positions(self):
        return {"data": self._req("GET", "/portfolio/positions")}

    def orders(self):
        return {"data": self._req("GET", "/orders")}

    def place(self, params):
        return (self._req("POST", "/orders/regular", params) or {}).get("order_id")

    def modify(self, order_id, params):
        return self._req("PUT", "/orders/regular/%s" % urllib.parse.quote(order_id), params)

    def cancel(self, order_id):
        return self._req("DELETE", "/orders/regular/%s" % urllib.parse.quote(order_id))

    def session(self, request_token, api_secret):
        checksum = hashlib.sha256((self.api_key + request_token + api_secret).encode()).hexdigest()
        return self._req("POST", "/session/token", {"api_key": self.api_key, "request_token": request_token,
                                                     "checksum": checksum}, auth=False)


def place_params(a, cfg):
    p = {"tradingsymbol": ticket.tradingsymbol(a["symbol"]), "exchange": "NSE",
         "transaction_type": a["side"].upper(), "order_type": a["order_type"], "quantity": int(round(a["qty"])),
         "product": "MIS", "validity": "DAY", "tag": a["tag"][:20]}
    if a["order_type"] == "SL-M":
        p["trigger_price"] = "%.2f" % a["trigger"]
    elif cfg.get("guardian_market_protection") is not None:
        p["market_protection"] = cfg["guardian_market_protection"]
    return p


def send(kite, a, o, cfg):
    """One validated action to Kite. Returns a short result string."""
    if a["do"] == "place":
        return "order %s" % kite.place(place_params(a, cfg))
    if a["do"] == "cancel":
        kite.cancel(a["order_id"])
        return "cancelled"
    params = {"order_type": o["type"], "validity": "DAY"}
    params["quantity"] = int(round(a["qty"] if a.get("qty") is not None else o["pending"]))
    if o["type"] in ticket.STOP_TYPES:
        params["trigger_price"] = "%.2f" % (a["trigger"] if a.get("trigger") is not None else o["trigger"])
    if o["type"] in ("LIMIT", "SL") and o.get("price"):
        params["price"] = "%.2f" % o["price"]
    kite.modify(a["order_id"], params)
    return "modified"


# ---------------------------------------------------------------- one pass

def session_open(now_ist):
    d = now_ist.date()
    if d.weekday() >= 5 or check.NSE_HOLIDAYS.get(d.isoformat()):
        return False
    return OPEN <= now_ist.time() < CLOSE


def one_pass(kite, state, cfg, now_utc, armed, say=print, log=None):
    """Read Kite, decide, act (if armed), up to ROUNDS times. Returns {"actions": [...], "alerts": [...]}."""
    now_ist = check.to_ist(now_utc)
    today = now_ist.date().isoformat()
    plans = plans_today(state, today)
    done, alerts, seen = [], [], set()
    for _ in range(ROUNDS):
        pos, orders, others = book(kite.positions(), kite.orders())
        if others and not done:
            say("not managed (delivery or another exchange): %s" % ", ".join(others))
        acts, al = decide(pos, orders, plans, now_ist, cfg)
        alerts.extend(x for x in al if x not in alerts)
        acts = [a for a in acts if key(a) not in seen]
        if not acts:
            break
        for a in acts:
            seen.add(key(a))
            fpos, forders, _ = book(kite.positions(), kite.orders()) if armed else (pos, orders, others)
            why_not = validate(a, fpos, forders)
            rec = {"at": check.to_ist(now_utc).strftime("%Y-%m-%dT%H:%M:%S+05:30"), "action": a, "armed": armed}
            if why_not:
                rec["result"] = "skipped: " + why_not
            elif not armed:
                rec["result"] = "dry run"
            else:
                o = next((x for x in forders if x["id"] == a.get("order_id")), None)
                try:
                    rec["result"] = send(kite, a, o, cfg)
                except KiteError as e:
                    rec["result"] = "failed: %s" % e
                    alerts.append("%s: %s failed: %s" % (a["symbol"], a["why"], e))
            say("%s  %s  [%s]" % (a["phase"].upper(), a["why"], rec["result"]))
            if log:
                log(rec)
            done.append(rec)
        if not armed:
            break
    return {"actions": done, "alerts": alerts}


def key(a):
    return (a["do"], a["symbol"], a.get("order_id"), a.get("side"), a.get("order_type"), a.get("phase"))


# ---------------------------------------------------------------- files, credentials, the loop

def append_log(path):
    def write(rec):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return write


def credentials(path=SESSION):
    key_, token = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
    if key_ and token:
        return key_, token
    doc = ticket.read_json(path, "Kite session", required=False) or {}
    if doc.get("api_key") and doc.get("access_token"):
        return doc["api_key"], doc["access_token"]
    raise GuardianError("no Kite session: set KITE_API_KEY and KITE_ACCESS_TOKEN, or run guardian.py login "
                        "--request-token <token from Kite's login redirect>")


def notify(text):
    try:
        import scheduler
        return scheduler.notify("guardian: " + text)
    except Exception:  # noqa: BLE001  (an alert that cannot be sent must not stop the guardian)
        return False


def heartbeat(path, now_utc, armed, res=None, error=None):
    doc = ticket.read_json(path, "guardian heartbeat", required=False) or {}
    today = check.to_ist(now_utc).date().isoformat()
    if doc.get("date") != today:
        doc = {"date": today, "actions": 0}
    doc.update({"alive": check.to_ist(now_utc).strftime("%Y-%m-%d %H:%M:%S"), "armed": armed, "error": error})
    if res:
        doc["actions"] = doc.get("actions", 0) + sum(1 for r in res["actions"] if r["result"] not in ("dry run",)
                                                     and not r["result"].startswith("skipped"))
        doc["alerts"] = res["alerts"][-10:]
    ticket.write_json(path, doc)


def acquire(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        fh.close()
        raise GuardianError("another guardian is running (%s is held)" % path)
    return fh


def load_cfg(path):
    cfg = dict(check.DEFAULTS)
    cfg.update(check.inr_keys(ticket.read_json(path, "config", required=False) or {}))
    return cfg


def cmd_login(a):
    key_, secret = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_API_SECRET")
    if not key_ or not secret:
        raise GuardianError("set KITE_API_KEY and KITE_API_SECRET in the environment (never in the chat)")
    data = Kite(key_, None).session(a.request_token, secret)
    os.makedirs(os.path.dirname(a.session), exist_ok=True)
    fd = os.open(a.session, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"api_key": key_, "access_token": data["access_token"],
                   "date": check.to_ist(check.parse_now(None)).date().isoformat()}, fh)
    print("Kite session saved to %s (you only). It expires tomorrow morning." % a.session)
    return 0


def cmd_plan(a):
    pos, orders, others = book(ticket.read_json(a.positions, "Kite positions"),
                               ticket.read_json(a.broker_orders, "Kite orders"))
    now_ist = check.to_ist(check.parse_now(a.now))
    acts, alerts = decide(pos, orders, plans_today(ticket.load_state(a.state), now_ist.date().isoformat()),
                          now_ist, load_cfg(a.config))
    if a.json:
        print(json.dumps({"actions": acts, "alerts": alerts, "not_managed": others}, indent=2))
        return 0
    for x in acts:
        bad = validate(x, pos, orders)
        print("%s  %s%s" % (x["phase"].upper(), x["why"], ("  [would be skipped: %s]" % bad) if bad else ""))
    for x in alerts:
        print("ALERT  " + x)
    if not acts and not alerts:
        print("OK: every MIS position is covered, nothing left over")
    return 0


def run(a, loop):
    cfg = load_cfg(a.config)
    armed = bool(a.arm)
    if armed and cfg.get("paper", True):
        raise GuardianError("--arm needs a live account (\"paper\": false in %s): on paper there is nothing at the "
                            "broker to guard" % a.config)
    kite = Kite(*credentials(a.session))
    lock = acquire(os.path.join(STATE_DIR, "guardian.lock"))
    print("guardian %s, every %ss, flat-by %s" % ("ARMED" if armed else "DRY RUN (add --arm to act)", a.every,
                                                    cfg.get("flat_by")), flush=True)
    fails, told = 0, set()
    try:
        while True:
            now = check.parse_now(a.now) if a.now else check.parse_now(None)
            now_ist = check.to_ist(now)
            if not a.now and not session_open(now_ist):
                if loop and now_ist.time() >= CLOSE:
                    print("the session is over", flush=True)
                    return 0
                if not loop:
                    print("the market is not open (%s IST): nothing to guard" % now_ist.strftime("%a %H:%M"))
                    return 0
                time.sleep(a.every)
                continue
            try:
                state = ticket.load_state(a.state)
                res = one_pass(kite, state, cfg, now, armed, say=lambda s: print(s, flush=True), log=append_log(LOG))
                fails = 0
                for x in res["alerts"]:
                    if x not in told:
                        told.add(x)
                        print("ALERT  " + x, flush=True)
                        notify(x)
                heartbeat(HEARTBEAT, now, armed, res)
            except KiteError as e:
                fails += 1
                heartbeat(HEARTBEAT, now, armed, error=str(e))
                print("Kite: %s" % e, flush=True)
                if e.kind == "TokenException":
                    notify("the Kite session has expired: run guardian.py login. Positions are NOT being guarded")
                    return 1
                if fails == FAIL_ALERT:
                    notify("cannot read Kite (%s), %d passes in a row: positions are NOT being guarded" % (e, fails))
            if not loop:
                return 0
            time.sleep(a.every)
    finally:
        lock.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Keeps live intraday positions protected at Zerodha (risk-only).")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", default="trader/config.json")
        sp.add_argument("--state", default="trader/state/broker.json")
        sp.add_argument("--session", default=SESSION)

    for name in ("run", "once"):
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--arm", action="store_true", help="send the orders (default: a dry run)")
        sp.add_argument("--every", type=float, default=POLL, help="seconds between passes")
        sp.add_argument("--now", help="pretend it is this IST time (tests)")
    sp = sub.add_parser("plan", help="offline: what it would do with saved get_positions and get_orders answers")
    common(sp)
    sp.add_argument("--positions", required=True)
    sp.add_argument("--broker-orders", required=True)
    sp.add_argument("--now")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("login", help="trade Kite's request token for today's access token")
    sp.add_argument("--request-token", required=True)
    sp.add_argument("--session", default=SESSION)
    a = p.parse_args(argv)
    if getattr(a, "now", None) and a.cmd == "run":
        a.cmd = "once"                    # a pinned clock is one pass, never a loop
    try:
        if a.cmd == "login":
            return cmd_login(a)
        if a.cmd == "plan":
            return cmd_plan(a)
        return run(a, loop=a.cmd == "run")
    except (GuardianError, ticket.TicketError, check.RiskError, KiteError) as e:
        print("guardian: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
