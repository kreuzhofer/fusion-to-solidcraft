#!/usr/bin/env python3
"""Set up the next extraction pass from what the translator asked for.

After a translator run, `state-wanted.json` (next to `rebuild-report.json`) lists for each design
the steps whose state in Fusion would help: steps the translator could not reproduce. Many of
those states are often on disk already (an earlier pass wrote them, but the translator still could
not pair them), and fetching them again would change nothing. This script:

  1. keeps only the designs with at least one state that is not on disk yet,
  2. writes `_state_wanted.json` for those designs: the new steps plus every state already on disk
     (ExtractTimeline only keeps states that are on the wanted list),
  3. writes `_reextract.txt` with the same designs, one hub path per line (ExtractTimeline's
     SELECT = "list"),
  4. keeps the previous two files with a time stamp, and says what the pass will do.

Then run ExtractTimeline (or fusion/nightloop.py) and translate again. Nothing in Fusion is touched.

    python3 next_pass.py                      # defaults below
    python3 next_pass.py --dry-run            # only say what it would write
"""
import argparse
import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wanted", default=os.path.join(HOME, "cad", "fusion-rebuild", "state-wanted.json"),
                    help="the translator's state-wanted.json")
    ap.add_argument("--timelines", default=os.path.join(HOME, "cad", "fusion-timeline"),
                    help="ExtractTimeline's output folder")
    ap.add_argument("--work", default=os.path.join(HOME, "cad", "work"),
                    help="folder of _state_wanted.json and _reextract.txt")
    ap.add_argument("--per-visit", type=int, default=40,
                    help="ExtractTimeline's STATE_CHUNK, for the estimate only")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    with open(a.wanted, encoding="utf-8") as fh:
        asked = json.load(fh)
    plan, already, missing_files = {}, 0, []
    for key, steps in sorted(asked.items()):
        tl = os.path.join(a.timelines, key + ".timeline.json")
        try:
            with open(tl, encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError:
            missing_files.append(key)
            continue
        have = {s.get("index") for s in doc.get("timeline") or [] if s.get("state_step")}
        new = sorted(set(int(i) for i in steps) - have)
        already += len(set(int(i) for i in steps) & have)
        if new:
            plan[key] = {"new": new, "list": sorted(have | set(int(i) for i in steps))}

    print("translator asked for {} states in {} designs".format(
        sum(len(v) for v in asked.values()), len(asked)))
    print("  already on disk (would change nothing): {}".format(already))
    if missing_files:
        print("  no timeline found for {} designs, left out: {}".format(len(missing_files), ", ".join(missing_files[:5])))
    if not plan:
        print("nothing new to fetch from Fusion: no further pass needed.")
        return 0
    rounds = max(-(-len(v["new"]) // a.per_visit) for v in plan.values())
    print("next pass: {} new states in {} designs (about {} visit(s) of the biggest design)".format(
        sum(len(v["new"]) for v in plan.values()), len(plan), rounds))
    for key, v in plan.items():
        print("  {:4d} new, from step {:<5d} {}".format(len(v["new"]), v["new"][0], key))
    if a.dry_run:
        return 0

    os.makedirs(a.work, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wanted_path = os.path.join(a.work, "_state_wanted.json")
    list_path = os.path.join(a.work, "_reextract.txt")
    for p in (wanted_path, list_path):
        if os.path.exists(p):
            root, ext = os.path.splitext(p)
            shutil.copy2(p, "{}-before-{}{}".format(root, stamp, ext))
    with open(wanted_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({k: v["list"] for k, v in plan.items()}, fh, indent=1)
    os.replace(wanted_path + ".tmp", wanted_path)
    with open(list_path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(plan) + "\n")
    os.replace(list_path + ".tmp", list_path)
    print("wrote {} and {} (previous versions kept with -before-{})".format(wanted_path, list_path, stamp))
    print("next: run ExtractTimeline with SELECT = \"list\" (or fusion/nightloop.py), then translate again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
