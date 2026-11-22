# SPDX-License-Identifier: Apache-2.0
# ExportAllDesigns - back up every Fusion design as STEP and F3D, again and again.
#
# Walks every project and folder in your hub(s) and writes:
#   <OUT>/<Hub>/<Project>/<folder path>/<Design>.step   geometry, portable B-rep (what SolidCraft imports)
#   <OUT>/<Hub>/<Project>/<folder path>/<Design>.f3d    Fusion archive, insurance against losing cloud access
#   <OUT>/manifest.json                                 one record per design, for reconciling the migration
#   <OUT>/backup-log.txt                                one line per run: what was new, changed, moved, gone
#
# Crosslink 2026-11-22: a backup tool you can run as often as you like, on its own (it needs nothing
# from the other steps). Each run exports only what is new or has a new version in Fusion, which it
# can tell without opening the design; everything else is skipped in seconds. Nothing is ever
# deleted: the export of an older version moves to <OUT>/_previous/ (KEEP_PREVIOUS), a design that
# was moved or renamed in the hub has its files moved along, and a design deleted from the hub keeps
# its files and is marked `gone_from_hub` in the manifest. Files are written under a temporary name
# first, so a run that is stopped never leaves a half-written file that looks finished.
# Run from Fusion: Utilities > ADD-INS > Scripts and Add-Ins > ExportAllDesigns > Run.

import adsk.core
import adsk.fusion
import traceback
import json
import os
import re
import time

# ---- settings -------------------------------------------------------------
OUT_DIR = os.path.expanduser("~/cad/fusion-export")
EXPORT_STEP = True
EXPORT_F3D = True
ALL_HUBS = False  # True walks every hub you belong to, not just the active one
KEEP_PREVIOUS = True  # keep the export of an older version in <OUT>/_previous/ as <Design>.v<N>.step/.f3d
# ---------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name):
    """Make a Fusion name usable as one path segment on Windows and mac."""
    cleaned = _INVALID.sub("_", (name or "untitled")).strip().rstrip(".")
    # Reserved device names on Windows would make an unopenable file.
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:120] or "untitled"


def get(obj, name):
    """Crosslink fix 2026-11-12: upstream calls get() here without defining it, so every body raised
    NameError inside the try below and the manifest recorded 0 bodies and 0 volume."""
    try:
        return getattr(obj, name)
    except Exception:
        return None


def measure(design):
    """Fusion's own volume/area/body count, so the import can be checked against it later.

    Fusion's API works in centimetres; the rest of the migration is in millimetres.
    Counts occurrence bodies too, because that is what a STEP export actually writes.
    """
    bodies = []
    root = design.rootComponent
    for i in range(root.bRepBodies.count):
        bodies.append(root.bRepBodies.item(i))
    for occ in root.allOccurrences:
        for i in range(occ.bRepBodies.count):
            bodies.append(occ.bRepBodies.item(i))

    volume_cm3 = 0.0
    area_cm2 = 0.0
    counted = 0
    hidden = 0
    for b in bodies:
        try:
            if not b.isSolid:
                continue
            # A STEP export writes the visible bodies, so measuring the hidden ones too makes
            # Fusion look like it holds more material than it exported and turns a correct
            # import into a reported mismatch. Count what will actually be written.
            if get(b, "isVisible") is False or get(b, "isLightBulbOn") is False:
                hidden += 1
                continue
            props = b.physicalProperties
            volume_cm3 += props.volume
            area_cm2 += props.area
            counted += 1
        except Exception:
            pass

    return {
        "bodies": len(bodies),
        "hidden_bodies": hidden,
        "solid_bodies": counted,
        "volume_mm3": volume_cm3 * 1000.0,
        "area_mm2": area_cm2 * 100.0,
    }


def collect(folder, rel_parts, found):
    """Depth-first walk of a DataFolder, collecting every Fusion design."""
    for i in range(folder.dataFiles.count):
        f = folder.dataFiles.item(i)
        try:
            if f.fileExtension == "f3d":
                found.append((f, list(rel_parts)))
        except Exception:
            pass
    for i in range(folder.dataFolders.count):
        sub = folder.dataFolders.item(i)
        collect(sub, rel_parts + [safe(sub.name)], found)


def wanted_exts():
    return [e for e, on in (("step", EXPORT_STEP), ("f3d", EXPORT_F3D)) if on]


def base_of(rec):
    """The design's file path under OUT_DIR without extension, as the manifest has it."""
    for k in ("f3d", "step"):
        if rec and rec.get(k):
            return os.path.splitext(rec[k].replace("\\", "/"))[0]
    return None


def assign_bases(designs, records):
    """A file path for every design (index -> path under OUT_DIR without extension).

    Two designs can share a name inside one folder, and then they would share an output path; the
    second one gets " (2)" and so on. A design keeps the path it had in an earlier run as long as
    it is still in the same folder under the same name, so the numbering does not shift between
    runs when the hub lists designs in another order.
    """
    taken, out = set(), {}
    for i, (data_file, parts) in enumerate(designs):
        rec = records.get(data_file.id)
        old = base_of(rec)
        if old and old.lower() not in taken and \
                os.path.dirname(old) == "/".join(parts) and \
                os.path.basename(old).lower().startswith(safe(data_file.name).lower()):
            out[i] = old
            taken.add(old.lower())
    for i, (data_file, parts) in enumerate(designs):
        if i in out:
            continue
        stem, n = "/".join(parts + [safe(data_file.name)]), 1
        cand = stem
        while cand.lower() in taken:
            n += 1
            cand = "%s (%d)" % (stem, n)
        out[i] = cand
        taken.add(cand.lower())
    return out


def decide(data_file, rel_base, rec, out_dir):
    """What a design needs: "unchanged", "changed" or "new" (run() has moved the files of moved
    and renamed designs to their new place before).

    Decided without opening the design: the version number and the modification date come with the
    hub listing. A record from an older run without a version number counts as current when its
    files are newer than the design's last change.
    """
    exts = wanted_exts()
    version = get(data_file, "versionNumber")
    old = base_of(rec)
    if not rec or not old:
        return "new"
    old_files = [os.path.join(out_dir, old + "." + e) for e in exts]
    if not all(os.path.exists(f) for f in old_files):
        return "new"
    if rec.get("version") is not None:
        same = rec["version"] == version
    else:
        modified = get(data_file, "dateModified") or 0
        same = all(os.path.getmtime(f) >= modified for f in old_files)
    if not same:
        return "changed"
    return "unchanged" if old == rel_base else "new"


def move_files(src_base, dst_base, out_dir, exts=None):
    """Move a design's exports (and nothing else) from one path under OUT_DIR to another."""
    for e in exts or ("step", "f3d"):
        src = os.path.join(out_dir, src_base + "." + e)
        dst = os.path.join(out_dir, dst_base + "." + e)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)


def keep_previous(rec, out_dir):
    """Move the export of the version before into _previous/, named with its version number."""
    old = base_of(rec)
    if not old:
        return None
    tag = "v%s" % rec["version"] if rec.get("version") is not None else time.strftime("%Y-%m-%d")
    dst = "_previous/%s.%s" % (old, tag)
    move_files(old, dst, out_dir)
    return dst


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Fusion versions disagree about how (and whether) they expose a sign-in flag, so ask only
        # if this build has one, and otherwise let the hub enumeration below be the real test.
        signed_in = getattr(app.data, "isSignedIn", None)
        if signed_in is False:
            ui.messageBox("Not signed in to Autodesk. Sign in, then run this again.")
            return

        # ---- enumerate every design first, so we can show real progress ----
        try:
            hubs = []
            if ALL_HUBS:
                for i in range(app.data.dataHubs.count):
                    hubs.append(app.data.dataHubs.item(i))
            else:
                hubs.append(app.data.activeHub)
            hubs = [h for h in hubs if h is not None]
        except Exception as e:
            ui.messageBox(
                "Could not reach your Autodesk hub ({}).\n\n"
                "Sign in to Fusion and make sure you are online, then run this again.".format(e)
            )
            return

        if not hubs:
            ui.messageBox(
                "No Autodesk hub available. Sign in to Fusion and make sure you are online, "
                "then run this again."
            )
            return

        designs = []  # (dataFile, [path parts])
        for hub in hubs:
            if len(hubs) > 1:
                # Only switch when there is something to switch to; the setter is not writable
                # on every build, and the single-hub case is already active.
                try:
                    app.data.activeHub = hub
                except Exception:
                    pass
            adsk.doEvents()
            hub_name = safe(hub.name)
            projects = app.data.dataProjects
            for p in range(projects.count):
                proj = projects.item(p)
                try:
                    root = proj.rootFolder
                except Exception:
                    continue
                collect(root, [hub_name, safe(proj.name)], designs)

        if not designs:
            ui.messageBox("No Fusion designs found in your hub.")
            return

        os.makedirs(OUT_DIR, exist_ok=True)
        manifest_path = os.path.join(OUT_DIR, "manifest.json")

        # The records of earlier runs: what was exported, at which version, and where.
        records = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    for r in json.load(fh).get("designs", []):
                        records[r["id"]] = r
            except Exception:
                records = {}

        bases = assign_bases(designs, records)
        total = len(designs)
        count = {"new": 0, "changed": 0, "moved": 0, "unchanged": 0, "failed": 0}
        changed_names = []

        # Designs moved or renamed in the hub: their files follow them before anything is
        # exported, so a new design that takes over an old name cannot be written over the files
        # of the design that had it. A move whose target is taken is left alone (the design is
        # then exported again at its new place; nothing is deleted).
        todo = [i for i, (f, _p) in enumerate(designs)
                if base_of(records.get(f.id)) not in (None, bases[i])]
        while todo:
            left = []
            for i in todo:
                data_file, parts = designs[i]
                prev, new = records[data_file.id], bases[i]
                old = base_of(prev)
                olds = [os.path.join(OUT_DIR, old + "." + e) for e in ("step", "f3d")]
                news = [os.path.join(OUT_DIR, new + "." + e) for e in ("step", "f3d")]
                if not any(os.path.exists(f) for f in olds):
                    continue
                if any(os.path.exists(f) for f in news):
                    left.append(i)
                    continue
                move_files(old, new, OUT_DIR)
                prev = dict(prev, moved_from=old)
                for e in ("step", "f3d"):
                    if prev.get(e):
                        prev[e] = new + "." + e
                records[data_file.id] = prev
                count["moved"] += 1
                changed_names.append("moved: {} -> {}".format(old.split("/", 1)[-1], new.split("/", 1)[-1]))
            if len(left) == len(todo):
                # designs that swapped names: their old files go to _previous/ and they are
                # exported again, rather than one being written over the other
                for i in left:
                    data_file, _parts = designs[i]
                    prev = records[data_file.id]
                    kept = "_previous/%s.%s-moved" % (base_of(prev), time.strftime("%Y-%m-%d"))
                    move_files(base_of(prev), kept, OUT_DIR)
                    records[data_file.id] = dict(prev, step=None, f3d=None,
                                                 previous=list(prev.get("previous") or []) + [kept])
                break
            todo = left
        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show("Backing up Fusion designs", "%v / %m   %p%", 0, total, 0)

        started = time.time()
        cancelled = False

        def save():
            # Written as we go, so a crash or a cancel still leaves a usable manifest.
            write_json(manifest_path, {
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "out_dir": OUT_DIR,
                "total_found": total,
                "designs": list(records.values()),
            })

        # Crosslink: documents.open() on an already-open design returns the SAME document, and
        # close(False) would then shut the owner's tab and drop unsaved work. Remember what was
        # open before this run and never close those.
        pre_open = set()
        try:
            for d in app.documents:
                try:
                    pre_open.add(d.name)
                except Exception:
                    pass
        except Exception:
            pass

        def close_if_ours(d):
            try:
                # 2026-11-14: only the pre-open check. Fusion flags many old designs as modified
                # the moment they are opened (19 of 215 on the first run), and leaving those open
                # filled the session and the crash-recovery list. A document this script opened
                # has no edits worth keeping; nothing is ever saved.
                if d.name in pre_open:
                    return
            except Exception:
                return
            try:
                d.close(False)
            except Exception:
                pass

        exts = wanted_exts()
        for idx, (data_file, parts) in enumerate(designs):
            if progress.wasCancelled:
                cancelled = True
                break
            progress.progressValue = idx
            progress.message = "%v / %m   %p%\n" + data_file.name[:60]
            adsk.doEvents()

            rel_base = bases[idx]
            prev = records.get(data_file.id)
            state = decide(data_file, rel_base, prev, OUT_DIR)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")

            if state == "unchanged":
                rec = dict(prev)
                rec.update({"name": data_file.name, "path": "/".join(parts),
                            "version": get(data_file, "versionNumber"), "checked_at": now})
                for e in ("step", "f3d"):
                    rec[e] = rel_base + "." + e if e in exts else rec.get(e)
                rec.pop("gone_from_hub", None)
                rec.pop("skipped", None)
                records[data_file.id] = rec
                count["unchanged"] += 1
                continue

            rec = {
                "id": data_file.id,
                "name": data_file.name,
                "path": "/".join(parts),
                "version": None,
                "step": None,
                "f3d": None,
                "measure": None,
                "error": None,
                "checked_at": now,
            }

            doc = None
            try:
                # Opening invisibly is much faster, but some builds will not export from a
                # hidden document -- fall back to a visible one rather than lose the design.
                design = None
                for visible in (False, True):
                    try:
                        doc = app.documents.open(data_file, visible)
                    except Exception:
                        doc = None
                    if doc is None:
                        continue
                    adsk.doEvents()
                    design = adsk.fusion.Design.cast(
                        doc.products.itemByProductType("DesignProductType")
                    )
                    if design is not None:
                        break
                    close_if_ours(doc)
                    doc = None

                if doc is None:
                    raise RuntimeError("document would not open")
                if design is None:
                    raise RuntimeError("not a Design document")

                rec["version"] = data_file.versionNumber
                try:
                    rec["measure"] = measure(design)
                except Exception as e:
                    rec["measure"] = {"error": str(e)}

                # Written under a temporary name and renamed when complete, so a stopped run
                # never leaves a half-written file at the real name (which the next run would
                # take for a finished export).
                em = design.exportManager
                written = {}
                for e in exts:
                    tmp = os.path.join(OUT_DIR, rel_base + ".~partial." + e)
                    os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    if e == "step":
                        em.execute(em.createSTEPExportOptions(tmp))
                    else:
                        em.execute(em.createFusionArchiveExportOptions(tmp))
                    if not os.path.exists(tmp):
                        raise RuntimeError("Fusion wrote no .%s file" % e)
                    written[e] = tmp

                if prev and prev.get("previous"):
                    rec["previous"] = list(prev["previous"])
                if state == "changed" and KEEP_PREVIOUS:
                    rec["previous"] = rec.get("previous", []) + [keep_previous(prev, OUT_DIR)]
                if prev and prev.get("moved_from"):
                    rec["moved_from"] = prev["moved_from"]
                for e, tmp in written.items():
                    os.replace(tmp, os.path.join(OUT_DIR, rel_base + "." + e))
                    rec[e] = rel_base + "." + e
                rec["exported_at"] = now
                rec["checked_at"] = now
                if state == "new":
                    changed_names.append("new: " + "/".join(parts[1:] + [data_file.name]))
                if state == "changed":
                    changed_names.append("v{} -> v{}: {}".format(
                        prev.get("version") if prev.get("version") is not None else "?",
                        rec["version"], "/".join(parts[2:] + [data_file.name])))
                count[state] += 1
            except Exception as e:
                count["failed"] += 1
                changed_names.append("failed: {}: {}".format(data_file.name, str(e)[:100]))
                if prev:
                    # keep what the last good export recorded; the files are still there
                    rec = dict(prev, last_error="{}: {}".format(type(e).__name__, e), checked_at=now)
                else:
                    rec["error"] = "{}: {}".format(type(e).__name__, e)
                for e2 in exts:
                    try:
                        os.remove(os.path.join(OUT_DIR, rel_base + ".~partial." + e2))
                    except Exception:
                        pass
            finally:
                if doc is not None:
                    close_if_ours(doc)
                adsk.doEvents()

            records[data_file.id] = rec
            save()

        # Designs that were in an earlier backup but are no longer in the hub keep their files;
        # the record says since when they are gone. Only after a complete walk of their hub.
        gone = 0
        if not cancelled:
            walked = {safe(h.name) for h in hubs}
            found_ids = {f.id for f, _p in designs}
            for rid, r in records.items():
                if rid in found_ids or (r.get("path") or "").split("/")[0] not in walked:
                    continue
                if not r.get("gone_from_hub"):
                    r["gone_from_hub"] = time.strftime("%Y-%m-%d")
                    changed_names.append("gone from the hub (files kept): " + (r.get("name") or rid))
                gone += 1
        save()

        progress.hide()
        elapsed = int(time.time() - started)
        line = "{}  found {}  new {}  changed {}  moved {}  unchanged {}  failed {}  gone {}  ({}m {}s){}".format(
            time.strftime("%Y-%m-%d %H:%M"), total, count["new"], count["changed"], count["moved"],
            count["unchanged"], count["failed"], gone, elapsed // 60, elapsed % 60,
            "  STOPPED" if cancelled else "")
        try:
            with open(os.path.join(OUT_DIR, "backup-log.txt"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                for n in changed_names:
                    fh.write("    " + n + "\n")
        except Exception:
            pass
        failed_names = [r.get("name") for r in records.values()
                        if r.get("checked_at", "").startswith(time.strftime("%Y-%m-%d")) and
                        (r.get("last_error") or r.get("error"))]
        ui.messageBox(
            "Backup {}.\n\n"
            "new: {}\nchanged (new version): {}\nmoved or renamed: {}\nunchanged: {}\nfailed: {}\n"
            "gone from the hub (files kept): {}\nfound: {}\nelapsed: {}m {}s\n\n{}{}{}".format(
                "stopped" if cancelled else "finished",
                count["new"], count["changed"], count["moved"], count["unchanged"], count["failed"],
                gone, total, elapsed // 60, elapsed % 60, OUT_DIR,
                "\n\n" + "\n".join(changed_names[:12]) if changed_names else "",
                "\n\nfailed: " + ", ".join(failed_names[:8]) if failed_names else "",
            )
        )

    except Exception:
        if ui:
            ui.messageBox("Script failed:\n{}".format(traceback.format_exc()))
