"""Being told when the live guardian is not running: its heartbeat, the stop alerts, the ping, the scheduler's
watchdog and the doctor."""

import argparse
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

check = load("trader-risk", "check")
g = load("trader-broker", "guardian")
scheduler = load("trader-broker", "scheduler")
doctor = load("core", "doctor")

DAY = "2026-11-05"           # a Thursday, a session


def utc(hhmmss):
    return check.parse_now("%s %s" % (DAY, hhmmss))


def ist(hhmmss):
    return datetime.datetime.strptime("%s %s" % (DAY, hhmmss), "%Y-%m-%d %H:%M:%S")


def beat(alive="10:00:00", armed=True, error=None, stopped=None):
    return {"date": DAY, "alive": "%s %s" % (DAY, alive), "armed": armed, "error": error, "stopped": stopped}


class State(unittest.TestCase):
    def test_what_counts_as_alive(self):
        now = utc("10:00:30")
        self.assertEqual(check.guardian_state(beat(), now), (True, "the guardian is armed and checked in 30 seconds ago"))
        cases = [(None, "the guardian has not run"), (beat("09:59:00"), "last checked in 90 seconds ago"),
                 (beat(armed=False), "dry run, not armed"), (beat(error="no answer from Kite"), "last pass failed"),
                 (beat(stopped="crashed: boom"), "the guardian stopped at 10:00:00 (crashed: boom)"),
                 ({"alive": "?"}, "no time in it")]
        for doc, why in cases:
            with self.subTest(why):
                ok, text = check.guardian_state(doc, now)
                self.assertFalse(ok)
                self.assertIn(why, text)


class InFolder(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))
        self.sent = []
        self.notify = scheduler.notify
        scheduler.notify = lambda text, url=None: self.sent.append(text) or True
        self.gnotify = g.notify
        g.notify = lambda text: self.sent.append(text) or True

    def tearDown(self):
        scheduler.notify = self.notify
        g.notify = self.gnotify
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)


class Guardian(InFolder):
    def args(self, **over):
        a = argparse.Namespace(config=os.path.join("trader", "config.json"), state="trader/state/broker.json",
                               session="none", arm=True, every=0.0, now=DAY + " 10:05", portfolio="trader/portfolio.json",
                               no_sync=True, forward="trader/forward.json")
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def run_with(self, kite, **over):
        self.write(os.path.join("trader", "config.json"), {"paper": False})
        real = g.Kite
        g.Kite = lambda *a, **k: kite
        os.environ["KITE_API_KEY"], os.environ["KITE_ACCESS_TOKEN"] = "k", "t"
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return g.run(self.args(**over), loop=False)
        finally:
            g.Kite = real
            del os.environ["KITE_API_KEY"], os.environ["KITE_ACCESS_TOKEN"]

    def heartbeat(self):
        with open(g.HEARTBEAT) as fh:
            return json.load(fh)

    def test_a_good_pass_leaves_a_fresh_armed_heartbeat_and_pings(self):
        class K:
            def positions(self):
                return {"data": {"net": []}}

            def orders(self):
                return {"data": []}
        pinged = []
        real = g.ping
        g.ping = lambda url: pinged.append(url) or True
        os.environ["GUARDIAN_PING_URL"] = "https://hc.example/abc"
        try:
            self.assertEqual(self.run_with(K()), 0)
        finally:
            g.ping = real
            del os.environ["GUARDIAN_PING_URL"]
        self.assertEqual(pinged, ["https://hc.example/abc"])
        hb = self.heartbeat()
        self.assertEqual((hb["armed"], hb["error"], hb["stopped"]), (True, None, None))
        self.assertTrue(check.guardian_state(hb, utc("10:05:20"))[0])

    def test_a_crash_alerts_on_the_way_down(self):
        class K:
            def positions(self):
                raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_with(K())
        self.assertEqual(self.sent, ["STOPPED during the session, crashed: RuntimeError: boom: live positions are NOT "
                                     "being guarded"])
        ok, why = check.guardian_state(self.heartbeat(), utc("10:05:10"))
        self.assertFalse(ok)
        self.assertIn("stopped", why)

    def test_ctrl_c_and_a_signal(self):
        class K:
            def positions(self):
                raise KeyboardInterrupt
        self.assertEqual(self.run_with(K()), 130)
        self.assertIn("stopped by hand (Ctrl-C)", self.sent[0])
        self.assertEqual(g.stop_reason_of(SystemExit(143)), "stopped by a signal (exit 143)")

    def test_an_expired_kite_session_says_so(self):
        class K:
            def positions(self):
                raise g.KiteError("Incorrect `api_key` or `access_token`.", "TokenException", 403)
        self.assertEqual(self.run_with(K()), 1)
        self.assertIn("the Kite session has expired", self.sent[0])
        self.assertEqual(self.heartbeat()["stopped"], "the Kite session expired")


class Watchdog(InFolder):
    def live(self, **over):
        c = {"paper": False}
        c.update(over)
        self.write(os.path.join("trader", "config.json"), c)

    def test_one_alert_when_it_goes_quiet_and_one_when_it_is_back(self):
        self.live()
        done = {}
        self.assertTrue(scheduler.guardian_watch(ist("09:15:30"), done))          # the minute's grace
        self.assertEqual(self.sent, [])
        self.assertTrue(scheduler.guardian_watch(ist("09:20:00"), done))          # never started
        self.assertEqual(len(self.sent), 1)
        self.assertIn("guardian DOWN: the guardian has not run", self.sent[0])
        self.assertIn("new live entries are blocked", self.sent[0])
        scheduler.guardian_watch(ist("09:20:30"), done)
        self.assertEqual(len(self.sent), 1)                                         # once per outage
        self.write(os.path.join("trader", "state", "guardian.json"), beat("09:21:00"))
        scheduler.guardian_watch(ist("09:21:20"), done)
        self.assertEqual(self.sent[1], "%s 09:21: guardian is back (down since 09:20:00)" % DAY)
        scheduler.guardian_watch(ist("09:25:00"), done)                            # quiet again
        self.assertIn("last checked in 240 seconds ago", self.sent[2])

    def test_it_watches_only_a_live_session(self):
        done = {}
        self.write(os.path.join("trader", "config.json"), {"paper": True})
        self.assertFalse(scheduler.guardian_watch(ist("10:00:00"), done))
        self.live(require_guardian=False)
        self.assertFalse(scheduler.guardian_watch(ist("10:00:00"), done))
        self.live()
        self.assertFalse(scheduler.guardian_watch(ist("15:31:00"), done))
        self.assertFalse(scheduler.guardian_watch(datetime.datetime(2026, 11, 7, 10, 0), done))   # a Saturday
        self.assertFalse(scheduler.guardian_watch(datetime.datetime(2026, 11, 24, 10, 0), done))  # an NSE holiday
        self.assertEqual(self.sent, [])


class Doctor(InFolder):
    def rows(self, hhmmss):
        return {n: (lvl, d) for lvl, n, d in doctor.run(now_utc=utc(hhmmss))}

    def test_the_guardian_row(self):
        self.write(os.path.join("trader", "config.json"), {"paper": False, "budget": 100000})
        lvl, detail = self.rows("10:00:00")["guardian"]
        self.assertEqual(lvl, "FAIL")
        self.assertIn("the guardian has not run", detail)
        self.assertEqual(self.rows("08:30:00")["guardian"][0], "OK")                 # before the session
        self.write(os.path.join("trader", "state", "guardian.json"), beat("10:00:00"))
        self.assertEqual(self.rows("10:00:20")["guardian"][0], "OK")
        self.assertEqual(self.rows("10:00:20")["kite session"][0], "WARN")
        self.write(os.path.join("trader", "state", "kite_session.json"), {"api_key": "k", "access_token": "t",
                                                                          "date": DAY})
        self.assertNotIn("kite session", self.rows("10:00:20"))
        self.write(os.path.join("trader", "config.json"), {"paper": True, "budget": 100000})
        self.assertNotIn("guardian", self.rows("10:00:00"))


if __name__ == "__main__":
    unittest.main()
