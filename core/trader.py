#!/usr/bin/env python3
"""trader: one command for the whole pack. Run it from your trading folder (or set TRADER_HOME).

    python3 core/trader.py <command> [its own options]
    python3 core/trader.py help                     # this list
    python3 core/trader.py <command> --help         # a command's options

The commands are the skills' tools under one name, plus a few of the pack's own (doctor, migrate, stop, resume,
version, mcp, package). Each tool still runs on its own as before (python3 trader-risk/check.py ...): this is a
front door, not a second copy. In Docker: docker compose -f docker/compose.yaml exec trader trader doctor
Standard library only, Python 3.8 and later.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)

# command: (skill folder, script, what it does)
TOOLS = {
    "morning": ("trader-scanner", "morning.py", "the morning brief, with the pre-flight's GO or NO-GO"),
    "scan": ("trader-scanner", "scan.py", "scan NSE charts for setups; build the universe and price bands"),
    "news": ("trader-news", "news.py", "Indian business news on your watchlist"),
    "entry": ("trader-entry", "entry.py", "watch a setup and call the entry"),
    "exit": ("trader-exit", "exit.py", "set the stop and the target"),
    "timemachine": ("trader-timemachine", "timemachine.py", "replay a trade plan 10,000 times"),
    "edge": ("trader-timemachine", "edge.py", "the intraday edge report"),
    "swing": ("trader-timemachine", "swing.py", "the swing edge report on daily bars"),
    "openclose": ("trader-timemachine", "openclose.py", "open-to-close rules on daily bars"),
    "tom": ("trader-timemachine", "tom.py", "turn-of-the-month against buy and hold"),
    "rotation": ("trader-timemachine", "rotation.py", "the factor rotation report"),
    "archive": ("trader-timemachine", "archive.py", "save today's bars beyond Yahoo's window"),
    "research": ("trader-timemachine", "research.py", "a few hypotheses, walk-forward, on years of bars you bring"),
    "size": ("trader-budget", "size.py", "size a trade to your budget and max loss"),
    "sip": ("trader-budget", "sip.py", "the index-fund core tracker"),
    "check": ("trader-risk", "check.py", "the risk check: PASS or BLOCK"),
    "ticket": ("trader-broker", "ticket.py", "tickets, approvals, paper fills, the journal, tax"),
    "forward": ("trader-broker", "forward.py", "the forward test of a frozen rule"),
    "gapshadow": ("trader-broker", "gapshadow.py", "the gap-up shadow test"),
    "preflight": ("trader-broker", "preflight.py", "today's GO or NO-GO"),
    "autopilot": ("trader-broker", "autopilot.py", "the paper autopilot (paper only)"),
    "daily": ("trader-broker", "daily.py", "the end-of-day report"),
    "audit": ("trader-broker", "audit.py", "the trade audit page"),
    "schedule": ("trader-broker", "scheduler.py", "the day's schedule (the Docker container's command)"),
    "alerts": ("trader-alerts", "alerts.py", "price alerts and the market clock"),
    "results": ("trader-analyst", "results.py", "a company's quarterly results"),
}
OWN = {
    "doctor": "is this install and this trading folder ready to run? (--online, --json, --bundle FILE)",
    "migrate": "bring trader/config.json to the current format (a backup is kept)",
    "setup": "a new trading folder: the lists, and a paper config from the example",
    "stop": "the stop switch: no new entries, paper or live, until `resume` (exits still run)",
    "resume": "turn the stop switch off",
    "version": "the pack's version and code fingerprint",
    "mcp": "run the pack's MCP server on stdin/stdout (Claude Code starts it from the plugin)",
    "package": "build standalone skill zips (for claude.ai) in dist/",
}
STOP_FILE = os.path.join("trader", "STOP")


def usage():
    lines = ["trader: the day-trader pack's tools under one command (run it in your trading folder)", "",
             "Pack commands:"]
    lines += ["  %-12s %s" % (k, v) for k, v in OWN.items()]
    lines += ["", "Tools (each takes its own --help):"]
    lines += ["  %-12s %s" % (k, v[2]) for k, v in TOOLS.items()]
    return "\n".join(lines)


def home():
    """TRADER_HOME, when set, is the trading folder every command works in."""
    h = os.environ.get("TRADER_HOME")
    if h:
        os.chdir(os.path.expanduser(h))


def _pack(name):
    sys.path.insert(0, HERE)
    if name == "doctor":
        import doctor
        return doctor
    if name == "package":
        import package
        return package
    if name == "mcp":
        import mcp_server
        return mcp_server
    raise KeyError(name)


def stop(argv):
    why = " ".join(argv).strip() or "stopped by hand"
    os.makedirs("trader", exist_ok=True)
    with open(STOP_FILE, "w", encoding="utf-8") as fh:
        fh.write(why + "\n")
    print("Stop switch ON (%s): no new entries, paper or live, until `trader.py resume`. Exits, stops and the "
          "flat-by close still run." % STOP_FILE)
    return 0


def resume(argv):
    if not os.path.exists(STOP_FILE):
        print("The stop switch is already off.")
        return 0
    os.remove(STOP_FILE)
    print("Stop switch OFF: new entries are allowed again, within every other limit.")
    return 0


def version(argv):
    doctor = _pack("doctor")
    sys.path.insert(0, os.path.join(CODE, "trader-broker"))
    import forward
    print("day-trader-skill-india %s, code %s" % (doctor.plugin_version() or "unknown", forward.code_version(CODE)))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(usage())
        return 0
    cmd, rest = argv[0], argv[1:]
    home()
    if cmd in TOOLS:
        folder, script, _ = TOOLS[cmd]
        return subprocess.call([sys.executable or "python3", os.path.join(CODE, folder, script)] + rest)
    if cmd == "doctor":
        return _pack("doctor").main(rest)
    if cmd == "migrate":
        return _pack("doctor").migrate_main(rest)
    if cmd == "setup":
        return subprocess.call([sys.executable or "python3", os.path.join(CODE, "trader-broker", "scheduler.py"),
                                "setup"] + rest)
    if cmd == "stop":
        return stop(rest)
    if cmd == "resume":
        return resume(rest)
    if cmd == "version":
        return version(rest)
    if cmd == "mcp":
        return _pack("mcp").main(rest)
    if cmd == "package":
        return _pack("package").main(rest)
    print("trader: no command %r\n\n%s" % (cmd, usage()), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
