"""The pack's shared tables, in one place: the NSE calendar and tick sizes (core/nse.json) and each broker's charges
(core/brokers/<broker>.json). Every skill reads these files instead of keeping its own copy.

Where the files are found, in order:
  1. TRADER_CORE (an environment variable naming a folder), for tests and unusual installs;
  2. the folder this file is in (core/ in the repo, or a standalone skill zip built by core/package.py);
  3. ../core next to a skill folder.
Standard library only, Python 3.8 and later.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE = {}


def _folders():
    env = os.environ.get("TRADER_CORE")
    out = [env] if env else []
    out += [HERE, os.path.join(os.path.dirname(HERE), "core")]
    return out


def _read(rel):
    if rel in _CACHE:
        return _CACHE[rel]
    for base in _folders():
        path = os.path.join(base, rel)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                _CACHE[rel] = json.load(fh)
            return _CACHE[rel]
    raise FileNotFoundError("the pack's shared table %s is missing (looked in %s): reinstall the plugin, or keep the "
                            "core folder next to the skill folders" % (rel, ", ".join(_folders())))


def nse():
    """The NSE calendar: holidays, calendar_years, early_close, special_sessions, ticks, top_tick."""
    return _read("nse.json")


def holidays():
    return dict(nse()["holidays"])


def calendar_years():
    return tuple(nse()["calendar_years"])


def early_close():
    return dict(nse()["early_close"])


def special_sessions():
    return dict(nse()["special_sessions"])


def ticks(kind=float):
    """((price below, tick), ...) and the tick above the last band, as floats or, with kind=Decimal, Decimals."""
    n = nse()
    return tuple((kind(str(a)), kind(t)) for a, t in n["ticks"]), kind(n["top_tick"])


def broker(name="zerodha"):
    """A broker's charges table: {"name", "source", "read", "charges": {...}}."""
    safe = "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "-_")
    return _read(os.path.join("brokers", safe + ".json"))


def charges(name="zerodha"):
    return dict(broker(name)["charges"])


def order_charges(side, value, rates=None, name="zerodha"):
    """Charges on one executed intraday order of this value (rupees), each to the paisa: brokerage (a percent, capped
    per order), STT on a sale, exchange and SEBI fees, stamp duty on a buy, and GST on brokerage and fees. rates
    overrides the broker's table key by key ("charges" in trader/config.json). The one copy: every skill calls this."""
    r = charges(name)
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


def slipped_stop(stop, long, bp):
    """Where an SL-M stop is assumed to fill: bp basis points past its trigger (stop_slippage_bp), to the paisa,
    away from the entry. 0 bp is the trigger itself."""
    bp = float(bp or 0)
    if bp <= 0:
        return stop
    cents = stop * 100.0 * (1 - bp / 1e4) if long else stop * 100.0 * (1 + bp / 1e4)
    import math
    return (math.floor(cents + 1e-6) if long else math.ceil(cents - 1e-6)) / 100.0


def charges_read(name="zerodha"):
    return broker(name)["read"]


def brokers():
    for base in _folders():
        folder = os.path.join(base, "brokers")
        if os.path.isdir(folder):
            return sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json"))
    return []
