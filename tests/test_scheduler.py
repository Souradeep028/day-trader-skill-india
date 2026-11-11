"""trader-broker's scheduler.py (the day's schedule in one process, for Docker) and the docker/ files."""

import datetime
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load, ROOT

sch = load("trader-broker", "scheduler")
DT = datetime.datetime


class Plan(unittest.TestCase):
    def test_a_weekday_and_a_weekend(self):
        p = sch.plan(datetime.date(2026, 11, 11))
        # the brief, 76 passes from 9:16 to 15:31 every 5 minutes, the archive and the daily report
        self.assertEqual(len(p), 79)
        self.assertEqual([j for _, j in p].count("pass"), 76)
        self.assertEqual((p[0], p[1][0].strftime("%H:%M"), p[-3][0].strftime("%H:%M")),
                         ((DT(2026, 11, 11, 8, 40), "morning"), "09:16", "15:31"))
        self.assertEqual(p[-2:], [(DT(2026, 11, 11, 15, 45), "archive"), (DT(2026, 11, 11, 15, 50), "daily")])
        self.assertEqual(sch.plan(datetime.date(2026, 11, 14)), [])

    def test_what_is_due(self):
        self.assertEqual(sch.due(DT(2026, 11, 11, 8, 59), {}), (["morning"], DT(2026, 11, 11, 9, 16)))
        # a late start: no brief after 9:15, but this slot's pass (the autopilot runs the pre-flight first)
        self.assertEqual(sch.due(DT(2026, 11, 11, 9, 17), {}), (["pass"], DT(2026, 11, 11, 9, 21)))
        self.assertEqual(sch.due(DT(2026, 11, 11, 9, 19), {"pass_at": "2026-11-11 09:16"})[0], [])
        self.assertEqual(sch.due(DT(2026, 11, 11, 9, 21, 5), {"pass_at": "2026-11-11 09:16"})[0], ["pass"])
        # a pass more than 4 minutes late is not run: the next slot is
        self.assertEqual(sch.due(DT(2026, 11, 11, 9, 20, 30), {"pass_at": "2026-11-11 09:11"})[0], [])
        done = {"morning": "2026-11-11", "pass_at": "2026-11-11 15:31"}
        self.assertEqual(sch.due(DT(2026, 11, 11, 15, 47), done), (["archive"], DT(2026, 11, 11, 15, 50)))
        # evening jobs catch up later the same day; after them, the next wake is Monday's brief on a Friday
        self.assertEqual(sch.due(DT(2026, 11, 11, 21, 0), dict(done, archive="2026-11-11"))[0], ["daily"])
        fri = {"morning": "2026-11-13", "archive": "2026-11-13", "daily": "2026-11-13", "pass_at": "2026-11-13 15:31"}
        self.assertEqual(sch.due(DT(2026, 11, 13, 16, 0), fri), ([], DT(2026, 11, 16, 8, 40)))


class Jobs(unittest.TestCase):
    def setUp(self):
        self.old, self.d = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.d)
        self.jobs = dict(sch.JOBS)

    def tearDown(self):
        sch.JOBS.clear()
        sch.JOBS.update(self.jobs)
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def test_a_pass_runs_the_preflight_first_when_today_has_none(self):
        say = lambda w: [[sys.executable, "-c", "print(%r)" % w]]  # noqa: E731
        sch.JOBS.update({"preflight": say("preflight ran"), "pass": say("pass ran")})
        now = DT(2026, 11, 11, 9, 16)
        self.assertEqual(sch.run_job("pass", now), [0, 0])
        os.makedirs("trader/state", exist_ok=True)
        with open("trader/state/preflight.json", "w") as fh:
            fh.write('{"date": "2026-11-11", "go": true}')
        self.assertEqual(sch.run_job("pass", now), [0])
        with open("trader/logs/autopilot.log") as fh:
            log = fh.read()
        self.assertEqual(log.count("preflight ran"), 1)
        self.assertEqual(log.count("pass ran"), 2)

    def test_setup_a_new_trading_folder(self):
        out = io.StringIO()
        with redirect_stdout(out):
            sch.main(["setup"])
        self.assertEqual(sorted(os.listdir("trader")), ["banknifty.txt", "config.json", "nifty100.txt", "nifty50.txt",
                                                        "sectors.json", "state"])
        self.assertIn("No frozen rule yet", out.getvalue())
        with redirect_stdout(io.StringIO()) as again:
            sch.main(["setup"])
        self.assertIn("nothing to add", again.getvalue())

    def test_one_loop_records_what_ran(self):
        sch.JOBS.update({"morning": [[sys.executable, "-c", "pass"]]})
        real = sch.ist_now
        sch.ist_now = lambda: DT(2026, 11, 11, 8, 41)
        try:
            with redirect_stdout(io.StringIO()):
                sch.loop(once=True)
        finally:
            sch.ist_now = real
        st = sch.read_state()
        self.assertEqual((st["done"]["morning"], st["next"]), ("2026-11-11", "2026-11-11 09:16"))


class Hardening(unittest.TestCase):
    def setUp(self):
        self.old, self.d = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.d)
        self.jobs, self.timeout, self.notify = dict(sch.JOBS), dict(sch.TIMEOUT), sch.notify

    def tearDown(self):
        sch.JOBS.clear()
        sch.JOBS.update(self.jobs)
        sch.TIMEOUT.clear()
        sch.TIMEOUT.update(self.timeout)
        sch.notify = self.notify
        os.chdir(self.old)
        shutil.rmtree(self.d)

    def test_a_stuck_step_is_stopped_at_its_own_limit(self):
        sch.JOBS["archive"] = [[sys.executable, "-c", "import time; time.sleep(30)"]]
        sch.TIMEOUT["archive"] = 1
        self.assertEqual(sch.run_job("archive", DT(2026, 11, 11, 15, 45)), [124])
        with open("trader/logs/archive.log") as fh:
            self.assertIn("stopped: over the 1-second limit for archive", fh.read())
        self.assertLess(sch.TIMEOUT.get("pass", self.timeout["pass"]), 300)   # a pass ends before the next is due
        self.assertLess(self.timeout["pass"], 300)

    def test_logs_rotate(self):
        os.makedirs("trader/logs")
        with open("trader/logs/morning.log", "w") as fh:
            fh.write("x" * (sch.LOG_MAX + 1))
        sch.JOBS["morning"] = [[sys.executable, "-c", "pass"]]
        sch.run_job("morning", DT(2026, 11, 11, 8, 40))
        self.assertTrue(os.path.exists("trader/logs/morning.log.1"))
        self.assertLess(os.path.getsize("trader/logs/morning.log"), 1000)

    def test_alerts_once_a_day_each(self):
        got = []
        sch.notify = lambda text, url=None: got.append(text) or True
        done, now = {}, DT(2026, 11, 11, 15, 50)
        sch.alerts("archive", [1], now, done)
        sch.alerts("archive", [1], now, done)
        os.makedirs("trader/state")
        with open("trader/state/preflight.json", "w") as fh:
            fh.write('{"date": "2026-11-11", "go": false, "closed": false, "no_go": ["data: Yahoo is a session behind"]}')
        sch.alerts("morning", [0], DT(2026, 11, 11, 8, 40), done)
        sch.alerts("pass", [0], DT(2026, 11, 11, 9, 16), done)
        sch.alerts("daily", [0], now, done)
        self.assertEqual(len(got), 3, got)
        self.assertIn("the archive job failed (exit [1])", got[0])
        self.assertIn("pre-flight NO-GO, no new paper trades today: data: Yahoo is a session behind", got[1])
        self.assertIn("Daily report: not written today", got[2])
        # a new day starts the count again
        sch.alerts("archive", [1], DT(2026, 11, 12, 15, 45), done)
        self.assertEqual(len(got), 4)

    def test_notify_posts_plain_text_and_never_raises(self):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.assertTrue(self.notify("NO-GO today", "http://127.0.0.1:%d/topic" % srv.server_address[1]))
        srv.server_close()
        self.assertEqual(seen, ["NO-GO today"])
        self.assertFalse(self.notify("x", ""))                                # off without a URL
        self.assertFalse(self.notify("x", "http://127.0.0.1:9/nothing-here"))  # a dead URL is not an error


class DockerFiles(unittest.TestCase):
    def test_the_image_runs_the_scheduler_on_a_mounted_folder(self):
        with open(os.path.join(ROOT, "docker", "Dockerfile")) as fh:
            df = fh.read()
        self.assertIn('ENTRYPOINT ["python3", "/app/trader-broker/scheduler.py"]', df)
        self.assertIn("WORKDIR /data", df)
        self.assertRegex(df, r"FROM python:\d+\.\d+\.\d+-slim")              # an exact version
        for s in ("trader-broker", "trader-risk", "trader-scanner", "trader-timemachine", "trader-budget"):
            self.assertIn("COPY %s/ %s/" % (s, s), df)
        with open(os.path.join(ROOT, "docker", "compose.yaml")) as fh:
            dc = fh.read()
        self.assertIn(":/data", dc)
        self.assertIn("restart: unless-stopped", dc)
        with open(os.path.join(ROOT, "docker", "Dockerfile.dockerignore")) as fh:
            ig = fh.read()
        self.assertIn("trader/config.json", ig)                              # personal settings stay out
        # the image builds only when the test suite passes on it
        self.assertIn("RUN python3 -m unittest discover -s tests", df)
        self.assertIn("COPY --from=test /tmp/tests-passed", df)
        self.assertNotIn("\ntests\n", "\n" + ig)


if __name__ == "__main__":
    unittest.main()
