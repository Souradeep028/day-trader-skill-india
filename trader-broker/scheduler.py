#!/usr/bin/env python3
"""The day's schedule for trader-broker, in one long-running process: what cron does, for a container.

Run it from the trading folder (in Docker, /data, the folder mounted from your machine). Every NSE weekday, in
India time whatever the machine's clock zone:

    08:40           the morning brief (trader-scanner morning.py), with the pre-flight's GO or NO-GO
    09:16 - 15:31   a paper autopilot pass a minute after each 5-minute bar closes (the pre-flight first, if
                    today's is missing, so a late start still trades on a GO only)
    15:45           today's 5-minute and 1-minute bars saved (trader-timemachine archive.py)
    15:50           the daily report, then the trade audit page (trader/audit/index.html)

A container that starts late catches up: the passes from now on, and the evening jobs if they have not run
today. Weekends are skipped here; the tools themselves skip NSE holidays. Paper only: nothing here can place
a live order. Each job's output goes to trader/logs/<job>.log; trader/state/scheduler.json says what ran.

    python3 scheduler.py run                  # forever (the container's command)
    python3 scheduler.py setup                # a new trading folder: the lists, and a config from the example
    python3 scheduler.py now daily            # one job right away: morning, preflight, pass, archive, daily
    python3 scheduler.py plan [--now "2026-11-11 08:00"]
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
STATE = os.path.join("trader", "state", "scheduler.json")
LOGS = os.path.join("trader", "logs")
PY = sys.executable or "python3"


def tool(*parts):
    return [PY, os.path.join(CODE, *parts)]


JOBS = {
    "morning": [tool("trader-scanner", "morning.py")],
    "preflight": [tool("trader-broker", "preflight.py")],
    "pass": [tool("trader-broker", "autopilot.py") + ["run", "--once"]],
    "archive": [tool("trader-timemachine", "archive.py") + ["save"],
                tool("trader-timemachine", "archive.py") + ["save", "--interval", "1m"]],
    "daily": [tool("trader-broker", "daily.py"), tool("trader-broker", "audit.py")],
}
MORNING = datetime.time(8, 40)
FIRST_PASS = datetime.time(9, 16)
LAST_PASS = datetime.time(15, 31)
ARCHIVE = datetime.time(15, 45)
DAILY = datetime.time(15, 50)


def ist_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def plan(day):
    """[(IST datetime, job)] for one day, in order. Nothing on a Saturday or Sunday."""
    if day.weekday() >= 5:
        return []
    at = lambda t: datetime.datetime.combine(day, t)  # noqa: E731
    out = [(at(MORNING), "morning")]
    t = at(FIRST_PASS)
    while t <= at(LAST_PASS):
        out.append((t, "pass"))
        t += datetime.timedelta(minutes=5)
    out += [(at(ARCHIVE), "archive"), (at(DAILY), "daily")]
    return out


def due(now, done):
    """The jobs to run now and the next time to wake. done: {job: date ran last}. A pass is due only in its own
    5-minute slot (a missed pass is not worth running late); the morning brief until 9:15; the evening jobs any
    time later that day if they have not run."""
    today = now.date().isoformat()
    run = []
    for t, job in plan(now.date()):
        if t > now:
            continue
        if job == "pass":
            if now - t < datetime.timedelta(minutes=4) and done.get("pass_at") != t.strftime("%Y-%m-%d %H:%M"):
                run = [j for j in run if j != "pass"] + ["pass"]
        elif done.get(job) != today:
            if job == "morning" and now.time() >= datetime.time(9, 15):
                continue
            run.append(job)
    later = [t for t, _ in plan(now.date()) if t > now]
    if not later:
        d = now.date() + datetime.timedelta(days=1)
        while not plan(d):
            d += datetime.timedelta(days=1)
        later = [plan(d)[0][0]]
    return run, later[0]


def preflight_today(today):
    p = os.path.join("trader", "state", "preflight.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("date") == today
    except (OSError, ValueError):
        return False


def run_job(job, now):
    os.makedirs(LOGS, exist_ok=True)
    codes = []
    with open(os.path.join(LOGS, "%s.log" % ("autopilot" if job == "pass" else job)), "a", encoding="utf-8") as log:
        log.write("== %s IST %s\n" % (now.strftime("%Y-%m-%d %H:%M:%S"), job))
        log.flush()
        steps = list(JOBS[job])
        if job == "pass" and not preflight_today(now.date().isoformat()):
            steps = JOBS["preflight"] + steps
        for argv in steps:
            try:
                codes.append(subprocess.call(argv, stdout=log, stderr=subprocess.STDOUT, timeout=1200))
            except subprocess.TimeoutExpired:
                log.write("timed out after 20 minutes\n")
                codes.append(124)
    return codes


def read_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(doc):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, STATE)


def loop(once=False):
    while True:
        now = ist_now()
        st = read_state()
        done = st.setdefault("done", {})
        jobs, wake = due(now, done)
        for job in jobs:
            codes = run_job(job, now)
            if job == "pass":
                done["pass_at"] = [t for t, j in plan(now.date()) if j == "pass" and t <= now][-1].strftime("%Y-%m-%d %H:%M")
            else:
                done[job] = now.date().isoformat()
            st.setdefault("last", {})[job] = {"at": now.strftime("%Y-%m-%d %H:%M"), "exit_codes": codes}
            print("%s IST %s: exit %s" % (now.strftime("%H:%M"), job, codes), flush=True)
        st["alive"] = ist_now().strftime("%Y-%m-%d %H:%M:%S")
        st["next"] = wake.strftime("%Y-%m-%d %H:%M")
        write_state(st)
        if once:
            return 0
        time.sleep(max(5.0, min(300.0, (wake - ist_now()).total_seconds() + 5)))


def setup():
    """A new trading folder: NSE's lists from the pack, and trader/config.json from the example (paper)."""
    os.makedirs(os.path.join("trader", "state"), exist_ok=True)
    made = []
    src = os.path.join(CODE, "trader")
    for f in sorted(os.listdir(src)):
        dst = os.path.join("trader", f)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(src, f), dst)
            made.append(dst)
    cfg = os.path.join("trader", "config.json")
    if not os.path.exists(cfg):
        shutil.copy(os.path.join(CODE, "trader-budget", "config.example.json"), cfg)
        made.append(cfg)
    print("Trading folder %s: %s." % (os.getcwd(), ("added " + ", ".join(made)) if made else "nothing to add"))
    if not os.path.exists(os.path.join("trader", "forward.json")):
        print("No frozen rule yet: forward.py start --rule ... (see the README), or copy your trader/forward.json in.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The day's schedule in one process (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run").add_argument("--once", action="store_true", help="one check, then exit (tests)")
    sub.add_parser("setup")
    n = sub.add_parser("now")
    n.add_argument("job", choices=sorted(JOBS))
    p = sub.add_parser("plan")
    p.add_argument("--now", help="pretend it is this IST time, YYYY-MM-DD HH:MM")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print("scheduler: trading folder %s, code %s, India time %s" % (os.getcwd(), CODE,
                                                                     ist_now().strftime("%Y-%m-%d %H:%M")), flush=True)
        return loop(a.once)
    if a.cmd == "setup":
        return setup()
    if a.cmd == "now":
        codes = run_job(a.job, ist_now())
        print("%s: exit %s (log in %s)" % (a.job, codes, LOGS))
        return 0 if all(c == 0 for c in codes) else 1
    if a.cmd == "plan":
        now = datetime.datetime.strptime(a.now, "%Y-%m-%d %H:%M") if a.now else ist_now()
        jobs, wake = due(now, read_state().get("done", {}))
        print("At %s IST: due now %s; next wake %s." % (now.strftime("%Y-%m-%d %H:%M"), ", ".join(jobs) or "nothing",
                                                     wake.strftime("%Y-%m-%d %H:%M")))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
