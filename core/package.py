#!/usr/bin/env python3
"""Build standalone skill zips, for uploading a skill on its own (claude.ai, Settings, Capabilities, Skills).

Each zip holds one skill folder plus the shared tables it reads (tradercore.py, nse.json, brokers/), so it runs
without the rest of the repo. The skills that drive other skills (trader-broker's daily run, trader-scanner's
morning brief) still need the whole pack: install the plugin for those.

    python3 core/trader.py package                 # every skill, into dist/
    python3 core/trader.py package trader-risk --out /tmp/zips
"""

import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
SHARED = ("tradercore.py", "nse.json")


def skills():
    return sorted(d for d in os.listdir(CODE) if os.path.isfile(os.path.join(CODE, d, "SKILL.md")))


def build(skill, out):
    src = os.path.join(CODE, skill)
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        raise SystemExit("%s is not a skill folder" % skill)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, skill + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(base, f)
                z.write(full, os.path.join(skill, os.path.relpath(full, src)))
        for f in SHARED:
            z.write(os.path.join(HERE, f), os.path.join(skill, f))
        for f in sorted(os.listdir(os.path.join(HERE, "brokers"))):
            if f.endswith(".json"):
                z.write(os.path.join(HERE, "brokers", f), os.path.join(skill, "brokers", f))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trader.py package", description=__doc__.split("\n")[0])
    ap.add_argument("skills", nargs="*", help="default: every skill")
    ap.add_argument("--out", default=os.path.join(CODE, "dist"))
    a = ap.parse_args(argv)
    for s in a.skills or skills():
        print(build(s, a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
