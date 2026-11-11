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


if __name__ == "__main__":
    unittest.main()
