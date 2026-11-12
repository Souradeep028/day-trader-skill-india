#!/usr/bin/env python3
"""trader doctor: is this install and this trading folder in a state to run? And trader migrate: bring an older
trader/config.json up to the current format.

Run it from the trading folder (or set TRADER_HOME). Offline by default; --online also checks that Yahoo answers and
that this machine's clock agrees with the internet's (a clock a minute off moves every bar and every window).

    python3 core/trader.py doctor                 # OK / WARN / FAIL lines; exit 1 on any FAIL
    python3 core/trader.py doctor --online
    python3 core/trader.py doctor --json
    python3 core/trader.py doctor --bundle trader/doctor.zip   # a file to attach to a GitHub issue (see below)
    python3 core/trader.py migrate [--dry-run]

The bundle holds this report, trader/config.json, the scheduler's, the autopilot's and the pre-flight's state files
and the last 200 lines of each log. It holds no trades, no positions and no broker keys (the pack never has those:
they live in the Kite MCP's own config). Open it and read it before you share it: the config has your limits.
Standard library only, Python 3.8 and later.
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
for _p in (HERE, os.path.join(CODE, "trader-risk"), os.path.join(CODE, "trader-broker"),
           os.path.join(CODE, "trader-timemachine")):
    if _p not in sys.path:
        sys.path.append(_p)
import tradercore  # noqa: E402
import check  # noqa: E402  (trader-risk: the calendar, the defaults, the stop switch)

CONFIG = os.path.join("trader", "config.json")
EXAMPLE = os.path.join(CODE, "trader-budget", "config.example.json")
CONFIG_VERSION = 3          # 3: the platform release (core/ tables, config_version itself)
OTHER_KEYS = {"config_version", "slippage_ticks"}
STATE_FILES = ("trader/state/scheduler.json", "trader/state/autopilot.json", "trader/state/preflight.json")
OK, WARN, FAIL = "OK", "WARN", "FAIL"


def plugin_version():
    try:
        with open(os.path.join(CODE, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            return json.load(fh).get("version")
    except (OSError, ValueError):
        return None


def known_keys():
    keys = set(check.DEFAULTS) | OTHER_KEYS
    try:
        with open(EXAMPLE, encoding="utf-8") as fh:
            keys |= set(json.load(fh))
    except (OSError, ValueError):
        pass
    return keys


# ---- migrations: each takes a config at version n and returns it at n + 1, with a note of what changed ----

def _to_3(cfg):
    notes = []
    if "broker" not in cfg:
        cfg["broker"] = "zerodha"
        notes.append("added \"broker\": \"zerodha\" (charges now come from core/brokers/<broker>.json)")
    return cfg, notes


MIGRATIONS = {2: _to_3}          # from version: step. A config with no config_version is version 2.


def config_version(cfg):
    try:
        return int(cfg.get("config_version", 2))
    except (TypeError, ValueError):
        return 2


def migrate(cfg):
    """-> (the config at CONFIG_VERSION, [notes]). Never removes or changes a limit you set."""
    cfg, notes = dict(cfg), []
    v = config_version(cfg)
    while v < CONFIG_VERSION:
        step = MIGRATIONS.get(v)
        if step:
            cfg, more = step(cfg)
            notes += more
        v += 1
    if config_version(cfg) != CONFIG_VERSION:
        cfg["config_version"] = CONFIG_VERSION
        notes.append("config_version set to %d" % CONFIG_VERSION)
    return cfg, notes


# ---- the checks ----

def _rupees(x):
    v = check._f(x)
    return check.indian_commas("%.0f" % v) if v else "-"


def _age_text(seconds):
    if seconds < 120:
        return "%d s" % seconds
    if seconds < 7200:
        return "%d min" % (seconds // 60)
    if seconds < 172800:
        return "%.0f h" % (seconds / 3600)
    return "%d days" % (seconds // 86400)


def run(today=None, online=False, now_utc=None):
    """-> [(level, name, detail)]."""
    out = []

    def add(level, name, detail):
        out.append((level, name, detail))

    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    now_ist = check.to_ist(now_utc)
    d = today or now_ist.date()
    # the install
    py = sys.version_info
    add(OK if py >= (3, 8) else FAIL, "python", "%d.%d.%d%s" % (py[0], py[1], py[2],
                                                               "" if py >= (3, 8) else ": the pack needs 3.8 or later"))
    try:
        import forward
        fp = forward.code_version(CODE)
    except Exception as e:  # noqa: BLE001
        fp = "unknown (%s)" % e
    add(OK, "code", "version %s, fingerprint %s, at %s" % (plugin_version() or "unknown", fp, CODE))
    try:
        tradercore.nse()
        tradercore.charges("zerodha")
        add(OK, "shared tables", "core/nse.json and core/brokers/ (%s)" % ", ".join(tradercore.brokers()))
    except (OSError, ValueError) as e:
        add(FAIL, "shared tables", str(e))
        return out
    years = tradercore.calendar_years()
    if d.year not in years:
        add(FAIL, "holidays", "core/nse.json covers %s, not %d: no new entries until this year's NSE holidays are "
            "added" % (", ".join(map(str, years)), d.year))
    elif d.month == 12 and d.year + 1 not in years:
        add(WARN, "holidays", "NSE publishes %d's holidays in December: add them to core/nse.json before 1 January"
            % (d.year + 1))
    else:
        add(OK, "holidays", "NSE calendar for %s" % ", ".join(map(str, years)))
    # the trading folder
    if not os.path.isdir("trader"):
        add(FAIL, "trading folder", "no trader/ folder in %s: run this where you trade, or `trader.py setup` there"
            % os.getcwd())
        return out
    add(OK, "trading folder", os.getcwd())
    cfg = None
    if not os.path.exists(CONFIG):
        add(FAIL, "config", "no trader/config.json: `trader.py setup` writes one from the example (paper)")
    else:
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            add(FAIL, "config", "trader/config.json does not read: %s" % e)
            cfg = None
    if cfg is not None:
        v = config_version(cfg)
        if v < CONFIG_VERSION:
            add(WARN, "config format", "version %d, the code expects %d: `trader.py migrate` updates it (a backup is "
                "kept)" % (v, CONFIG_VERSION))
        elif v > CONFIG_VERSION:
            add(FAIL, "config format", "version %d is newer than this code (%d): update the plugin" % (v, CONFIG_VERSION))
        else:
            add(OK, "config format", "version %d" % v)
        unknown = sorted(set(cfg) - known_keys())
        if unknown:
            add(WARN, "config keys", "not used by the pack, a typo?: %s" % ", ".join(unknown))
        live = cfg.get("paper", True) is False
        budget = check._f(cfg.get("budget"))
        add(FAIL if not budget else (WARN if live else OK), "account",
            ("no budget" if not budget else "%s, budget ₹%s, max loss a trade ₹%s, a day ₹%s" % (
                "LIVE (real money; each order still needs your yes)" if live else "paper",
                check.indian_commas("%.0f" % budget), _rupees(cfg.get("max_loss_per_trade_inr")),
                _rupees(cfg.get("max_daily_loss_inr")))))
        name = str(cfg.get("broker") or "zerodha")
        if name not in tradercore.brokers():
            add(FAIL, "broker", "no charges table for %r in core/brokers/ (there are: %s)"
                % (name, ", ".join(tradercore.brokers())))
        else:
            b = tradercore.broker(name)
            age = (d - datetime.date.fromisoformat(b["read"])).days
            add(WARN if age > 365 else OK, "charges", "%s, read %s (%d days ago)%s" % (
                b["name"], b["read"], age, ": check %s and update core/brokers/%s.json" % (b["source"], name)
                if age > 365 else ""))
    halt = check.stop_reason()
    add(WARN if halt else OK, "stop switch", halt or "off")
    # the forward tests and the daily run
    for f, label in (("trader/forward.json", "ORB forward test"), ("trader/forward-gap.json", "gap shadow test")):
        if os.path.exists(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    t = json.load(fh)
                codes = t.get("code") or {}
                add(WARN if len(codes) > 1 else OK, label, "frozen %s%s" % (
                    t.get("started") or "?",
                    ", the code changed during it (%s)" % ", ".join("%s from %s" % kv for kv in sorted(
                        codes.items(), key=lambda kv: kv[1])) if len(codes) > 1 else
                    (", on code %s" % next(iter(codes)) if codes else "")))
            except (OSError, ValueError, AttributeError) as e:
                add(FAIL, label, "%s does not read: %s" % (f, e))
    sched = _read("trader/state/scheduler.json")
    if sched:
        alive = sched.get("alive")
        try:
            age = (now_ist - datetime.datetime.strptime(alive, "%Y-%m-%d %H:%M:%S")).total_seconds()   # IST
        except (TypeError, ValueError):
            age = None
        if age is None:
            add(WARN, "scheduler", "trader/state/scheduler.json has no alive time")
        else:
            add(WARN if age > 600 else OK, "scheduler", "last alive %s ago%s" % (
                _age_text(age), ": is the container running? (docker compose -f docker/compose.yaml ps)"
                if age > 600 else ", next job %s" % sched.get("next")))
    hb = _read("trader/state/autopilot.json")
    lag = (hb or {}).get("lag") or []
    if lag:
        worst = max(x[2] for x in lag if len(x) > 2)
        typical = sorted(x[1] for x in lag)[len(lag) // 2]
        add(WARN if worst > 180 else OK, "data lag", "Yahoo %d s typical, %d s worst over the last %d passes%s" % (
            typical, worst, len(lag), " (over 180 s: those passes enter nothing)" if worst > 180 else ""))
    free = shutil.disk_usage(".").free
    add(WARN if free < 500 * 2 ** 20 else OK, "disk", "%.1f GB free here" % (free / 2 ** 30))
    try:
        probe = os.path.join("trader", ".doctor-write-test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        add(OK, "write access", "trader/ is writable")
    except OSError as e:
        add(FAIL, "write access", "cannot write in trader/: %s (in Docker, check TRADER_UID)" % e)
    if online:
        out.extend(online_checks(now_utc))
    return out


def online_checks(now_utc=None):
    import email.utils
    import urllib.request
    out = []
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1d&interval=5m"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; day-trader-skill)"})
        t0 = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        with urllib.request.urlopen(req, timeout=15) as r:
            date = r.headers.get("Date")
            r.read(1)
        out.append((OK, "yahoo", "answered in %.1f s" % (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - t0).total_seconds()))
        if date:
            server = email.utils.parsedate_to_datetime(date).replace(tzinfo=None)
            skew = ((now_utc or t0) - server).total_seconds()
            out.append((WARN if abs(skew) > 30 else OK, "clock", "%+.0f s against the internet's clock%s" % (
                skew, ": fix the machine's time sync, bars and windows depend on it" if abs(skew) > 30 else "")))
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "yahoo", "no answer: %s" % e))
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def text(results):
    width = max(len(n) for _, n, _ in results) if results else 0
    lines = ["%-4s  %-*s  %s" % (lvl, width, n, det) for lvl, n, det in results]
    fails = sum(1 for r in results if r[0] == FAIL)
    warns = sum(1 for r in results if r[0] == WARN)
    lines.append("")
    lines.append("Healthy." if not fails and not warns else "%d problem(s), %d warning(s)." % (fails, warns))
    return "\n".join(lines)


def bundle(path, results):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doctor.txt", text(results) + "\n")
        for f in (CONFIG,) + STATE_FILES:
            if os.path.exists(f):
                z.write(f, f)
        logs = os.path.join("trader", "logs")
        if os.path.isdir(logs):
            for f in sorted(os.listdir(logs)):
                if f.endswith(".log"):
                    with open(os.path.join(logs, f), encoding="utf-8", errors="replace") as fh:
                        z.writestr("trader/logs/" + f, "".join(fh.readlines()[-200:]))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trader.py doctor", description=__doc__.split("\n")[0])
    ap.add_argument("--online", action="store_true", help="also check Yahoo and the clock (needs the internet)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bundle", help="also write a zip to attach to an issue (read it before you share it)")
    ap.add_argument("--today", help="YYYY-MM-DD (tests)")
    a = ap.parse_args(argv)
    today = datetime.date.fromisoformat(a.today) if a.today else None
    res = run(today, a.online)
    if a.json:
        print(json.dumps([{"level": l, "check": n, "detail": dt} for l, n, dt in res], indent=1, ensure_ascii=False))
    else:
        print(text(res))
    if a.bundle:
        print("\nBundle: %s (no trades, positions or keys; it does hold your config: read it before you share it)"
              % bundle(a.bundle, res))
    return 1 if any(r[0] == FAIL for r in res) else 0


def migrate_main(argv=None):
    ap = argparse.ArgumentParser(prog="trader.py migrate", description="bring trader/config.json to the current format")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        print("%s does not read: %s" % (a.config, e))
        return 1
    new, notes = migrate(cfg)
    if not notes:
        print("%s is already at version %d: nothing to do." % (a.config, CONFIG_VERSION))
        return 0
    print("\n".join("- " + n for n in notes))
    if a.dry_run:
        print("(dry run: nothing written)")
        return 0
    backup = "%s.v%d.bak" % (a.config, config_version(cfg))
    shutil.copy2(a.config, backup)
    tmp = a.config + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, a.config)
    print("Written %s (the old one is %s). No limit was changed." % (a.config, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
