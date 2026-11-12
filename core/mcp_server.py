#!/usr/bin/env python3
"""The pack's MCP server: typed tools Claude can call instead of running scripts through the shell.

Claude Code starts it from the plugin (.mcp.json at the repo root); it speaks MCP over stdin and stdout, one
JSON-RPC message a line. It works in the trading folder: TRADER_HOME if set, else the folder Claude Code was
started in.

What it can do, on purpose:
  - read: doctor, status, forward_tests, journal, daily_report, schedule
  - one write, and only in the safe direction: stop (the stop switch ON: no new entries). Turning it off again
    is `trader.py resume`, typed by you.
There is no tool that places, approves or changes an order. Live orders go through the broker's own MCP (Kite),
with its order tools set to "Needs approval", and each one still needs your yes.
Standard library only, Python 3.8 and later.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
PY = sys.executable or "python3"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TIMEOUT = 120

TOOLS = [
    {"name": "doctor", "title": "Health check",
     "description": "Is the install and the trading folder ready to run: Python, the shared tables, the NSE holiday "
                    "list, the config's format and keys, paper or live, the stop switch, the forward tests (and "
                    "whether the code changed during them), the scheduler's heartbeat, Yahoo's data lag, disk.",
     "inputSchema": {"type": "object", "properties": {
         "online": {"type": "boolean", "description": "also check that Yahoo answers and the clock is right"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "status", "title": "Today's status",
     "description": "Today's pre-flight GO or NO-GO and why, the stop switch, the scheduler's last and next job, and "
                    "the autopilot's last pass.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "forward_tests", "title": "Forward tests",
     "description": "The frozen rules' results since they were frozen: the ORB forward test (shadow and paper) and the "
                    "gap-up shadow test, against what the backtest expected.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "journal", "title": "Trade journal",
     "description": "Closed trades: win rate, average R, P&L after charges, profit factor, payoff, worst fall.",
     "inputSchema": {"type": "object", "properties": {
         "from": {"type": "string", "description": "YYYY-MM-DD, the first day to count"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "daily_report", "title": "Daily report",
     "description": "The end-of-day report the daily run wrote (trader/daily/<date>.md): the latest, or a given day's.",
     "inputSchema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD; default the latest"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "schedule", "title": "The day's schedule",
     "description": "What the daily run does next and when (India time).",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "stop", "title": "Stop new entries",
     "description": "Turn the stop switch ON: no new entries, paper or live, until the user types `trader.py resume`. "
                    "Exits, stops and the flat-by close still run. Use it when the user asks to stop trading.",
     "inputSchema": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "why, in the user's words"}}, "required": ["reason"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}},
]


def folder():
    h = os.environ.get("TRADER_HOME")
    return os.path.expanduser(h) if h else os.getcwd()


def run(*args):
    try:
        r = subprocess.run([PY, os.path.join(HERE, "trader.py")] + list(args), cwd=folder(), capture_output=True,
                           text=True, timeout=TIMEOUT, env=dict(os.environ, TRADER_HOME=folder()))
    except subprocess.TimeoutExpired:
        return "stopped after %d s" % TIMEOUT, True
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return out.strip() or "(no output)", r.returncode not in (0, 1)


def _read(rel):
    try:
        with open(os.path.join(folder(), rel), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def status():
    L = ["Trading folder: %s" % folder()]
    pf = _read("trader/state/preflight.json")
    if pf:
        L.append("Pre-flight %s: %s%s" % (pf.get("date"), "closed" if pf.get("closed") else
                                          ("GO" if pf.get("go") else "NO-GO"),
                                          "" if pf.get("go") else " (%s)" % "; ".join(pf.get("no_go") or [])))
    else:
        L.append("No pre-flight yet.")
    stop_path = os.path.join(folder(), "trader", "STOP")
    L.append("Stop switch: %s" % ("ON" if os.path.exists(stop_path) else "off"))
    sc = _read("trader/state/scheduler.json")
    if sc:
        L.append("Scheduler: alive %s, last %s, next %s, code %s" % (sc.get("alive"), json.dumps(sc.get("last")),
                                                                  sc.get("next"), sc.get("code")))
    hb = _read("trader/state/autopilot.json")
    if hb:
        errs = hb.get("errors") or []
        L.append("Autopilot %s: %d passes, last %s, last clean %s%s" % (
            hb.get("date"), hb.get("passes", 0), hb.get("last_pass", "-"), hb.get("last_ok", "-"),
            (", latest problem at %s: %s" % (errs[-1].get("at"), errs[-1].get("error"))) if errs else ""))
    return "\n".join(L), False


def daily_report(date=None):
    d = os.path.join(folder(), "trader", "daily")
    if not os.path.isdir(d):
        return "No daily reports yet (trader/daily/).", False
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    name = (date + ".md") if date else (files[-1] if files else None)
    if not name or name not in files:
        return "No report for %s. There are: %s" % (date, ", ".join(f[:-3] for f in files[-10:]) or "none"), False
    with open(os.path.join(d, name), encoding="utf-8") as fh:
        return fh.read(), False


def call(name, args):
    args = args or {}
    if name == "doctor":
        return run("doctor", *(["--online"] if args.get("online") else []))
    if name == "status":
        return status()
    if name == "forward_tests":
        a, e1 = run("forward", "status")
        b, e2 = run("gapshadow", "status")
        return "%s\n\n%s" % (a, b), e1 and e2
    if name == "journal":
        return run("ticket", "journal", *(["--from", str(args["from"])] if args.get("from") else []))
    if name == "daily_report":
        return daily_report(args.get("date"))
    if name == "schedule":
        return run("schedule", "plan")
    if name == "stop":
        return run("stop", str(args.get("reason") or "asked in Claude"))
    raise KeyError(name)


def handle(msg):
    """One JSON-RPC message -> the reply, or None for a notification."""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "day-trader-india", "version": _version()},
                  "instructions": "Read-only tools for the NSE day-trading pack, plus `stop` (no new entries). None "
                                  "of them places an order."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            text, err = call(p.get("name"), p.get("arguments"))
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "no tool %r" % p.get("name")}}
        result = {"content": [{"type": "text", "text": text}], "isError": bool(err)}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no method %r" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _version():
    try:
        with open(os.path.join(CODE, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            return json.load(fh).get("version") or "0"
    except (OSError, ValueError):
        return "0"


def main(argv=None):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
