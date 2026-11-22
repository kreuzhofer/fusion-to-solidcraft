# SPDX-License-Identifier: Apache-2.0
# f2s_buckets - the one place that decides what can be done with a design (Crosslink, 21 Nov 2026).
#
# InventoryHub sorts every design with it before anything is extracted; ExtractTimeline uses the same
# rule when it reads a design, so the inventory's promise and the extractor's decision cannot drift
# apart. Plain Python, no Fusion API: the facts are read by the caller and passed in, which also lets
# the rule be tested outside Fusion against timelines already on disk (see test_buckets.py).
#
# install.sh copies this file next to each Fusion script; the scripts load it from their own folder,
# or from fusion/common when they run straight from the toolkit.

import re

RULE_VERSION = 1   # bumped when a bucket can change for the same design; InventoryHub re-reads designs then

# Timeline steps that change no geometry by themselves. A mesh step followed only by these is
# not "modelled after the mesh".
PASSIVE = {"ConstructionPlane", "ConstructionAxis", "ConstructionPoint", "Sketch", "Occurrence",
           "JointOrigin", "Canvas", "Decal", "", "TimelineGroup", "NoneType"}

BIG_BODIES = 200   # above this many bodies in place, extraction is slow and needs a lot of memory (flag only)

BUCKETS = ("parametric", "mesh-base", "geometry-only", "not-a-design", "unreadable")

WHAT_HAPPENS = {
    "parametric": "ExtractTimeline reads its history; the translator rebuilds it step by step, with history",
    "mesh-base": "ExtractTimeline reads it from the step after the last mesh step; the translator starts "
                 "from that body and rebuilds the modelling after it",
    "geometry-only": "no history to keep: its exact final shape is imported as it is (STEP and meshes)",
    "not-a-design": "not a Fusion design; not part of the migration (download it from the Data Panel if "
                    "you need it)",
    "unreadable": "Fusion could not open it; open it by hand once, then run the inventory again",
}


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name):
    """A hub, project, folder or design name as it appears in the output paths (same as ExtractTimeline)."""
    cleaned = _INVALID.sub("_", (name or "untitled")).strip().rstrip(".")
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *("COM{}".format(i) for i in range(1, 10)),
        *("LPT{}".format(i) for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:120] or "untitled"


def timeline_kinds(tl):
    """(kinds, names) of a Fusion timeline, read the way the mesh-base rule expects.

    Expand the timeline's groups first (ExtractTimeline.expand_groups). Group headers come back as
    "TimelineGroup", unreadable steps as "".
    """
    kinds, names = [], []
    for i in range(tl.count):
        try:
            item = tl.item(i)
        except Exception:
            kinds.append("")
            names.append("")
            continue
        try:
            names.append(item.name or "")
        except Exception:
            names.append("")
        try:
            if item.isGroup:
                kinds.append("TimelineGroup")
                continue
        except Exception:
            pass
        try:
            kinds.append(type(item.entity).__name__)
        except Exception:
            kinds.append("")
    return kinds, names


def is_mesh_step(kind):
    return "Mesh" in (kind or "")


def is_work(kind):
    """A step that makes or changes geometry: neither passive nor a mesh operation."""
    return bool(kind) and kind not in PASSIVE and not is_mesh_step(kind)


def is_modelling(kind, name=""):
    """Work someone did in Fusion, for counting real work (not for the mesh-base rule).

    A Base Feature holds imported or directly edited bodies, and some mesh operations come back
    from the API as a plain "Feature" (named "Base Mesh Feature", "CopyPasteMeshes"...). Neither
    is modelling in the timeline.
    """
    if not is_work(kind) or kind == "BaseFeature":
        return False
    return not (kind == "Feature" and "mesh" in (name or "").lower())


def mesh_base(kinds, meshes_left):
    """Where a design with mesh steps can be read from.

    kinds: the timeline's entity type names in order, groups expanded (type(entity).__name__).
    meshes_left: number of mesh bodies in the finished model, or None if unknown.

    -> {"index": i, "entity": kind, "steps_after": n} when the design starts from a converted mesh
       and is really modelled after it, else None. Second value: why not (None when there are no
       mesh steps at all).
    """
    last = max((i for i, k in enumerate(kinds) if is_mesh_step(k)), default=None)
    if last is None:
        return None, None
    work = [k for k in kinds[last + 1:] if is_work(k)]
    if not work:
        return None, "nothing is modelled after the last mesh step ({})".format(kinds[last])
    if meshes_left is None:
        return None, "could not count the mesh bodies at the end"
    if meshes_left:
        return None, "{} mesh bod{} left in the finished model".format(
            meshes_left, "y" if meshes_left == 1 else "ies")
    return {"index": last, "entity": kinds[last], "steps_after": len(work)}, None


def classify(facts):
    """Sort one design into a bucket.

    facts (all optional except ext):
      ext            file extension ("f3d" for a Fusion design)
      error          why it could not be opened
      parametric     False for a direct-modelling design
      kinds          timeline entity type names, groups expanded
      names          the steps' names, same order (optional; sharpens "real work")
      meshes_left    mesh bodies in the finished model
      bodies         solid and surface bodies in the finished model (each component once)
      bodies_placed  the same counted once per placement, as ExtractTimeline measures them
      occurrences    component occurrences
      linked         True when it references other designs (has child references)

    -> {"bucket", "reason", "real_work", "flags": [...], "what_happens", "mesh_base"?}
    """
    out = {"flags": []}
    if (facts.get("ext") or "").lower() != "f3d":
        out.update(bucket="not-a-design", reason="a .{} file".format(facts.get("ext") or "?"),
                   real_work=False)
    elif facts.get("error"):
        out.update(bucket="unreadable", reason=str(facts["error"])[:160], real_work=None)
    else:
        kinds = list(facts.get("kinds") or [])
        names = list(facts.get("names") or []) + [""] * len(kinds)
        worked = [k for k, n in zip(kinds, names) if is_modelling(k, n)]
        meshes = [k for k in kinds if is_mesh_step(k)]
        if facts.get("parametric") is False or not kinds:
            out.update(bucket="geometry-only", real_work=False,
                       reason="direct modelling design: Fusion keeps no history" if facts.get("parametric") is False
                       else "empty timeline")
        elif not meshes:
            if worked:
                out.update(bucket="parametric", real_work=True,
                           reason="{} modelling steps, no mesh steps".format(len(worked)))
            else:
                out.update(bucket="geometry-only", real_work=False,
                           reason="only imported bodies, sketches or construction geometry, nothing "
                                  "modelled in the timeline")
        else:
            base, why = mesh_base(kinds, facts.get("meshes_left"))
            if base:
                out.update(bucket="mesh-base", real_work=True, mesh_base=base,
                           reason="starts from a converted mesh (step {}, {}); {} modelling steps after it".format(
                               base["index"] + 1, base["entity"], base["steps_after"]))
            else:
                out.update(bucket="geometry-only", real_work=bool(worked),
                           reason="mesh steps: " + why)
    if out["bucket"] not in ("not-a-design", "unreadable"):
        if facts.get("linked"):
            out["flags"].append("assembly: uses other designs")
        elif (facts.get("occurrences") or 0) > 0:
            out["flags"].append("assembly: {} components".format(facts["occurrences"]))
        placed = facts.get("bodies_placed")
        n = placed if placed is not None else (facts.get("bodies") or 0)
        if n > BIG_BODIES:
            out["flags"].append("big: {} bodies{}".format(n, " in place" if placed is not None else ""))
        if out["real_work"] is False and out["bucket"] == "geometry-only" and facts.get("kinds"):
            out["flags"].append("no real Fusion work: only imported or converted")
    out["what_happens"] = WHAT_HAPPENS[out["bucket"]]
    return out


def summary_lines(records):
    """The text a user reads at the end of the inventory: counts per bucket and the next step.

    records: dicts with at least "bucket", "real_work", "flags".
    """
    from collections import Counter
    n = Counter(r.get("bucket") for r in records)
    designs = sum(v for k, v in n.items() if k != "not-a-design")
    real = sum(1 for r in records if r.get("real_work"))
    lines = ["{} Fusion designs, {} other files.".format(designs, n.get("not-a-design", 0)), ""]
    for b in BUCKETS:
        if n.get(b):
            lines.append("{:>5}  {:<14} {}".format(n[b], b, WHAT_HAPPENS[b]))
    lines += ["", "{} of the {} designs have real modelling work in Fusion; those are the ones whose "
                  "history is worth moving.".format(real, designs)]
    asm = sum(1 for r in records if any(f.startswith("assembly") for f in r.get("flags") or []))
    big = sum(1 for r in records if any(f.startswith("big") for f in r.get("flags") or []))
    if asm or big:
        lines.append("{} assemblies, {} of them big (over {} bodies: slower, more memory).".format(
            asm, big, BIG_BODIES))
    if n.get("unreadable"):
        lines.append("{} designs could not be opened: open them in Fusion once, then run InventoryHub again."
                     .format(n["unreadable"]))
    lines += ["", "Next: run ExportAllDesigns (step 2, the backup), then ExtractTimeline (step 3).",
              "ExtractTimeline reads this inventory and goes straight to the designs, without walking the hub."]
    return lines
