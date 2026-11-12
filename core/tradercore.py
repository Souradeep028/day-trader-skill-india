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


def charges_read(name="zerodha"):
    return broker(name)["read"]


def brokers():
    for base in _folders():
        folder = os.path.join(base, "brokers")
        if os.path.isdir(folder):
            return sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json"))
    return []
