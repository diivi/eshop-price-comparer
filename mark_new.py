#!/usr/bin/env python3
"""Write new.json: for each deals_*.csv, the NSUIDs that are listed now but weren't a day ago.

"A day ago" is the newest commit at least 20 hours old, so a late or manually re-run job
still compares against the previous day's list. A list with no version that old gets no
New tags, rather than every title being tagged.
"""

import csv
import glob
import io
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def nsuids(text):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return set()
    key = next(k for k in rows[0] if k.endswith("_nsuid"))  # the cheap country's column
    return {r[key] for r in rows}


def main():
    base = git("rev-list", "-1", "--before=20 hours ago", "HEAD").stdout.strip()
    since = git("show", "-s", "--format=%cI", base).stdout.strip() if base else None
    out = {}
    for path in sorted(glob.glob(os.path.join(HERE, "deals_*.csv"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8-sig") as f:
            now = nsuids(f.read())
        old = git("show", f"{base}:{name}") if base else None
        new = sorted(now - nsuids(old.stdout)) if old and old.returncode == 0 else []
        out[name] = {"since": since, "nsuids": new}
        print(f"{name}: {len(new)} new since {since}")
    with open(os.path.join(HERE, "new.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
