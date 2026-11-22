# SPDX-License-Identifier: Apache-2.0
# InventoryHub - what is in my Fusion hub, and what can be done with each design?
# (Crosslink, Fusion-to-SolidCraft migration; step 1 of the README)
#
# Lists every file in the active hub, opens each design once (invisible, never saved) and sorts it
# into the bucket the later steps use (rule in f2s_buckets.py, shared with ExtractTimeline):
#   parametric     a timeline without mesh steps: extracted and rebuilt with history
#   mesh-base      a converted mesh, then really modelled: history from the step after the mesh on
#   geometry-only  no history to keep (direct modelling, plain imports, meshes left): exact geometry
#   not-a-design   other files in the hub (STL, PDF...): not migrated
#   unreadable     Fusion could not open it
# plus flags (assembly, big, no real Fusion work), with the reason for each.
#
# Writes to OUT_DIR:
#   inventory.json          every file with its bucket, reason, flags and the facts behind them;
#                           ExtractTimeline reads it and opens the designs by ID, without walking the hub
#   inventory.csv           the same as a table
#   inventory-summary.txt   counts per bucket and the next step (also shown at the end)
#   lists/<bucket>.txt      the designs of each bucket, one path per line, for ExtractTimeline's
#                           SELECT = "list" (e.g. to extract only the mesh-base designs)
#
# Resumable: a design already read at the same version is not opened again (a newer rule only
# re-sorts the facts already read; a newer FACTS_VERSION opens them again). Read-only: it never
# saves, and it never closes a document that was open before it started or that has unsaved
# changes.
# Run from Fusion: Utilities > Add-Ins > Scripts and Add-Ins > InventoryHub > Run.

import adsk.core
import adsk.fusion
import csv
import importlib.util
import json
import os
import time
import traceback

# ---- settings -------------------------------------------------------------
OUT_DIR = os.path.expanduser("~/cad/fusion-inventory")
DEEP = True       # False: list only, without opening anything (fast, but no buckets)
ALL_HUBS = False  # True walks every hub, not only the active one
LIMIT = 0         # deep mode only: stop after this many newly opened designs (0 = no limit)
# ---------------------------------------------------------------------------

FORMAT = 2   # inventory.json layout; 2 = buckets, path keys and facts (21 Nov 2026)
FACTS_VERSION = 2   # bumped when read_facts reads something new; designs are opened again then


def load_buckets():
    """The shared bucket rule: next to this script (installed) or in fusion/common (toolkit)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "f2s_buckets.py"), os.path.join(here, "..", "common", "f2s_buckets.py")):
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("f2s_buckets", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("f2s_buckets.py is missing next to InventoryHub.py: run install.sh again")


def get(obj, name, default=None):
    try:
        v = getattr(obj, name)
        return default if v is None else v
    except Exception:
        return default


def walk(folder, parts, found, errors):
    try:
        files = folder.dataFiles
        for i in range(files.count):
            try:
                found.append((files.item(i), list(parts)))
            except Exception as e:
                errors.append("%s: file %d: %s" % ("/".join(parts), i, e))
        subs = folder.dataFolders
        for i in range(subs.count):
            sub = subs.item(i)
            walk(sub, parts + [get(sub, "name", "?")], found, errors)
    except Exception as e:
        errors.append("%s: %s" % ("/".join(parts), e))
    adsk.doEvents()


def list_record(f, parts, B):
    return {
        "id": get(f, "id"),
        # the key the later steps use: ExtractTimeline's output path without ".timeline.json"
        "path": "/".join([B.safe(p) for p in parts] + [B.safe(get(f, "name"))]),
        "hub": parts[0], "project": parts[1], "folder": "/".join(parts[2:]),
        "name": get(f, "name"), "ext": get(f, "fileExtension"),
        "version": get(f, "versionNumber"), "versions": get(f, "latestVersionNumber"),
        "created": get(f, "dateCreated"), "modified": get(f, "dateModified"),
        "has_children": get(f, "hasChildReferences"), "has_parents": get(f, "hasParentReferences"),
    }


def expand_groups(tl):
    """Open collapsed timeline groups so their steps are read (as ExtractTimeline does; nothing is saved)."""
    for _round in range(6):
        changed = False
        for i in range(tl.count):
            try:
                o = tl.item(i)
                if get(o, "isGroup") and get(o, "isCollapsed"):
                    o.isCollapsed = False
                    changed = True
            except Exception:
                continue
        if not changed:
            break
        adsk.doEvents()


def read_facts(design, B):
    """What the bucket rule needs, and the counts people like to see."""
    out = {"parametric": design.designType == adsk.fusion.DesignTypes.ParametricDesignType}
    types, unhealthy = {}, 0
    out["kinds"], out["names"] = [], []
    if out["parametric"]:
        try:
            tl = design.timeline
            expand_groups(tl)
            out["kinds"], out["names"] = B.timeline_kinds(tl)
            for i in range(tl.count):
                try:
                    if get(tl.item(i), "healthState", 0) not in (
                            0, adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState):
                        unhealthy += 1
                except Exception:
                    pass
        except Exception as e:
            out["timeline_note"] = str(e)[:80]
    for k in out["kinds"]:
        types[k or "Unreadable"] = types.get(k or "Unreadable", 0) + 1
    out["timeline_steps"] = len(out["kinds"])
    out["feature_types"] = types
    out["unhealthy_steps"] = unhealthy

    root = design.rootComponent
    comps = design.allComponents
    sketches = bodies = meshes = 0
    for i in range(comps.count):
        c = comps.item(i)
        sketches += get(get(c, "sketches"), "count", 0)
        bodies += get(get(c, "bRepBodies"), "count", 0)
        meshes += get(get(c, "meshBodies"), "count", 0)
    placed = get(get(root, "bRepBodies"), "count", 0)
    occs = get(root, "allOccurrences")
    for i in range(get(occs, "count", 0)):
        try:
            placed += occs.item(i).bRepBodies.count
        except Exception:
            pass
    out.update({
        "facts_version": FACTS_VERSION, "bodies_placed": placed,
        "components": comps.count, "occurrences": get(get(root, "allOccurrences"), "count", 0),
        "sketches": sketches, "bodies": bodies, "meshes_left": meshes,
        "joints": get(get(root, "joints"), "count", 0) + get(get(root, "asBuiltJoints"), "count", 0),
        "user_parameters": get(get(design, "userParameters"), "count", 0),
    })
    return out


def sort_record(r, B):
    """Bucket, reason and flags from the facts read so far."""
    facts = dict(r.get("facts") or {}, ext=r.get("ext"), linked=r.get("has_children"), error=r.get("error"))
    if r.get("ext") == "f3d" and not r.get("facts") and not r.get("error"):
        for k in ("bucket", "reason", "real_work", "flags", "what_happens", "mesh_base"):
            r.pop(k, None)
        r["bucket"] = None
        r["reason"] = "not opened yet (DEEP = False, LIMIT reached, or cancelled)"
        return
    c = B.classify(facts)
    r.pop("mesh_base", None)
    r.update(c)
    r["rule"] = B.RULE_VERSION


def write(records, meta, B):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(OUT_DIR, "inventory.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(meta, designs=records), fh, indent=1, default=str)
    os.replace(tmp, os.path.join(OUT_DIR, "inventory.json"))
    cols = ["path", "bucket", "reason", "flags", "real_work", "ext", "version", "versions", "modified",
            "has_children", "timeline_steps", "sketches", "user_parameters", "bodies", "bodies_placed", "meshes_left",
            "components", "occurrences", "joints", "unhealthy_steps", "feature_types", "error"]
    with open(os.path.join(OUT_DIR, "inventory.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in records:
            d = dict(r, **{k: v for k, v in (r.get("facts") or {}).items() if k not in ("kinds", "names")})
            ft = d.get("feature_types")
            if isinstance(ft, dict):
                d["feature_types"] = " ".join("%s:%d" % kv for kv in sorted(ft.items(), key=lambda kv: -kv[1]))
            d["flags"] = "; ".join(d.get("flags") or [])
            w.writerow([d.get(c, "") if d.get(c) is not None else "" for c in cols])
    lists = os.path.join(OUT_DIR, "lists")
    os.makedirs(lists, exist_ok=True)
    for b in B.BUCKETS:
        paths = [r["path"] for r in records if r.get("bucket") == b and b != "not-a-design"]
        p = os.path.join(lists, b + ".txt")
        if paths:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(paths) + "\n")
        elif os.path.exists(p):
            os.remove(p)
    real = [r["path"] for r in records if r.get("real_work")]
    with open(os.path.join(lists, "real-work.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(real) + ("\n" if real else ""))


def summary(records, meta, B, started, opened, failed):
    sorted_ = [r for r in records if r.get("bucket")]
    lines = B.summary_lines(sorted_)
    waiting = sum(1 for r in records if r.get("ext") == "f3d" and not r.get("bucket"))
    if waiting:
        lines[1:1] = ["{} designs not opened yet: run InventoryHub again (DEEP = True) to sort them.".format(waiting)]
    el = int(time.time() - started)
    lines += ["", "opened now: {}, could not open: {}, listing errors: {}, time: {}m {}s".format(
        opened, failed, len(meta.get("walk_errors") or []), el // 60, el % 60),
        "Files: {} (inventory.json, inventory.csv, inventory-summary.txt, lists/)".format(OUT_DIR)]
    with open(os.path.join(OUT_DIR, "inventory-summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return lines


def status(n, total, r, started):
    """One line a watcher outside Fusion can read: where the deep pass is right now."""
    try:
        with open(os.path.join(OUT_DIR, "status.txt"), "w", encoding="utf-8") as fh:
            fh.write("%s  %d / %d  %ds elapsed  opening: %s / %s\n" % (
                time.strftime("%H:%M:%S"), n, total, int(time.time() - started), r["project"], r["name"]))
    except Exception:
        pass


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        B = load_buckets()
        started = time.time()
        hubs = []
        if ALL_HUBS:
            for i in range(app.data.dataHubs.count):
                hubs.append(app.data.dataHubs.item(i))
        else:
            hubs.append(app.data.activeHub)
        hubs = [h for h in hubs if h is not None]
        if not hubs:
            ui.messageBox("No hub available. Sign in and try again.")
            return

        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        found, errors = [], []
        for hub in hubs:
            if len(hubs) > 1:
                try:
                    app.data.activeHub = hub
                except Exception:
                    pass
            projects = app.data.dataProjects
            progress.show("Inventory: listing " + get(hub, "name", "hub"), "project %v / %m", 0, max(projects.count, 1), 0)
            for p in range(projects.count):
                if progress.wasCancelled:
                    break
                proj = projects.item(p)
                progress.progressValue = p
                progress.message = "project %v / %m\n" + get(proj, "name", "?")[:60]
                adsk.doEvents()
                try:
                    walk(proj.rootFolder, [get(hub, "name", "hub"), get(proj, "name", "?")], found, errors)
                except Exception as e:
                    errors.append("%s: %s" % (get(proj, "name", "?"), e))
            progress.hide()

        # keep what was read before: a design at the same version is not opened again
        old = {}
        try:
            with open(os.path.join(OUT_DIR, "inventory.json"), encoding="utf-8") as fh:
                for r in json.load(fh).get("designs", []):
                    if r.get("facts") or r.get("error"):
                        old[r["id"]] = r
        except Exception:
            pass

        records = []
        for f, parts in found:
            r = list_record(f, parts, B)
            prev = old.get(r["id"])
            if prev and prev.get("version") == r["version"]:
                if (prev.get("facts") or {}).get("facts_version") == FACTS_VERSION:
                    r["facts"] = prev["facts"]
                elif prev.get("error"):
                    r["error"] = prev["error"]
            sort_record(r, B)
            records.append(r)
        meta = {"format": FORMAT, "rule": B.RULE_VERSION, "listed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "hubs": [get(h, "name", "hub") for h in hubs], "deep": DEEP, "walk_errors": errors}
        write(records, meta, B)

        opened = failed = 0
        if DEEP:
            pre_open = set()
            for k in range(app.documents.count):
                try:
                    pre_open.add(app.documents.item(k).name)
                except Exception:
                    pass
            todo = [(f, r) for (f, _), r in zip(found, records)
                    if r["ext"] == "f3d" and not r.get("facts")]
            if LIMIT:
                todo = todo[:LIMIT]
            if todo:
                progress.show("Inventory: opening designs", "%v / %m   %p%", 0, len(todo), 0)
            for n, (f, r) in enumerate(todo):
                if progress.wasCancelled:
                    break
                progress.progressValue = n
                progress.message = "%v / %m   %p%\n" + (r["name"] or "")[:60]
                adsk.doEvents()
                status(n, len(todo), r, started)
                doc = None
                r.pop("error", None)
                try:
                    doc = app.documents.open(f, False)
                    design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
                    if design is None:
                        raise RuntimeError("no Design product")
                    r["facts"] = read_facts(design, B)
                    opened += 1
                except Exception as e:
                    r["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
                    failed += 1
                finally:
                    if doc is not None:
                        try:
                            if doc.name not in pre_open and get(doc, "isSaved") is not False:
                                doc.close(False)
                        except Exception:
                            pass
                    adsk.doEvents()
                sort_record(r, B)
                if n % 5 == 4:
                    write(records, meta, B)
            if todo:
                progress.hide()
            write(records, meta, B)
            status(len(todo), len(todo), {"project": "-", "name": "finished"}, started)

        lines = summary(records, meta, B, started, opened, failed)
        ui.messageBox("Inventory finished.\n\n" + "\n".join(lines))
    except Exception:
        if ui:
            ui.messageBox("InventoryHub failed:\n{}".format(traceback.format_exc()))
