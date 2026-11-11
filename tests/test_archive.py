"""trader-timemachine's archive.py: intraday bars kept past Yahoo's window. Real 5-minute bars (the edge fixtures,
60 sessions to 6 November 2026)."""

import datetime
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

archive = load("trader-timemachine", "archive")
edge = load("trader-timemachine", "edge")
FIVE = os.path.join(FIX, "trader-timemachine", "edge")
SYMS = ("INFY.NS", "TCS.NS", "^NSEI")


def quiet(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class Archive(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.root = os.path.join(self.d, "bars")
        self.charts = {s: edge.read_offline(FIVE, s) for s in SYMS}

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_a_session_is_saved_once_and_only_when_it_is_over(self):
        days = sorted(edge.sessions_of(self.charts["INFY.NS"], "INFY.NS"))
        last = days[-1]
        # at 15:00 on the last session's day that session is still open: everything before it is saved
        noon = datetime.datetime.combine(datetime.date.fromisoformat(last), datetime.time(15, 0))
        written, kept = archive.save(self.charts, "5m", self.root, noon)
        self.assertEqual(written, days[:-1])
        self.assertNotIn(last, archive.days_in(self.root, "5m"))
        after = noon.replace(hour=15, minute=31)
        written, kept = archive.save(self.charts, "5m", self.root, after)
        self.assertEqual((written, len(kept)), ([last], len(days) - 1))
        # files are never rewritten
        p = archive.day_path(self.root, "5m", days[0])
        before = os.path.getmtime(p)
        archive.save(self.charts, "5m", self.root, after)
        self.assertEqual(os.path.getmtime(p), before)

    def test_exported_bars_read_back_exactly(self):
        archive.save(self.charts, "5m", self.root, datetime.datetime(2026, 12, 1, 9, 0))
        out = os.path.join(self.d, "out")
        got = archive.export(self.root, "5m", out)
        self.assertEqual(sorted(got), sorted(SYMS))
        for s in SYMS:
            want = edge.sessions_of(self.charts[s], s)
            back = edge.sessions_of(edge.read_offline(out, s), s)
            self.assertEqual(back, want, s)
        # a date range
        days = archive.days_in(self.root, "5m")
        got = archive.export(self.root, "5m", os.path.join(self.d, "two"), days[0], days[1])
        self.assertEqual(len(edge.sessions_of(edge.read_offline(os.path.join(self.d, "two"), "INFY.NS"), "INFY.NS")), 2)

    def test_the_command_line(self):
        wl = os.path.join(self.d, "wl.txt")
        with open(wl, "w") as fh:
            fh.write("INFY.NS\nTCS.NS\n")
        code, out, err = quiet(archive.main, ["save", "--watchlist", wl, "--offline", FIVE, "--root", self.root,
                                              "--now", "2026-12-01 09:00"])
        self.assertEqual(code, 0, err)
        # the stocks run to 6 November, the Nifty 50 fixture to 9 November: 61 sessions in all
        self.assertIn("5m bars: 61 session(s) saved (2026-08-12 to 2026-11-09), 0 already archived", out)
        self.assertIn("4 of 4 symbols read", out)          # the watchlist plus Nifty and VIX
        code, out, _ = quiet(archive.main, ["status", "--root", self.root])
        self.assertIn("5m archive: 61 sessions, 2026-08-12 to 2026-11-09", out)


if __name__ == "__main__":
    unittest.main()
