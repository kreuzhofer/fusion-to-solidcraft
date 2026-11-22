#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare two translator runs design by design.

    python3 compare_runs.py OLD/rebuild-report.json NEW/rebuild-report.json [--names]

Prints the grades of both runs, how many designs match Fusion in each, and every design whose grade
changed (better or worse), plus designs that are in only one of the runs. Without `--names` the
designs are numbered instead of named, so the output can be shared without the hub's contents.
"""
import argparse
import json
import sys
from collections import Counter

ORDER = ["exact", "exact-with-frozen-steps", "imported-as-is", "partial-but-matching", "volume-mismatch", None]
MATCHING = {"exact", "exact-with-frozen-steps", "imported-as-is"}


def grades(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = {}
    for r in data.get("designs", data if isinstance(data, list) else []):
        f = r.get("fidelity")
        out[r.get("stem") or r.get("path") or r.get("design")] = f.get("grade") if isinstance(f, dict) else f
    return out


def rank(g):
    return ORDER.index(g) if g in ORDER else len(ORDER)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--names", action="store_true", help="print design names (private output)")
    a = ap.parse_args()
    old, new = grades(a.old), grades(a.new)

    co, cn = Counter(old.values()), Counter(new.values())
    print("{:<26} {:>6} {:>6}".format("grade", "old", "new"))
    for g in ORDER + sorted({g for g in set(co) | set(cn) if g not in ORDER}, key=str):
        if co.get(g) or cn.get(g):
            print("{:<26} {:>6} {:>6}".format(str(g), co.get(g, 0), cn.get(g, 0)))
    mo = sum(1 for g in old.values() if g in MATCHING)
    mn = sum(1 for g in new.values() if g in MATCHING)
    print("{:<26} {:>6} {:>6}".format("matching Fusion", mo, mn))
    print("{:<26} {:>6} {:>6}".format("designs", len(old), len(new)))

    label = (lambda k, i: k) if a.names else (lambda k, i: "design {}".format(i))
    keys = sorted(set(old) | set(new), key=str)
    better, worse, only = [], [], []
    for i, k in enumerate(keys, 1):
        if k not in old or k not in new:
            only.append("{}  only in the {} run".format(label(k, i), "new" if k not in old else "old"))
        elif old[k] != new[k]:
            (better if rank(new[k]) < rank(old[k]) else worse).append(
                "{}  {} -> {}".format(label(k, i), old[k], new[k]))
    for title, rows in (("better", better), ("worse", worse), ("in one run only", only)):
        if rows:
            print("\n{} ({}):".format(title, len(rows)))
            for r in rows:
                print("  " + r)
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
