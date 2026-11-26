# SPDX-License-Identifier: Apache-2.0
# ExtractTimeline - dump each design's parametric history to JSON, for rebuilding elsewhere.
#
# The geometry in a STEP export is a dead solid: no sketches, no parameters, nothing to edit.
# This asks Fusion for the recipe instead -- the ordered timeline, every sketch's curves,
# constraints and dimensions, and each feature's driving expressions -- so the design can be
# re-authored in another tool rather than merely re-imported.
#
# Writes <OUT>/<Hub>/<Project>/<folders>/<Design>.timeline.json, one file per design.
# Unknown feature types are still recorded, with whatever is readable, so nothing goes missing
# silently: a design is only as migratable as its least understood feature, and we want that
# visible rather than quietly dropped.
#
# Run from Fusion: Utilities > ADD-INS > Scripts and Add-Ins > ExtractTimeline > Run.

import adsk.core
import adsk.fusion
import traceback
import json
import os
import re
import time

# ---- settings -------------------------------------------------------------
OUT_DIR = os.path.expanduser("~/cad/fusion-timeline")
ALL_HUBS = False
SKETCH_GEOMETRY = True   # curve-by-curve sketch contents (the bulk of the output)

# Bumped whenever the captured shape changes, so re-running upgrades older files in place
# instead of skipping them -- a resumable run should not also mean a stale one.
SCHEMA = 26   # 26 = 25 plus mesh bases (a design whose mesh steps come first and real modelling after is read from the step after its last mesh step on; that state is its starting body) # 25 = 24 made affordable for big assemblies (linked designs read and hidden as a whole, per-step times, linked body lists written once) # 24 = 23 with body lists for big assemblies (linked bodies read once, cap 6000) # 23 = 22 with its test's fixes (feature results read rolled back again, mesh STL written directly) # Crosslink 2026-11-15: 17-21 add `x` (feature inputs/results), per-step bodies, spline shapes, sketch texts; 22 adds frozen states, mesh files, parent components

# Crosslink 2026-11-15 (schema 22): steps whose result the rebuild could not reproduce. The
# translator writes this list after a run ({"<path under OUT_DIR without suffix>": [step, ...]});
# for each listed step the bodies that step made or changed are written as a STEP file, so the
# rebuild can take that state from Fusion and carry on from there. A missing file means none.
STATE_WANTED = os.path.expanduser("~/cad/work/_state_wanted.json")

# A fillet consumes the edges it rounds, so they are absent from the finished body and can only
# be read with the model rolled back to that step. That costs a recompute per such feature, so
# it is a switch: turn it off if a hub is large and you would rather have the rest sooner.
ROLLBACK_FOR_EDGES = True
MEASURE_EACH_STEP = True  # record what the model measures after every step (needs rollback)

# Record what an unmapped feature actually exposes, rather than guessing at it and paying a whole
# run to learn the guess was wrong. Cheap: it reads property names, not geometry.
PROBE_UNMAPPED = True

# ---- what to process ------------------------------------------------------
# ONE selector. ONLY and ONLY_LIST used to be separate filters applied in sequence, so they
# INTERSECTED: a stale list left from a previous part cut the set to zero, and the progress
# dialog then died with "invalid argument minimumValue or maximumValue" rather than saying
# nothing matched. Both are folded in here, and an empty set is now reported, not crashed.
#
#   "all"        every design in the hub
#   "<group>"    a named family from GROUPS below, e.g. "brackets": any design whose NAME
#                contains the family word. Matching on parameters or feature names instead
#                catches far more, including long bodies that merely reference one -- too
#                loose to be a scope.
#   "list"       exactly the design paths in SELECT_LIST, one per line, written as they are
#                under OUT_DIR without the ".timeline.json" suffix:
#                   MyHub/ExampleProject/ExampleFolder/Example Part
#   "<text>"     anything else: a case-insensitive substring of the design name or its
#                folder path, as ONLY used to be
#
# Every mode is resumable and idempotent: a design already read is skipped, so a run that is
# cancelled or crashes can simply be started again. A file from an older SCHEMA counts as read
# when none of the changes since then concern that design (see still_current), and a design is
# read again when the rebuild's wanted list asks for a state its file does not hold yet.
SELECT = "all"   # step 5 sets "list" (SELECT_LIST); a bucket list from step 1 works too
SELECT_LIST = os.path.expanduser("~/cad/work/_reextract.txt")

# Crosslink 2026-11-21: where the designs come from. With InventoryHub's inventory.json (step 1) the
# designs are opened by their ID, without walking the hub (a few minutes per run on a big hub, and
# the unattended loop starts many runs). Without it, or with USE_INVENTORY = False, the hub is walked
# as before. SELECT applies either way. A design whose version or bucket changed since the inventory
# is still read; the final message then asks for a new inventory.
INVENTORY = os.path.expanduser("~/cad/fusion-inventory/inventory.json")
USE_INVENTORY = True

# Named groups, matched on the design's name and its folder path. Add one rather than editing
# SELECT to a substring that happens to work today.
GROUPS = {
    # an example family; edit or add your own
    "brackets": lambda name, path: "bracket" in name.lower(),
}

# Try a few designs before committing to a whole hub. Each design has to be downloaded and
# loaded to be read, so a smoke test on five beats discovering a problem on five hundred.
INPUT_BUDGET_S = 1800    # Crosslink: per design, stop the before-step input reads after this many seconds
MEASURE_BUDGET_S = 2400   # Crosslink: per design, stop per-step measuring after this many seconds (schema 25: was 600; a 160-step assembly with a linked motherboard needs more)
LIMIT = 0        # every design (Crosslink: was 3 for the smoke test on 12 Nov)
# ---------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MM = 10.0  # Fusion works in centimetres; everything here is written in millimetres.


def safe(name):
    cleaned = _INVALID.sub("_", (name or "untitled")).strip().rstrip(".")
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:120] or "untitled"


def pt(p):
    """A point in millimetres."""
    if p is None:
        return None
    try:
        return [round(p.x * MM, 9), round(p.y * MM, 9), round(p.z * MM, 9)]
    except Exception:
        return None


def pt2(p):
    if p is None:
        return None
    try:
        return [round(p.x * MM, 9), round(p.y * MM, 9)]
    except Exception:
        return None


def get(obj, *names):
    """First readable attribute of several, or None. Fusion raises on many properties."""
    for n in names:
        try:
            v = getattr(obj, n)
            if v is not None:
                return v
        except Exception:
            continue
    return None


def expr_of(value_input_owner, *names):
    """A feature's driving *expression* ('width/2 + 3 mm'), not just its number.

    The expression is the whole point: it is what makes the rebuilt design parametric.
    """
    for n in names:
        try:
            o = getattr(value_input_owner, n)
        except Exception:
            continue
        if o is None:
            continue
        try:
            return {"expression": o.expression, "value_mm": round(o.value * MM, 9)}
        except Exception:
            try:
                return {"expression": str(o.expression)}
            except Exception:
                continue
    return None


ITEM_CAP = 3000   # Crosslink: collections larger than this are not read item by item
_TOO_BIG = []


def count_of(coll):
    try:
        return coll.count
    except Exception:
        return 1


def as_items(coll):
    """Every entity in whatever Fusion handed back: a collection, a Vector, or one entity.

    `.count` + `.item(i)` is the ObjectCollection shape and covers most of the API. It does
    NOT cover the `*Vector` types -- `DraftFeature.inputFaces` is a `BRepFaceVector`, and a
    probe of the live object could not read `.count` off it at all. The old code's `except:
    return [coll]` then handed back the vector ITSELF as its one item, and `point_on` found no
    point on a vector, so the feature recorded an empty selection while reporting success.
    That is how every draft in a production design came out with `face_count: 0`.

    So try each access shape in turn and take the first that yields entities. Returning
    `[coll]` is still the last resort, because a single entity is a real case, but it is now
    reached only when nothing enumerable is there.
    """
    try:                                        # ObjectCollection
        n = coll.count
        if n is not None and n > ITEM_CAP:
            # Crosslink 2026-11-14: every .item(i) on a feature's faces rebuilds the whole face
            # list inside Fusion, so reading them one by one is quadratic. A mesh-converted
            # design with a very large face count kept Fusion at 100 % CPU for over 20 minutes
            # here. Such a selection is not a pick list anyone can replay; it is skipped and
            # recorded on the timeline step as `big_collections_skipped`.
            _TOO_BIG.append("{}[{}]".format(type(coll).__name__, n))
            return []
        if n is not None and n >= 0:
            return [coll.item(i) for i in range(n)]
    except Exception:
        pass
    try:                                        # sequence-like Vector
        n = len(coll)
        if n > ITEM_CAP:
            _TOO_BIG.append("{}[{}]".format(type(coll).__name__, n))
            return []
        if n > 0:
            return [coll[i] for i in range(n)]
    except Exception:
        pass
    for attr in ("asArray", "toArray"):         # Vector -> list
        try:
            arr = getattr(coll, attr)
            arr = arr() if callable(arr) else arr
            if arr:
                return list(arr)
        except Exception:
            continue
    try:                                        # plain iteration
        items = [x for x in coll]
        if items:
            return items
    except Exception:
        pass
    return [coll]


def first_nonempty(obj, *names):
    """The first of several attributes that actually HOLDS something, with its name.

    `get()` returns the first attribute that is not None, which is the wrong test here: a
    DraftFeature's `faces` is an empty-but-not-None BRepFaces, so `get(ent, "faces",
    "inputFaces")` always answered `faces` and `inputFaces` -- where the picks really live --
    was never consulted. Returns (value, name, tried) so the caller can record which source
    won and what was looked at, instead of a silent empty list.
    """
    tried = []
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception as e:
            tried.append("%s: raised %s" % (n, str(e)[:60]))
            continue
        if v is None:
            tried.append("%s: None" % n)
            continue
        items = as_items(v)
        # as_items' last resort is [v] itself; that is not a populated collection.
        if items and not (len(items) == 1 and items[0] is v):
            return v, n, tried
        tried.append("%s: %s, empty" % (n, type(v).__name__))
    return None, None, tried


def point_on(item, point_attr):
    """One world-space point on an edge or face, in millimetres.

    `pointOnEdge` is the direct answer but it fails on an edge the feature itself consumed --
    a fillet destroys the very edges it rounds, so by the time the design is read back they are
    gone from the body. The curve's own geometry usually survives that, so fall back to it, and
    finally to the bounding box, before giving up.
    """
    p = get(item, point_attr)
    if callable(p):
        try:
            p = p()
        except Exception:
            p = None
    q = pt(p) if p is not None else None
    if q:
        return q

    g = get(item, "geometry", "worldGeometry")
    if g is not None:
        s, e = pt(get(g, "startPoint")), pt(get(g, "endPoint"))
        if s and e:
            return [round((s[i] + e[i]) / 2.0, 9) for i in range(3)]
        for attr in ("center", "origin"):
            c = pt(get(g, attr))
            if c:
                return c

    bb = get(item, "boundingBox")
    if bb is not None:
        lo, hi = pt(get(bb, "minPoint")), pt(get(bb, "maxPoint"))
        if lo and hi:
            return [round((lo[i] + hi[i]) / 2.0, 9) for i in range(3)]
    return None


def picks(coll, point_attr):
    """A world-space point lying on each edge or face, in millimetres.

    This is the whole trick for re-creating fillets, chamfers, shells and drafts somewhere
    else: the identity of an edge does not survive the move, but a point on it does.
    """
    out = []
    for item in as_items(coll):
        q = point_on(item, point_attr)
        if q:
            out.append(q)
    return out


# Set per document so a feature can be read with the model rolled back to just before it.
_ROLLBACK = {"timeline": None}
# Schema 26: mesh bases (see describe_design). Steps that make no body do not count as modelling.
MESH_BASE = True
MESH_BASE_PASSIVE = {"ConstructionPlane", "ConstructionAxis", "ConstructionPoint", "Sketch",
                     "Occurrence", "JointOrigin", "Canvas", "Decal", ""}   # replaced by f2s_buckets.PASSIVE in run()

# Crosslink 2026-11-21: the bucket rule shared with InventoryHub (f2s_buckets.py next to this script,
# or fusion/common in the toolkit). Loaded in run().
_B = None


def load_buckets():
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "f2s_buckets.py"), os.path.join(here, "..", "common", "f2s_buckets.py")):
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("f2s_buckets", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("f2s_buckets.py is missing next to ExtractTimeline.py: run install.sh again")


def measure(design):
    """Volume, area, body count and bounding box of the model as it stands right now.

    Taken with the timeline marker parked after a given step, this is the reference a rebuild
    checks itself against feature by feature. The bounding box matters as much as the volume:
    a feature built at the wrong height keeps the volume exactly right and moves the box.

    Fusion works in centimetres; everything written here is millimetres.
    """
    try:
        root = design.rootComponent
    except Exception:
        return None
    bodies = []
    try:
        for i in range(root.bRepBodies.count):
            bodies.append(root.bRepBodies.item(i))
        for occ in root.allOccurrences:
            for i in range(occ.bRepBodies.count):
                bodies.append(occ.bRepBodies.item(i))
    except Exception:
        pass

    volume_cm3 = area_cm2 = 0.0
    counted = 0
    lo = [None, None, None]
    hi = [None, None, None]
    for b in bodies:
        try:
            if not b.isSolid:
                continue
            if get(b, "isVisible") is False or get(b, "isLightBulbOn") is False:
                continue
            props = b.physicalProperties
            volume_cm3 += props.volume
            area_cm2 += props.area
            counted += 1
            bb = b.boundingBox
            for k, (mn, mx) in enumerate(((bb.minPoint.x, bb.maxPoint.x),
                                          (bb.minPoint.y, bb.maxPoint.y),
                                          (bb.minPoint.z, bb.maxPoint.z))):
                lo[k] = mn if lo[k] is None else min(lo[k], mn)
                hi[k] = mx if hi[k] is None else max(hi[k], mx)
        except Exception:
            continue
    if not counted:
        return {"solid_bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
    return {"solid_bodies": counted,
            "volume_mm3": round(volume_cm3 * 1000.0, 6),
            "area_mm2": round(area_cm2 * 100.0, 6),
            "bbox_mm": [[round(v * MM, 6) for v in lo], [round(v * MM, 6) for v in hi]]}


def rolled_back_to(index, fn):  # noqa: C901 - guarded by ROLLBACK_FOR_EDGES below
    """Run fn() with the design rolled back to just before timeline step `index`.

    A fillet consumes the edges it rounds, so those edges are absent from the finished body and
    cannot be located there at all. Rolling the marker back puts the model into the state the
    feature actually saw, where its inputs exist. The marker is always restored, and nothing is
    saved, so the design on disk is untouched either way.
    """
    if not ROLLBACK_FOR_EDGES or _ROLLBACK.get("skip"):
        return None
    tl = _ROLLBACK.get("timeline")
    if tl is None or index is None:
        return None
    # Schema 26: never in front of a mesh base (see describe_design); that would redo the mesh steps.
    if index < (_ROLLBACK.get("min_index") or 0):
        return None
    saved = None
    try:
        saved = tl.markerPosition
        tl.markerPosition = index
        adsk.doEvents()
        return fn()
    except Exception:
        return None
    finally:
        if saved is not None:
            try:
                tl.markerPosition = saved
            except Exception:
                pass
        adsk.doEvents()


def edge_sets(ent):
    """Edge picks for fillets and chamfers, which do not keep their edges where you'd expect.

    A FilletFeature holds `edgeSets`, and each set carries its own radius -- a single feature can
    round several groups of edges to different radii. Chamfers are the same shape on newer
    builds. Returns one record per set so each can be re-created with its own size, falling back
    to a plain `edges` collection where that is all there is.
    """
    sets = []
    for attr in ("edgeSets", "filletEdgeSets", "chamferEdgeSets"):
        coll = get(ent, attr)
        if coll is None:
            continue
        for s in as_items(coll):
            pts = picks(get(s, "edges") or [], "pointOnEdge")
            if not pts:
                continue
            rec = {"edge_points": pts, "kind": type(s).__name__}
            for key, names in (("radius", ("radius", "radiusOne")),
                               ("radius2", ("radiusTwo",)),
                               ("distance", ("distance", "distanceOne")),
                               ("distance2", ("distanceTwo",)),
                               ("angle", ("angle",))):
                v = expr_of(s, *names)
                if v:
                    rec[key] = v
            sets.append(rec)
        if sets:
            return sets

    # Older shape: the edges hang directly off the feature.
    for attr in ("edges", "inputEntities"):
        coll = get(ent, attr)
        if coll is None:
            continue
        pts = picks(coll, "pointOnEdge")
        if pts:
            return [{"edge_points": pts, "kind": "direct"}]
    return sets


def axis_of(ent):
    """A revolve's axis as two points in model space.

    The axis is usually a sketch line or a construction axis, neither of which keeps its identity
    across a migration. Its geometry does, and two points define the same axis anywhere.
    """
    for attr in ("axis", "axisEntity", "centerAxis"):
        a = get(ent, attr)
        if a is None:
            continue
        g = get(a, "geometry", "worldGeometry")
        if g is None:
            continue
        start, end = pt(get(g, "startPoint")), pt(get(g, "endPoint"))
        if start and end:
            return {"start": start, "end": end, "kind": type(a).__name__}
        origin, direction = get(g, "origin"), get(g, "direction")
        if origin is not None and direction is not None:
            o, d = pt(origin), pt(direction)
            if o and d:
                return {"start": o, "end": [o[i] + d[i] for i in range(3)],
                        "kind": type(a).__name__}
    return None


def path_of(ent):
    """A sweep's path as the points along its curves, so the route survives the move."""
    p = get(ent, "path")
    if p is None:
        return None
    pts = []
    try:
        for i in range(p.count):
            c = p.item(i)
            g = get(c, "geometry", "worldGeometry")
            if g is None:
                continue
            for attr in ("startPoint", "endPoint"):
                q = pt(get(g, attr))
                if q and (not pts or q != pts[-1]):
                    pts.append(q)
    except Exception:
        return None
    return pts or None


def unmapped_pick(p, why):
    """What an unreadable profile actually IS, plus whatever identity can be had from it.

    The bare `continue` this replaces is why several designs each carried one extrude that
    cannot be replayed with nothing saying why: `profile_count` is set, `profiles` is absent,
    and the selection exists nowhere. PROBE_UNMAPPED cannot reach these -- it only fires for
    feature kinds outside MAPPED_KINDS, and ExtrudeFeature is inside it.

    Two parts to the record, kept separate on purpose:

      probe     the SHAPE of the object -- every readable property and its type. This is the
                diagnosis, and it decides how the real recorder gets written.
      the rest  a BEST-EFFORT identity (area, a point on it, centroid, bounds), each in its
                own try. The main path fails at `parentSketch`, which a BRepFace does not
                have, so the geometric properties may well still read. If they do, this is
                already enough to re-find the selection; if they do not, the probe still says
                why.

    The opportunistic fields are NOT a substitute for knowing the type: a consumer must check
    `type` before trusting any of them.
    """
    rec = {"type": type(p).__name__, "why": str(why)[:200]}
    try:
        rec["probe"] = probe(p)
    except Exception:
        pass
    # A face knows its own area and carries a point guaranteed to lie ON it; a sketch profile
    # answers the same questions through areaProperties. Try both spellings.
    try:
        a = get(p, "area")
        if isinstance(a, (int, float)):
            rec["area_mm2"] = round(a * MM * MM, 6)
    except Exception:
        pass
    try:
        rec["point_on_face"] = pt(get(p, "pointOnFace"))
    except Exception:
        pass
    try:
        ap = p.areaProperties()
        rec["centroid_model"] = pt(ap.centroid)
        if "area_mm2" not in rec:
            rec["area_mm2"] = round(ap.area * MM * MM, 6)
    except Exception:
        pass
    try:
        bb = p.boundingBox
        rec["bbox_model"] = [pt(bb.minPoint), pt(bb.maxPoint)]
    except Exception:
        pass
    try:
        b = get(p, "body")
        if b is not None:
            rec["body"] = get(b, "name")
    except Exception:
        pass
    return dict((k, v) for k, v in rec.items() if v is not None)


_PRE_OPEN = set()


def remember_open_documents(app):
    """Names of every document open before we started. These belong to the user."""
    _PRE_OPEN.clear()
    try:
        for k in range(app.documents.count):
            try:
                _PRE_OPEN.add(app.documents.item(k).name)
            except Exception:
                continue
    except Exception:
        pass
    return set(_PRE_OPEN)


def close_if_ours(doc):
    """Close `doc` only if WE opened it and it has nothing unsaved. Returns why not, or None.

    documents.open() on an already-open DataFile hands back the SAME Document, so an
    unguarded close(False) here shuts the owner's session and throws away their unsaved
    work. Nothing this script produces is worth that: a document left open costs one extra
    tab, and the next run skips it anyway.
    """
    try:
        name = doc.name
    except Exception:
        name = None
    if name is not None and name in _PRE_OPEN:
        return "was already open before this run (the user's)"
    try:
        saved = doc.isSaved
    except Exception:
        saved = None
    if saved is False:
        return "has unsaved changes"
    try:
        doc.close(False)
    except Exception as e:
        return "close failed: %s" % e
    return None


def profile_picks(prof):
    """Each profile as a point inside it, in its own sketch's 2D coordinates.

    Tools re-find a profile by a point inside it, which keeps following the right region when
    parameters change -- far more robust than a stored index.

    Returns (picks, unmapped). `unmapped` holds one `unmapped_pick` record per profile that
    could not be read, so a step that loses its selection says what it lost instead of going
    silent. Callers wanting only the picks take [0].
    """
    out, unmapped = [], []
    for p in as_items(prof):
        try:
            sk = p.parentSketch
            if type(p).__name__ == "SketchText":
                # Crosslink: a text used as a profile. It has no area properties; name it so the
                # sketch's `texts` entry (same token) can stand in.
                out.append({"sketch": get(sk, "name"), "kind": "text",
                            "text_token": token_of(p), "text": get(p, "text")})
                continue
            centroid = p.areaProperties().centroid
            local = sk.modelToSketchSpace(centroid)
            rec = {"sketch": get(sk, "name"), "point": pt2(local)}
            # The 2D point alone has proved unreliable off the XY plane: every profile of the
            # a sketch built on XZ comes back with y exactly 0, and sibling profiles on one
            # sketch all report an identical point. Whatever the conversion does, the centroid
            # in model space is unambiguous -- it needs no sketch frame to interpret, and a
            # consumer can project it onto the sketch plane itself.
            #
            # Still a centroid, so for an annulus or a crescent it lies outside its own
            # region and is a bearing rather than a pick. Recorded as what it is.
            rec["centroid_model"] = pt(centroid)
            try:
                a = p.areaProperties()
                rec["area_mm2"] = round(a.area * MM * MM, 6)
            except Exception:
                pass
            # The region's own extent, which bounds where a usable interior point can be and
            # distinguishes two regions that happen to share an area.
            try:
                bb = p.boundingBox
                rec["bbox_model"] = [pt(bb.minPoint), pt(bb.maxPoint)]
            except Exception:
                pass
            if rec["point"] or rec.get("centroid_model"):
                out.append(rec)
            else:
                unmapped.append(unmapped_pick(p, "no point and no centroid"))
        except Exception as e:
            unmapped.append(unmapped_pick(p, e))
            continue
    return out, unmapped


# ---------------------------------------------------------------------------
# Crosslink 2026-11-15, schema 17: what a rebuild needs and schema 16 did not carry
# ---------------------------------------------------------------------------
# The first full translation run showed that a feature's own numbers are not enough: a Box or a
# Cylinder exposes nothing but the body it made, a Combine does not say which bodies, a Hole
# keeps its position in objects the old reader only named ("Point3D"), and splines came out
# without their shape. Everything below only READS; like the rest of the script nothing is
# changed or saved. Every reader swallows its own errors: a missing extra must never cost the
# step that was already recorded.
X_CAP = 60        # entities described per collection
X_BODY_CAP = 6000  # bodies listed per step (schema 24: was 200, which left 19 assemblies without lists)
X_NAME_CAP = 200   # body names written into one state record
# Never read these. Substrings: they recompute, mutate, or walk the whole model.
X_SKIP = ("delete", "save", "compute", "update", "parent", "nativeObject", "createForAssembly",
          "timelineObject", "healthState", "errorOrWarningMessage", "attributes", "assemblyContext",
          "dissolve", "physicalProperties", "appearance", "material", "textureMapControl")
# Exact names: results read separately (bodies, faces), identity noise, or whole-model links.
X_SKIP_EXACT = {"linkedFeatures", "baseFeature", "targetBaseFeature", "sourceComponent",
                "component", "entityToken", "objectType", "isValid", "this", "thisown",
                "meshBodies", "classType", "revisionId", "design", "bodies", "faces",
                "startFaces", "endFaces", "sideFaces", "patternElements", "resultFeatures",
                "customFeature", "cast", "name"}


def vec(v):
    try:
        return [round(v.x, 9), round(v.y, 9), round(v.z, 9)]
    except Exception:
        return None


def surface_of(face):
    """The analytic surface under a face: enough to rebuild a box, a cylinder or a hole."""
    out = {}
    try:
        g = face.geometry
        t = type(g).__name__
        out["type"] = t
        if t == "Plane":
            out.update(origin=pt(g.origin), normal=vec(g.normal))
        elif t == "Cylinder":
            out.update(origin=pt(g.origin), axis=vec(g.axis), radius_mm=round(g.radius * MM, 9))
        elif t == "Cone":
            out.update(origin=pt(g.origin), axis=vec(g.axis), radius_mm=round(g.radius * MM, 9),
                       half_angle_rad=round(g.halfAngle, 9))
        elif t == "Sphere":
            out.update(origin=pt(g.origin), radius_mm=round(g.radius * MM, 9))
        elif t == "Torus":
            out.update(origin=pt(g.origin), axis=vec(g.axis),
                       major_radius_mm=round(g.majorRadius * MM, 9),
                       minor_radius_mm=round(g.minorRadius * MM, 9))
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def bbox_of(o):
    try:
        bb = o.boundingBox
        return [pt(bb.minPoint), pt(bb.maxPoint)]
    except Exception:
        return None


def body_ref(b, full=True):
    """A body by everything that can find it again elsewhere: name, extent, mass point, a pick."""
    r = {"name": get(b, "name")}
    try:
        r["component"] = b.parentComponent.name
    except Exception:
        pass
    for key, attr in (("solid", "isSolid"), ("visible", "isVisible")):
        v = get(b, attr)
        if v is not None:
            r[key] = bool(v)
    r["bbox_mm"] = bbox_of(b)
    try:
        r["faces"] = b.faces.count
    except Exception:
        pass
    if full:
        try:
            pp = b.physicalProperties
            r["volume_mm3"] = round(pp.volume * 1000.0, 6)
            r["area_mm2"] = round(pp.area * 100.0, 6)
            r["center_of_mass"] = pt(pp.centerOfMass)
        except Exception:
            pass
        try:
            if b.faces.count:
                r["pick"] = pt(b.faces.item(0).pointOnFace)
        except Exception:
            pass
    return r


def face_ref(f):
    r = {"surface": surface_of(f)}
    try:
        r["point"] = pt(f.pointOnFace)
    except Exception:
        pass
    try:
        r["area_mm2"] = round(f.area * 100.0, 6)
    except Exception:
        pass
    try:
        r["body"] = f.body.name
    except Exception:
        pass
    r["bbox_mm"] = bbox_of(f)
    return r


def edge_ref(e):
    r = {}
    try:
        r["point"] = pt(e.pointOnEdge)
    except Exception:
        pass
    try:
        r["length_mm"] = round(e.length * MM, 9)
    except Exception:
        pass
    try:
        r["curve"] = type(e.geometry).__name__
    except Exception:
        pass
    try:
        r["ends"] = [pt(e.startVertex.geometry), pt(e.endVertex.geometry)]
    except Exception:
        pass
    try:
        r["body"] = e.body.name
    except Exception:
        pass
    return r


def describe_any(v, depth=2):  # noqa: C901
    """Any value the API hands back, as plain data. Geometry and model entities are written as
    what locates them; unknown objects are walked `depth` levels."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(v, 12) if isinstance(v, float) else v
    if isinstance(v, str):
        return v[:400]
    t = type(v).__name__
    try:
        if t in ("ModelParameter", "UserParameter", "Parameter"):
            return {"_": "parameter", "name": get(v, "name"), "expression": get(v, "expression"),
                    "value": round(v.value, 12), "unit": get(v, "unit")}
        if t == "Point3D":
            return {"_": "point", "mm": pt(v)}
        if t == "Point2D":
            return {"_": "point2", "mm": [round(v.x * MM, 9), round(v.y * MM, 9)]}
        if t in ("Vector3D", "Vector2D"):
            return {"_": "vector", "v": vec(v) if t == "Vector3D" else [round(v.x, 9), round(v.y, 9)]}
        if t == "Matrix3D":
            return {"_": "matrix", "row_major_cm": [round(q, 12) for q in v.asArray()]}
        if t == "Plane":
            return {"_": "plane_geometry", "origin": pt(v.origin), "normal": vec(v.normal),
                    "u": vec(get(v, "uDirection")), "v": vec(get(v, "vDirection"))}
        if t in ("InfiniteLine3D", "Line3D"):
            return {"_": "line_geometry", "origin": pt(get(v, "origin", "startPoint")),
                    "direction": vec(get(v, "direction")) if t == "InfiniteLine3D" else None,
                    "end": pt(get(v, "endPoint")) if t == "Line3D" else None}
        if t == "BRepBody":
            return {"_": "body", "body": body_ref(v, full=True)}
        if t == "BRepFace":
            return {"_": "face", "face": face_ref(v)}
        if t == "BRepEdge":
            return {"_": "edge", "edge": edge_ref(v)}
        if t == "BRepVertex":
            return {"_": "vertex", "mm": pt(v.geometry)}
        if t == "ConstructionPlane":
            g = get(v, "geometry")
            r = {"_": "construction_plane", "name": get(v, "name")}
            if g is not None:
                r.update(origin=pt(g.origin), normal=vec(g.normal),
                         u=vec(get(g, "uDirection")), v=vec(get(g, "vDirection")))
            return r
        if t == "ConstructionAxis":
            g = get(v, "geometry")
            r = {"_": "construction_axis", "name": get(v, "name")}
            if g is not None:
                r.update(origin=pt(get(g, "origin")), direction=vec(get(g, "direction")))
            return r
        if t == "ConstructionPoint":
            return {"_": "construction_point", "name": get(v, "name"), "mm": pt(get(v, "geometry"))}
        if t == "Sketch":
            return {"_": "sketch", "name": get(v, "name")}
        if t == "Profile":
            r = {"_": "profile"}
            try:
                r["sketch"] = v.parentSketch.name
                ap = v.areaProperties()
                r["area_mm2"] = round(ap.area * 100.0, 6)
                r["centroid_raw"] = pt(ap.centroid)
            except Exception:
                pass
            return r
        if t == "SketchPoint":
            r = {"_": "sketch_point", "at": pt2(get(v, "geometry")), "world": pt(get(v, "worldGeometry"))}
            try:
                r["sketch"] = v.parentSketch.name
            except Exception:
                pass
            return r
        if t.startswith("Sketch") and hasattr(v, "parentSketch"):
            r = {"_": "sketch_entity", "type": t, "token": token_of(v)}
            try:
                r["sketch"] = v.parentSketch.name
            except Exception:
                pass
            if t == "SketchText":
                r["text"] = get(v, "text")
            return r
        if t in ("Occurrence", "Component"):
            return {"_": t.lower(), "name": get(v, "name"), "full_path": get(v, "fullPathName")}
    except Exception as e:
        return {"_": "unreadable", "type": t, "why": str(e)[:80]}

    # collections
    n = None
    try:
        n = v.count
    except Exception:
        try:
            n = len(v)
        except Exception:
            n = None
    if isinstance(n, int) and not isinstance(v, (str, bytes)):
        if n > ITEM_CAP:
            return {"_": "list", "type": t, "count": n, "skipped": "too big"}
        items = []
        for k in range(min(n, X_CAP)):
            try:
                it = v.item(k) if hasattr(v, "item") else v[k]
            except Exception:
                break
            items.append(describe_any(it, depth))
        return {"_": "list", "type": t, "count": n, "items": items}

    if depth <= 0 or not hasattr(v, "objectType"):
        return {"_": "object", "type": t}
    out = {"_": "object", "type": t}
    # Schema 21: a feature used as an input (the feature a pattern or mirror repeats) is named
    # and placed, so the rebuild can tell which of its own steps it is.
    try:
        nm = v.name
        if isinstance(nm, str):
            out["name"] = nm
    except Exception:
        pass
    try:
        out["timeline_index"] = v.timelineObject.index
    except Exception:
        pass
    read = 0
    for name in dir(v):
        if name.startswith("_") or name in X_SKIP_EXACT or any(k in name for k in X_SKIP):
            continue
        try:
            a = getattr(v, name)
        except Exception:
            continue
        if a is None or callable(a):
            continue
        try:
            out[name] = describe_any(a, depth - 1)
        except Exception:
            continue
        read += 1
        if read >= 80:
            break
    return out


def feature_extra(ent):
    """Inputs and results of one timeline entity, read in the model state just after it.

    Called with the marker parked after the step where the design allows it, so `bodies` and
    `faces` are what the feature really made, before later steps consumed them.
    """
    kind = type(ent).__name__
    if kind in ("Sketch", "Occurrence"):
        return None
    x = {}
    try:
        if kind.startswith("Construction"):
            x["self"] = describe_any(ent, 0)
            d = get(ent, "definition")
            if d is not None:
                x["definition"] = describe_any(d, 1)
            return x
        x["props"] = describe_any(ent, 2)
        if isinstance(x["props"], dict):
            x["props"].pop("_", None)
        bodies = get(ent, "bodies")
        if bodies is not None:
            n = count_of(bodies)
            x["body_count"] = n
            if n <= ITEM_CAP:
                x["bodies"] = [body_ref(bodies.item(k)) for k in range(min(n, X_CAP))]
        faces = get(ent, "faces")
        if faces is not None:
            n = count_of(faces)
            x["face_count"] = n
            if n <= 400:
                x["faces"] = [face_ref(faces.item(k)) for k in range(min(n, X_CAP))]
    except Exception as e:
        x["error"] = "{}: {}".format(type(e).__name__, str(e)[:120])
    return x


# Kinds whose inputs are still there after the step, so the read above already has them.
# Schema 19: Shell, Draft and Hole are read before the step too. A shell's removed faces and a
# draft's faces are gone or moved afterwards, and the rebuild needs them as they were.
X_NO_INPUT_READ = ("Sketch", "Occurrence", "ExtrudeFeature", "RevolveFeature", "FilletFeature",
                   "ChamferFeature", "BoxFeature",
                   "CylinderFeature", "SphereFeature", "TorusFeature", "CoilFeature",
                   "ConstructionPlane", "ConstructionAxis", "ConstructionPoint", "Snapshot", "Canvas")
X_INPUT_NAMES = ("targetBody", "toolBodies", "splitBodies", "splittingTool", "inputEntities",
                 "inputBodies", "inputFaces", "body", "point", "basePoint", "sourceBody",
                 "sourceBodies", "patternEntities", "mirrorPlane", "axis", "directionOneEntity",
                 "directionTwoEntity", "surfaces", "tools", "entities", "path", "profile",
                 "stitchSurfaces", "facesToDelete", "sourceFaces", "targetFaces", "trimTool",
                 "inputSurfaces", "boundaryCurves", "definition", "plane", "holePositionDefinition",
                 "loftSections", "sections", "itemsToRemove", "bodiesToRemove", "removeBodies")


def feature_inputs(ent):
    """What a feature works ON, read with the model rolled back to just before it.

    A Combine consumes its tool bodies, a Remove its body, a Split the body it cuts: after the
    step those are gone and the feature answers with nothing (the 16-design test of 15 Nov
    showed `CombineFeature` and `SplitBodyFeature` with no inputs at all).
    """
    out = {}
    for name in X_INPUT_NAMES:
        try:
            v = getattr(ent, name)
        except Exception:
            continue
        if v is None or callable(v):
            continue
        try:
            out[name] = describe_any(v, 1)
        except Exception as e:
            out[name] = {"_": "unreadable", "why": str(e)[:80]}
    return out


_DEST = {"base": None}   # path of the timeline file being written, without its suffix


def expand_groups(tl):
    """Open every timeline group so its steps are part of the timeline that is read.

    Schema 18 found 32 designs whose whole history sits inside one collapsed group: the
    timeline then has a single item, "Group1", with no entity. Expanding changes only how the
    open document shows its timeline; nothing is saved.
    """
    opened = 0
    for _round in range(6):               # groups do not nest deeply; stop when nothing changes
        changed = False
        try:
            n = tl.count
        except Exception:
            break
        for i in range(n):
            try:
                o = tl.item(i)
                if get(o, "isGroup") and get(o, "isCollapsed"):
                    o.isCollapsed = False
                    opened += 1
                    changed = True
            except Exception:
                continue
        if not changed:
            break
        adsk.doEvents()
    return opened


def _feature_body_keys(ent):
    """(component name, body name) of every body a feature made."""
    keys = set()
    for attr in ("bodies", "bRepBodies"):
        col = get(ent, attr)
        if col is None:
            continue
        try:
            for k in range(col.count):
                b = col.item(k)
                try:
                    keys.add((b.parentComponent.name, b.name))
                except Exception:
                    pass
        except Exception:
            pass
        if keys:
            break
    return keys


_WANTED = {"map": None}
_EXPORT_S = {"s": 0.0}   # seconds spent writing files for the design being read
_PENDING_EXTRA = []   # (path, [(body name, transient copy)]) written once the timeline is read


def _nfc(text):
    import unicodedata
    return unicodedata.normalize("NFC", text)


def wanted_steps():
    """Step indexes of the design being read whose state the rebuild asked for."""
    if _WANTED["map"] is None:
        _WANTED["map"] = {}
        try:
            with open(STATE_WANTED, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            _WANTED["map"] = {_nfc(k): set(int(i) for i in v) for k, v in raw.items()}
        except Exception:
            pass
    base = _DEST.get("base")
    if not base:
        return set()
    try:
        rel = _nfc(os.path.relpath(base, OUT_DIR).replace(os.sep, "/"))
    except Exception:
        return set()
    return _WANTED["map"].get(rel) or set()


# Crosslink 2026-11-17: pass 2 froze about 250 states in one design and Fusion grew to 137 GB
# (macOS ran out of memory, Fusion had to be force-quit). A design is therefore visited in
# portions: at most STATE_CHUNK new state files per visit, then it is closed and opened again;
# states already on disk are taken over from the file of the visit before. Fusion's own memory
# is read along the way, and past MEM_CAP_GB the run ends cleanly and asks for a restart.
STATE_CHUNK = 40
MEM_CAP_GB = 40.0
MEM_START_GB = 32.0      # no new visit above this: it would hit the cap without writing anything
_CHUNK = {"left": 0, "pending": 0, "new": 0, "reused": 0, "old": {}, "keep": set(), "stop": None, "n": 0}


def _win_private_gb(handle=None):
    """Private bytes of a process on Windows (this one by default), in GB, or None.

    What Windows has committed for the process, the counterpart of the footprint macOS reports;
    read through the Win32 API (ctypes), so no extra module is needed inside Fusion."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        if not k32.K32GetProcessMemoryInfo(handle or k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return None
        return c.PrivateUsage / 1e9
    except Exception:
        return None


def fusion_mem_gb():
    """Memory footprint of this Fusion process in GB, or None. macOS: as `top` counts it
    (compressed and swapped memory included; the resident size alone showed 127 MB of 136 GB).
    Windows: the process's private bytes."""
    if os.name == "nt":
        return _win_private_gb()
    try:
        import subprocess
        out = subprocess.run(["/usr/bin/top", "-l", "1", "-pid", str(os.getpid()), "-stats", "mem"],
                             capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        m = re.match(r"\s*([0-9.]+)([KMGT])", out[-1])
        return float(m.group(1)) * {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3}[m.group(2)]
    except Exception:
        return None


def mem_over(limit=None):
    """True once Fusion is past the cap; remembers it so the run stops after this design."""
    if _CHUNK["stop"] is None:
        g = fusion_mem_gb()
        if g is not None and g > (limit or MEM_CAP_GB):
            _CHUNK["stop"] = round(g, 1)
    return _CHUNK["stop"] is not None


def old_states(dest):
    """Frozen-state records of an earlier read of this design whose files are still there."""
    got = {}
    try:
        with open(dest, encoding="utf-8") as fh:
            d = json.load(fh)
        # Crosslink 2026-11-21: schema 26 only adds mesh bases; states of a schema 25 read are
        # still good, so a design re-listed later does not write them all again.
        if d.get("schema", 0) < 25:
            return got
        folder = os.path.dirname(dest)
        for r in d.get("timeline") or []:
            st = r.get("state_step")
            if (isinstance(st, dict) and st.get("frozen") and st.get("file") and not st.get("error")
                    and os.path.exists(os.path.join(folder, st["file"]))):
                got[r.get("index")] = (r.get("name"), st)
    except Exception:
        pass
    return got


# Crosslink 2026-11-22: the oldest file layout that later schemas only add to. A file from 23 on
# is kept unless one of the changes after it concerns the design (still_current).
KEEP_FROM_SCHEMA = 23


def _capped(v):
    """True when an old body list stopped at the old 200-body cap (fixed in schemas 24 and 25)."""
    if isinstance(v, dict):
        return str(v.get("skipped", "")).startswith("more than") or any(_capped(x) for x in v.values())
    if isinstance(v, list):
        return any(_capped(x) for x in v if isinstance(x, (dict, list)))
    return False


def still_current(seen):
    """True when a file needs no new read for this SCHEMA (Crosslink 2026-11-22).

    Each schema after KEEP_FROM_SCHEMA changed the output of some designs only; a file older
    than this SCHEMA is read again only when such a change concerns it. Re-reading every file
    after each extractor change cost hours on a big hub, and the old rule did exactly that:
      24, 25  body lists past the old cap of 200 bodies (big assemblies)
      26      mesh bases (designs modelled after their last mesh step)
    """
    schema = seen.get("schema", 0)
    if schema >= SCHEMA:
        return True
    if schema < KEEP_FROM_SCHEMA:
        return False
    if schema < 25 and (_capped(seen.get("final_bodies")) or
                        any(_capped(r.get("bodies_after")) for r in seen.get("timeline") or [])):
        return False
    if schema < 26:
        if _B is None:
            return False
        kinds = [r.get("entity") or "" for r in seen.get("timeline") or []]
        base, _why = _B.mesh_base(kinds, len(seen.get("final_meshes") or []))
        if base:
            return False
    return True


def has_state(st):
    """True if a step's `state_step` record holds its state: frozen (wanted states, mesh bases) or
    written after a base or derive feature, which carries no `frozen` flag.

    Crosslink 2026-11-26: only `frozen` counted, so a base step on the wanted list (next_pass.py
    lists every state on disk) looked missing forever and its design was read again every round."""
    return isinstance(st, dict) and bool(st.get("frozen") or (st.get("file") and not st.get("error")))


def frozen_steps(seen):
    """Indexes of the steps whose state an earlier read wrote, mesh bases included (any schema)."""
    out = set()
    for r in (seen or {}).get("timeline") or []:
        if has_state(r.get("state_step")) and r.get("index") is not None:
            out.add(r["index"])
    return out


def states_missing(dest, seen):
    """Wanted steps (rebuild's list) that the file at `dest` holds no frozen state for.

    Crosslink 2026-11-22: replaces comparing the wanted list's date with the file's (states_newer, 17 Nov),
    which read every listed design again whenever the list was rewritten, even when the file
    already held every state asked for."""
    _DEST_saved = _DEST.get("base")
    try:
        _DEST["base"] = dest[: -len(".timeline.json")]
        return wanted_steps() - frozen_steps(seen)
    except Exception:
        return set()
    finally:
        _DEST["base"] = _DEST_saved


def _body_sig(b):
    return (b.get("faces"), round(b.get("volume_mm3") or 0.0, 4), round(b.get("area_mm2") or 0.0, 4),
            tuple(round(v, 5) for v in (b.get("center_of_mass") or [])),
            tuple(round(v, 5) for c in (b.get("bbox_mm") or []) for v in c))


def changed_body_keys(before, after):
    """(component, body) of every solid body a step made or changed, from the two body lists."""
    old = {}
    for b in before:
        old.setdefault((b.get("component"), b.get("name")), []).append(_body_sig(b))
    keys = set()
    for b in after:
        if b.get("solid") is False:
            continue
        k = (b.get("component"), b.get("name"))
        if _body_sig(b) not in old.get(k, []):
            keys.add(k)
    return keys


def meshes_now(design):
    """Mesh bodies of the model as it stands: (component, name) and visibility."""
    out = []
    try:
        comps = design.allComponents
        for ci in range(comps.count):
            comp = comps.item(ci)
            for k in range(comp.meshBodies.count):
                mb = comp.meshBodies.item(k)
                out.append({"name": mb.name, "component": comp.name,
                            "visible": bool(get(mb, "isVisible"))})
                if len(out) > X_BODY_CAP:
                    return out
    except Exception:
        pass
    return out


MESH_TRIANGLE_CAP = 3000000


def write_mesh_stl(mb, path):
    """Binary STL of a mesh body, in millimetres. -> number of triangles"""
    import struct
    last = None
    for attr in ("displayMesh", "mesh"):
        try:
            m = getattr(mb, attr)
            if m is None:
                continue
            co = m.nodeCoordinatesAsDouble
            idx = getattr(m, "nodeIndices", None)
            if idx is None:
                idx = m.triangleNodeIndices
                if getattr(m, "quadCount", 0) or getattr(m, "polygonCount", 0):
                    raise RuntimeError("mesh has quads or polygons besides triangles")
            n = len(idx) // 3
            if not n:
                continue
            if n > MESH_TRIANGLE_CAP:
                raise RuntimeError("{} triangles, more than the cap".format(n))
            pack = struct.Struct("<12fH").pack
            with open(path, "wb") as fh:
                fh.write(b"Crosslink ExtractTimeline mesh, mm".ljust(80, b" "))
                fh.write(struct.pack("<I", n))
                buf = []
                for t in range(n):
                    a, b, c = idx[3 * t] * 3, idx[3 * t + 1] * 3, idx[3 * t + 2] * 3
                    buf.append(pack(0.0, 0.0, 0.0,
                                    co[a] * MM, co[a + 1] * MM, co[a + 2] * MM,
                                    co[b] * MM, co[b + 1] * MM, co[b + 2] * MM,
                                    co[c] * MM, co[c + 1] * MM, co[c + 2] * MM, 0))
                    if len(buf) >= 20000:
                        fh.write(b"".join(buf))
                        buf = []
                fh.write(b"".join(buf))
            return n
        except Exception as e:
            last = e
    raise RuntimeError("no triangles could be read ({})".format(last))


def export_new_meshes(design, index, before, after):
    """Write the mesh bodies that appeared at step `index` as STL (OBJ if STL is refused).

    A mesh import has no recipe and is not a solid, so no STEP holds it. Nothing is changed in
    the design: the export manager is handed the mesh body itself."""
    base = _DEST.get("base")
    had = {(m.get("component"), m.get("name")) for m in before or []}
    want = [(m.get("component"), m.get("name")) for m in after or []
            if (m.get("component"), m.get("name")) not in had]
    if not base or not want:
        return None
    res = []
    try:
        comps = design.allComponents
        n = 0
        for ci in range(comps.count):
            comp = comps.item(ci)
            for k in range(comp.meshBodies.count):
                mb = comp.meshBodies.item(k)
                if (comp.name, mb.name) not in want:
                    continue
                n += 1
                one = {"name": mb.name, "component": comp.name}
                try:
                    bb = mb.boundingBox       # the mesh's size, to tell the file's unit by
                    one["extent_mm"] = round(max(bb.maxPoint.x - bb.minPoint.x,
                                                 bb.maxPoint.y - bb.minPoint.y,
                                                 bb.maxPoint.z - bb.minPoint.z) * MM, 6)
                except Exception:
                    pass
                # The 16-design test of schema 22 (15 Nov, 23:35): the export manager takes a mesh
                # body for STL and OBJ, answers True and writes no file. So the triangles are read
                # from the body and the STL is written here, in millimetres.
                path = "{}.mesh{}_{}.stl".format(base, index, n)
                try:
                    t0 = time.time()
                    tris = write_mesh_stl(mb, path)
                    _EXPORT_S["s"] += time.time() - t0
                    one.update(file=os.path.basename(path), bytes=os.path.getsize(path),
                               triangles=tris, units="mm")
                except Exception as e:
                    one["error"] = str(e)[:160]
                res.append(one)
                if len(res) >= 20:
                    return res
    except Exception as e:
        res.append({"error": str(e)[:160]})
    return res


def flush_extra_exports():
    """Write the bodies a STEP export left out, from copies, through an empty scratch document.

    Schema 21 showed 14 designs where bodies were switched on for the export and still not
    written (a case with three fan bodies). A transient copy of such a body is taken at the step and
    kept until the whole timeline is read. Only then is a new, untitled document made, the
    copies put into it, that document exported and closed without saving. The design being read
    is not touched by this at all.
    """
    if not _PENDING_EXTRA:
        return None
    res = {}
    app = adsk.core.Application.get()
    home = None
    try:
        home = app.activeDocument
    except Exception:
        pass
    try:
        for path, items in list(_PENDING_EXTRA):
            key = os.path.basename(path)
            ndoc = None
            try:
                ndoc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
                nd = adsk.fusion.Design.cast(ndoc.products.itemByProductType("DesignProductType"))
                nd.designType = adsk.fusion.DesignTypes.DirectDesignType
                root = nd.rootComponent
                put = 0
                for name, tb in items:
                    try:
                        nb = root.bRepBodies.add(tb)
                        try:
                            nb.name = name
                        except Exception:
                            pass
                        put += 1
                    except Exception:
                        pass
                em = nd.exportManager
                ok = em.execute(em.createSTEPExportOptions(path))
                res[key] = {"bodies": put, "ok": bool(ok) and os.path.exists(path)}
            except Exception as e:
                res[key] = {"error": str(e)[:160]}
            finally:
                if ndoc is not None:
                    try:
                        ndoc.close(False)
                    except Exception as e:
                        res.setdefault(key, {})["close_error"] = str(e)[:120]
    finally:
        del _PENDING_EXTRA[:]
        try:
            if home is not None and app.activeDocument != home:
                home.activate()
        except Exception as e:
            res["reactivate_error"] = str(e)[:120]
    return res


def export_state_step(design, index, ent=None, keys=None):
    """Write the bodies an import step brought in (marker just after step `index`) as STEP.

    For a base feature, i.e. bodies that came in from outside: there is no recipe to rebuild
    them from, so the rebuild has to start from the bodies themselves.

    Schema 20: a STEP export writes what is visible. The 12-design test of schema 19 showed
    both sides of that: hidden imported bodies were missing from the file (a fan mount:
    3 bodies in Fusion, 1 in the file), and when the import came late in a timeline the file
    also held everything built before it (a card design: 58 bodies that were not imported).
    So for the moment of the export the feature's own bodies are shown and all others hidden,
    and every light bulb is put back afterwards. If the feature will not name its bodies, all
    bodies are shown instead and the record says scope "all". Nothing is saved.
    """
    base = _DEST.get("base")
    if not base:
        return {"error": "no destination"}
    path = "{}.base{}.step".format(base, index)
    # Schema 22: `keys` names the bodies outright (those a step made or changed, worked out
    # from the body lists before and after it); a feature that names its own bodies comes first.
    want = _feature_body_keys(ent) if ent is not None else set()
    scope = "feature" if want else "all"
    if not want and keys:
        want, scope = set(keys), "changed"
    undo = []          # (object, attribute, old value)
    solids = {}        # name -> body, of the solid bodies shown for the export

    def put(obj, attr, val):
        try:
            old = getattr(obj, attr)
            if bool(old) != bool(val):
                setattr(obj, attr, val)
                undo.append((obj, attr, old))
        except Exception:
            pass

    rec = {}
    t_export = time.time()
    try:
        try:
            root = design.rootComponent
            # Schema 25: a linked design none of whose bodies is wanted is switched off as a
            # whole (one light bulb) instead of body by body: 2,400 bodies of a motherboard
            # were switched off and on again for every state file. Only when every linked
            # design can be left out that way; otherwise as before.
            skip_ids, occs = set(), None
            if want:
                try:
                    own, tops = _own_and_tops(root)
                    wanted_comps = {cn for cn, _ in want}
                    infos = [_LINKED_CACHE.get(get(t, "fullPathName")) for t in tops]
                    if tops and all(c is not None and c["comp_ids"]
                                    and not (wanted_comps & c["comp_names"]) for c in infos):
                        for occ in own:
                            put(occ, "isLightBulbOn", True)
                        for t, c in zip(tops, infos):
                            put(t, "isLightBulbOn", False)
                            skip_ids |= c["comp_ids"]
                        occs = own
                        rec["linked_hidden_whole"] = len(tops)
                except Exception:
                    skip_ids, occs = set(), None
            if occs is None:
                for occ in root.allOccurrences:
                    put(occ, "isLightBulbOn", True)
            shown = []
            comps = design.allComponents
            for ci in range(comps.count):
                comp = comps.item(ci)
                if skip_ids and get(comp, "id") in skip_ids:
                    continue
                put(comp, "isBodiesFolderLightBulbOn", True)
                for k in range(comp.bRepBodies.count):
                    bd = comp.bRepBodies.item(k)
                    on = (not want) or ((comp.name, bd.name) in want)
                    put(bd, "isLightBulbOn", on)
                    if on:
                        shown.append(bd.name)
                        if want and get(bd, "isSolid") and comp == root:
                            solids[bd.name] = bd
                try:
                    for k in range(comp.meshBodies.count):
                        put(comp.meshBodies.item(k), "isLightBulbOn", False)
                except Exception:
                    pass
            rec["scope"] = scope
            rec["bodies"] = shown[:X_NAME_CAP]
            rec["bodies_shown"] = len(shown)
            t_a = time.time()
            adsk.doEvents()
            # schema 25: where the time of a state file goes (seconds): switching bodies,
            # letting Fusion redraw, writing the file, the check after it, switching back
            rec["t"] = {"switch": round(t_a - t_export, 2), "events": round(time.time() - t_a, 2)}
        except Exception as e:
            rec["visibility_error"] = str(e)[:160]
        t_w = time.time()
        try:
            em = design.exportManager
            ok = em.execute(em.createSTEPExportOptions(path))
            if ok and os.path.exists(path):
                rec.update(file=os.path.basename(path), bytes=os.path.getsize(path))
            else:
                rec["error"] = "export returned {}".format(ok)
        except Exception as e:
            rec["error"] = str(e)[:160]
        if isinstance(rec.get("t"), dict):
            rec["t"]["write"] = round(time.time() - t_w, 2)
        # Schema 22: is every body that was switched on really in the file? Checked by name for
        # solid bodies of the top component with plain ASCII names (STEP writes other letters
        # in an escaped form). What is missing is copied now, while the model stands at this
        # step, and written through a scratch document once the timeline is read.
        try:
            if rec.get("file") and solids:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                have = set(re.findall(r"(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(\s*'([^']*)'", txt))
                miss = [n for n in solids if n.isascii() and n not in have]
                if miss:
                    rec["missing"] = miss[:X_NAME_CAP]
                    rec["missing_state"] = {n: {"bulb": bool(get(solids[n], "isLightBulbOn")),
                                                "visible": bool(get(solids[n], "isVisible"))}
                                            for n in miss[:20]}
                    tmp = adsk.fusion.TemporaryBRepManager.get()
                    copies = []
                    for n in miss:
                        try:
                            copies.append((n, tmp.copy(solids[n])))
                        except Exception:
                            pass
                    if copies:
                        extra = "{}.base{}.extra.step".format(base, index)
                        _PENDING_EXTRA.append((extra, copies))
                        rec["extra_file"] = os.path.basename(extra)
                        rec["extra_bodies"] = [n for n, _ in copies][:X_NAME_CAP]
        except Exception as e:
            rec["missing_check_error"] = str(e)[:160]
    finally:
        t_r = time.time()
        failed = 0
        for obj, attr, old in reversed(undo):
            try:
                setattr(obj, attr, old)
            except Exception:
                failed += 1
        if failed:
            rec["restore_failed"] = failed
        if isinstance(rec.get("t"), dict):
            rec["t"]["restore"] = round(time.time() - t_r, 2)
            rec["t"]["switched"] = len(undo)
        _EXPORT_S["s"] += time.time() - t_export
    return rec


_LINKED_CACHE = {}   # path of a linked design's occurrence -> {key, records, comp_ids, comp_names}
_LINKED_SETS = []    # every list of linked body records made for this design (written once each)


def _own_and_tops(root):
    """The occurrence tree from the top, without entering linked designs.

    -> (occurrences of this design, occurrences that are a linked design). Schema 25: asking
    every one of a motherboard's 1,000 occurrences at every step is what made a step of a big
    assembly cost half a minute.
    """
    own, tops = [], []
    queue = [root.occurrences.item(i) for i in range(root.occurrences.count)]
    while queue:
        occ = queue.pop(0)
        if get(occ, "isReferencedComponent"):
            tops.append(occ)
            continue
        own.append(occ)
        ch = occ.childOccurrences
        queue.extend(ch.item(i) for i in range(ch.count))
    return own, tops


def _linked_records(root, top):
    """Body records of everything inside one linked design, read once per placement."""
    path = get(top, "fullPathName")
    place = None
    try:
        place = tuple(round(v, 7) for v in top.transform2.asArray())
    except Exception:
        try:
            place = tuple(round(v, 7) for v in top.transform.asArray())
        except Exception:
            place = None
    key = (place, get(get(top, "component"), "revisionId"))
    c = _LINKED_CACHE.get(path) if isinstance(path, str) else None
    if c is not None and place is not None and c["key"] == key:
        return c["records"]
    recs, ids, names = [], set(), set()
    for occ in root.allOccurrences:
        p = get(occ, "fullPathName")
        if not (isinstance(p, str) and isinstance(path, str)
                and (p == path or p.startswith(path + "+"))):
            continue
        comp = get(occ, "component")
        if comp is not None:
            ids.add(get(comp, "id"))
            names.add(get(comp, "name"))
        for i in range(occ.bRepBodies.count):
            r = body_ref(occ.bRepBodies.item(i))
            r["linked"] = p
            recs.append(r)
    ids.discard(None)
    if isinstance(path, str) and place is not None:
        _LINKED_CACHE[path] = {"key": key, "records": recs, "comp_ids": ids, "comp_names": names}
        _LINKED_SETS.append(recs)
    return recs


def bodies_now(design):
    """Every body of the model as it stands, so a step can be compared body by body.

    Schema 24: an inserted assembly (a motherboard with 1,092 bodies) used to end the list at
    200 bodies, and with it the step-by-step check and the frozen states of 19 designs. Bodies
    of a linked design cannot be changed from this timeline, only placed, so they are marked
    `linked` with the path of their occurrence and read once per placement of the linked
    design (schema 25: per linked design as a whole, without visiting its occurrences again;
    their `visible` is as it was at that first reading). All others are measured at every step.
    """
    out = []
    try:
        root = design.rootComponent
        for i in range(root.bRepBodies.count):
            out.append(body_ref(root.bRepBodies.item(i)))
        own, tops = _own_and_tops(root)
        for occ in own:
            for i in range(occ.bRepBodies.count):
                out.append(body_ref(occ.bRepBodies.item(i)))
                if len(out) > X_BODY_CAP:
                    return {"skipped": "more than {} bodies".format(X_BODY_CAP)}
        for top in tops:
            out.extend(_linked_records(root, top))
            if len(out) > X_BODY_CAP:
                return {"skipped": "more than {} bodies".format(X_BODY_CAP)}
    except Exception as e:
        return {"error": str(e)[:120]}
    return out


def measure_from(bodies):
    """measure() worked out from a body list that is already there (schema 25).

    measure() asks Fusion for the volume of every visible solid body again; with 2,400 bodies
    of a linked motherboard that was most of a step's time. None when the list cannot give it.
    """
    volume = area = 0.0
    counted = 0
    lo, hi = [None] * 3, [None] * 3
    for b in bodies:
        if b.get("solid") is not True or b.get("visible") is False:
            continue
        if b.get("volume_mm3") is None or b.get("area_mm2") is None:
            return None
        volume += b["volume_mm3"]
        area += b["area_mm2"]
        counted += 1
        bb = b.get("bbox_mm")
        if bb:
            for k in range(3):
                lo[k] = bb[0][k] if lo[k] is None else min(lo[k], bb[0][k])
                hi[k] = bb[1][k] if hi[k] is None else max(hi[k], bb[1][k])
    if not counted:
        return {"solid_bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
    if lo[0] is None:
        return None
    return {"solid_bodies": counted, "volume_mm3": round(volume, 6), "area_mm2": round(area, 6),
            "bbox_mm": [[round(v, 6) for v in lo], [round(v, 6) for v in hi]], "from": "bodies"}


def compact_linked(doc):
    """Write each linked design's body records once (schema 25).

    A step's `bodies_after` repeated the 2,414 records of a linked motherboard at every step:
    108 MB for a 55-step design. Each such run of records becomes {"linked_set": n, "count": k}
    and the records stand once in `linked_sets`. The reader puts them back.
    """
    try:
        first = {id(recs[0]): n for n, recs in enumerate(_LINKED_SETS) if recs}
        used = set()

        def pack(lst):
            res, i = [], 0
            while i < len(lst):
                n = first.get(id(lst[i]))
                if n is not None:
                    recs = _LINKED_SETS[n]
                    k = len(recs)
                    if i + k <= len(lst) and lst[i + k - 1] is recs[-1]:
                        res.append({"linked_set": n, "count": k})
                        used.add(n)
                        i += k
                        continue
                res.append(lst[i])
                i += 1
            return res

        packed = []
        for rec in doc.get("timeline") or []:
            if isinstance(rec.get("bodies_after"), list):
                packed.append((rec, "bodies_after", pack(rec["bodies_after"])))
        if isinstance(doc.get("final_bodies"), list):
            packed.append((doc, "final_bodies", pack(doc["final_bodies"])))
        if not used:
            return
        sets = {str(n): list(_LINKED_SETS[n]) for n in sorted(used)}
        for holder, key, val in packed:
            holder[key] = val
        doc["linked_sets"] = sets
    except Exception as e:
        doc["linked_sets_error"] = str(e)[:160]


def curve_samples(g, n_hint=0):
    """Points along a curve at even parameter steps, ends included (2D, mm)."""
    ev = g.evaluator
    r = ev.getParameterExtents()
    t0, t1 = r[1], r[2]
    n = min(400, max(24, 8 * n_hint))
    params = [t0 + (t1 - t0) * k / (n - 1.0) for k in range(n)]
    r = ev.getPointsAtParameters(params)
    pts = r[1]
    flat = all(abs(q.z) < 1e-9 for q in pts)
    return [pt2(q) for q in pts] if flat else [pt(q) for q in pts]


def spline_exact(c, rec):
    """The spline's real shape: its NURBS data and points along it.

    Fit points alone do not fix a spline (the tangent handles are missing), control point
    splines raised on read and fixed splines had no reader at all, so schema 16 carried 2,917
    spline curves across the hub without a usable shape.
    """
    g = get(c, "geometry")
    if g is None:
        rec["geometry_missing"] = True
        return
    n_cp = 0
    try:
        d = g.getData()
        if d and d[0]:
            cps = list(d[1])
            n_cp = len(cps)
            rec["nurbs"] = {
                "control_points": [pt(q) for q in cps],
                "degree": d[2],
                "knots": [round(k, 12) for k in d[3]],
                "rational": bool(d[4]),
                "weights": [round(w, 12) for w in d[5]] if d[4] else None,
                "periodic": bool(d[6]),
            }
    except Exception as e:
        rec["nurbs_error"] = str(e)[:120]
    try:
        rec["samples"] = curve_samples(g, n_cp)
    except Exception as e:
        rec["samples_error"] = str(e)[:120]
    v = get(c, "isClosed")
    if v is not None:
        rec["closed"] = bool(v)


def geometry_curve(g):
    """One Curve3D (as SketchText.asCurves returns them) as plain data."""
    t = type(g).__name__
    r = {"type": t}
    try:
        if t == "Line3D":
            r.update(start=pt(g.startPoint), end=pt(g.endPoint))
        elif t == "Arc3D":
            r.update(center=pt(g.center), start=pt(g.startPoint), end=pt(g.endPoint),
                     radius_mm=round(g.radius * MM, 9), normal=vec(g.normal))
        elif t == "Circle3D":
            r.update(center=pt(g.center), radius_mm=round(g.radius * MM, 9), normal=vec(g.normal))
        else:
            r["samples"] = curve_samples(g, 3)
    except Exception as e:
        r["error"] = str(e)[:80]
    return r


def sketch_texts(sk):
    """Sketch texts: the words and their settings, and the outline Fusion draws for them."""
    out = []
    try:
        texts = sk.sketchTexts
        n = texts.count
    except Exception:
        return out
    for i in range(min(n, 200)):
        try:
            t = texts.item(i)
        except Exception:
            continue
        r = {"token": token_of(t), "text": get(t, "text"), "font": get(t, "fontName")}
        try:
            r["height_mm"] = round(t.height * MM, 9)
        except Exception:
            pass
        hp = get(t, "heightParameter")
        if hp is not None:
            r["height_expression"] = get(hp, "expression")
        for key, attr in (("style", "textStyle"), ("h_flip", "isHorizontalFlip"),
                          ("v_flip", "isVerticalFlip"), ("angle_rad", "angle")):
            v = get(t, attr)
            if v is not None:
                r[key] = v if isinstance(v, (bool, int, float)) else str(v)
        r["bbox_mm"] = bbox_of(t)
        try:
            r["definition"] = describe_any(get(t, "definition"), 2)
        except Exception:
            pass
        try:
            cs = t.asCurves()
            items = as_items(cs)
            r["outline_count"] = len(items)
            r["outline"] = [geometry_curve(g) for g in items[:6000]]
        except Exception as e:
            r["outline_error"] = str(e)[:120]
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# sketches
# ---------------------------------------------------------------------------

def sketch_curves(sk):
    out = []
    try:
        curves = sk.sketchCurves
    except Exception:
        return out

    for coll, kind in (
        (get(curves, "sketchLines"), "line"),
        (get(curves, "sketchCircles"), "circle"),
        (get(curves, "sketchArcs"), "arc"),
        (get(curves, "sketchEllipses"), "ellipse"),
        (get(curves, "sketchFittedSplines"), "fitted_spline"),
        (get(curves, "sketchFixedSplines"), "fixed_spline"),
        (get(curves, "sketchControlPointSplines"), "control_spline"),
        (get(curves, "sketchConicCurves"), "conic"),
    ):
        if coll is None:
            continue
        for i in range(coll.count):
            c = coll.item(i)
            rec = {"type": kind, "construction": bool(get(c, "isConstruction"))}
            tok = token_of(c)
            if tok:
                rec["token"] = tok
            def pt_tok(rec, c, attr, key):
                """Record a defining point's position AND its token.

                A linear dimension is attached to sketch POINTS, not to the curve. Recording the
                position without the token is what made every size dimension unusable: the
                recipe knew where the point was and could not tell which dimension meant it.
                """
                try:
                    p = getattr(c, attr)
                except Exception:
                    return
                try:
                    rec[key] = pt2(p.geometry)
                except Exception:
                    pass
                t = token_of(p)
                if t:
                    rec.setdefault("point_tokens", {})[key] = t

            try:
                if kind == "line":
                    pt_tok(rec, c, "startSketchPoint", "start")
                    pt_tok(rec, c, "endSketchPoint", "end")
                elif kind == "circle":
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    rec["radius_mm"] = round(c.radius * MM, 9)
                elif kind == "arc":
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    pt_tok(rec, c, "startSketchPoint", "start")
                    pt_tok(rec, c, "endSketchPoint", "end")
                    rec["radius_mm"] = round(c.radius * MM, 9)
                elif kind == "ellipse":
                    # CENTRE ALONE IS NOT AN ELLIPSE. Recording only the centre meant the curve
                    # was never drawn in the rebuild, so the regions its arc bounds never
                    # formed: a profile sketch holding a single ellipse loses every region that
                    # ellipse bounds, so the rebuild finds fewer regions than Fusion reports.
                    # The thin slivers left over resolved to nothing, which looked convincingly
                    # like a region finder that would not divide -- it was a boundary that was
                    # never there. `sketch.ellipse` wants centre, the major-axis end, and a
                    # minor radius, so record exactly those.
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    for key, names in (("major_radius_mm", ("majorAxisRadius",)),
                                       ("minor_radius_mm", ("minorAxisRadius",))):
                        v = get(c, *names)
                        if isinstance(v, float):
                            rec[key] = round(v * MM, 9)
                    # The major axis as a direction, and the axis END as a 2D sketch point --
                    # what the rebuild actually selects with.
                    ax = get(c, "majorAxis")
                    ctr = rec.get("center")
                    if ax is not None and ctr and rec.get("major_radius_mm"):
                        try:
                            n = (ax.x * ax.x + ax.y * ax.y + ax.z * ax.z) ** 0.5
                        except Exception:
                            n = 0.0
                        if n > 0:
                            # majorAxis is a model-space vector; the sketch's own frame is what
                            # `center` is in, so project it the same way a sketch point is.
                            try:
                                sk_ax = sk.modelToSketchSpace(adsk.core.Point3D.create(
                                    ax.x / n * (rec["major_radius_mm"] / MM), ax.y / n *
                                    (rec["major_radius_mm"] / MM), ax.z / n *
                                    (rec["major_radius_mm"] / MM)))
                                org = sk.modelToSketchSpace(adsk.core.Point3D.create(0, 0, 0))
                                rec["major_end"] = [round(ctr[0] + (sk_ax.x - org.x) * MM, 9),
                                                    round(ctr[1] + (sk_ax.y - org.y) * MM, 9)]
                            except Exception:
                                pass
                    if not rec.get("major_radius_mm"):
                        # Never leave a curve recorded as a bare point without saying so.
                        rec["geometry_missing"] = True
                    # SELF-CHECK. A record that contradicts itself must say so in the record;
                    # a consumer cannot be expected to re-derive what the extractor knew.
                    _ma = rec.get("major_radius_mm")
                    _mi = rec.get("minor_radius_mm")
                    if isinstance(_ma, float) and isinstance(_mi, float) and _ma < _mi:
                        # The major semi-axis is by definition the larger one, so the two
                        # magnitudes are right and the LABELS are not: whichever field a
                        # consumer reads as "major" will be the wrong axis.
                        rec["axis_labels_suspect"] = True
                        rec["larger_semi_axis_mm"] = max(_ma, _mi)
                        rec["smaller_semi_axis_mm"] = min(_ma, _mi)
                    _me, _ctr = rec.get("major_end"), rec.get("center")
                    if _me and _ctr and isinstance(_ma, float):
                        _d = ((_me[0] - _ctr[0]) ** 2 + (_me[1] - _ctr[1]) ** 2) ** 0.5
                        if abs(_d - _ma) > 1e-6:
                            # Degenerate when _d is ~0: the axis endpoint sits ON the centre,
                            # so the curve has magnitudes but NO ORIENTATION. Harmless where
                            # the axes are the sketch axes; silently wrong on a rotated one.
                            rec["orientation_unrecorded"] = True
                            rec["major_end_length_mm"] = round(_d, 9)
                else:
                    # Splines and conics: record the defining points so the shape can be
                    # rebuilt even where the tool has no identical curve type.
                    # Crosslink: controlPoints is a vector without .count (that raised and lost
                    # the curve), so read the points through as_items and never let them cost
                    # the exact shape that follows.
                    try:
                        fp = get(c, "fitPoints", "controlPoints")
                        if fp is not None:
                            pts = [pt2(get(q, "geometry") or q) for q in as_items(fp)]
                            pts = [q for q in pts if q]
                            if pts:
                                rec["points"] = pts
                    except Exception as e:
                        rec["points_error"] = str(e)[:120]
                    spline_exact(c, rec)
            except Exception as e:
                rec["read_error"] = str(e)
            out.append(rec)
    return out


# Where Fusion's constraints and dimensions keep the things they act on. Each subclass names
# them differently and there is no common accessor, so probe the documented names.
REF_ATTRS = (
    "line", "lineOne", "lineTwo", "curveOne", "curveTwo", "entityOne", "entityTwo",
    "entity", "point", "pointOne", "pointTwo", "midPointCurve", "symmetryLine",
    "circle", "arc", "parentCurve",
)


def token_of(obj):
    """A stable identity for a sketch entity, used to tie a constraint back to its curves."""
    try:
        t = obj.entityToken
        return t or None
    except Exception:
        return None


REF_COLLECTIONS = ("parentCurves", "childCurves", "curves", "entities", "points")


def refs_unmapped(obj):
    """The attribute names an operand-less constraint or dimension actually exposes.

    Recorded only when nothing else resolved, so a run says what to read next instead of leaving
    the next guess to be paid for with another extraction -- the same reason profile picks record
    why they could not be mapped.
    """
    out = []
    for a in dir(obj):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v) or v is None:
            continue
        out.append("%s:%s" % (a, type(v).__name__))
    return out[:40]


def refs_of(obj):
    """Tokens of whatever this constraint or dimension is attached to.

    A constraint recorded without its operands is unusable -- it says a relationship existed but
    not between what -- which is why these were captured but could never be rebuilt before.
    """
    out = []
    for attr in REF_ATTRS:
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        if v is None:
            continue
        t = token_of(v)
        if t and t not in out:
            out.append(t)
    # Operands held in a collection rather than a single-valued attribute.
    for attr in REF_COLLECTIONS:
        try:
            coll = getattr(obj, attr)
            cnt = coll.count
        except Exception:
            continue
        for i in range(cnt):
            try:
                t = token_of(coll.item(i))
            except Exception:
                continue
            if t and t not in out:
                out.append(t)
    return out


def sketch_constraints(sk):
    out = []
    try:
        cons = sk.geometricConstraints
    except Exception:
        return out
    for i in range(cons.count):
        c = cons.item(i)
        name = type(c).__name__
        name = re.sub(r"^.*?([A-Za-z]+)Constraint$", r"\1", name) or name
        rec = {"type": name.lower()}
        refs = refs_of(c)
        if refs:
            rec["refs"] = refs
        out.append(rec)
    return out


def sketch_dimensions(sk):
    out = []
    try:
        dims = sk.sketchDimensions
    except Exception:
        return out
    for i in range(dims.count):
        d = dims.item(i)
        rec = {"type": re.sub(r"^Sketch|Dimension$", "", type(d).__name__).lower()}
        try:
            p = d.parameter
            rec["expression"] = p.expression
            rec["value_mm"] = round(p.value * MM, 9)
            rec["name"] = p.name
        except Exception:
            pass
        refs = refs_of(d)
        if refs:
            rec["refs"] = refs
        else:
            rec["exposes"] = refs_unmapped(d)
        out.append(rec)
    return out


def sketch_points(sk):
    """Every sketch point with its token and position.

    Curve endpoints are covered on the curves themselves; this also catches the sketch origin
    and any free point, which is what a dimension measuring from the origin refers to.
    """
    out = []
    try:
        pts = sk.sketchPoints
    except Exception:
        return out
    for i in range(pts.count):
        p = pts.item(i)
        rec = {}
        t = token_of(p)
        if t:
            rec["token"] = t
        try:
            rec["at"] = pt2(p.geometry)
        except Exception:
            pass
        try:
            rec["construction"] = bool(get(p, "isConstruction"))
        except Exception:
            pass
        if rec:
            out.append(rec)
    return out


def describe_sketch(sk):
    rec = {
        "name": get(sk, "name"),
        "plane": None,
        "is_visible": bool(get(sk, "isVisible")),
    }
    try:
        rec["is_fully_constrained"] = bool(sk.isFullyConstrained)
    except Exception:
        pass
    try:
        ref = sk.referencePlane
        rec["plane"] = {"name": get(ref, "name"), "kind": type(ref).__name__}
    except Exception:
        pass
    try:
        t = sk.transform.asArray()
        rec["transform"] = [round(v, 9) for v in t]
    except Exception:
        pass
    if SKETCH_GEOMETRY:
        rec["curves"] = sketch_curves(sk)
        rec["points"] = sketch_points(sk)
        rec["constraints"] = sketch_constraints(sk)
        rec["dimensions"] = sketch_dimensions(sk)
        try:
            texts = sketch_texts(sk)
            if texts:
                rec["texts"] = texts
        except Exception as e:
            rec["texts_error"] = str(e)[:120]
        try:
            rec["profile_count"] = sk.profiles.count
        except Exception:
            pass
    return rec


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


def describe_occurrence(ent):
    """A placement of another design: which component, and where.

    This is what an assembly is made of, and the largest unmapped group in a real hub. The
    component's name is what ties it to the design that was migrated separately; the transform
    is where it sits.
    """
    rec = {}
    comp = get(ent, "component")
    if comp is not None:
        rec["component"] = get(comp, "name")
    for key, attr in (("full_path", "fullPathName"), ("referenced", "isReferencedComponent"),
                      ("grounded", "isGrounded"), ("visible", "isLightBulbOn")):
        v = get(ent, attr)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = v if isinstance(v, (bool, str)) else str(v)
    # The live placement first, falling back to the one it was created with.
    for attr in ("transform", "transform2", "initialTransform"):
        m = get(ent, attr)
        if m is None:
            continue
        try:
            rec["transform"] = [round(v, 9) for v in m.asArray()]
            break
        except Exception:
            continue
    try:
        rec["bodies"] = ent.bRepBodies.count
    except Exception:
        pass
    # A document reference names the design this placement came from.
    ref = get(ent, "documentReference")
    if ref is not None:
        for key, attr in (("ref_name", "referencedFileName"), ("ref_id", "id")):
            v = get(ref, attr)
            if isinstance(v, str):
                rec[key] = v
    return rec


def _face_profiles(ent):
    """An extrude's profiles when they are model FACES rather than sketch regions.

    Call inside rolled_back_to(): the faces an extrude is built on are consumed by it, so on
    the finished body they are gone. Point, area and normal -- enough for another tool to pick
    the same face. The API's `area` is cm2; everything written here is mm2, like the rest of
    this file.
    """
    out = []
    for f in as_items(get(ent, "profile")):
        if type(f).__name__ != "BRepFace":
            continue
        q = point_on(f, "pointOnFace")
        if not q:
            continue
        r = {"point": q}
        try:
            r["body"] = f.body.name                       # schema 22: whose face it is
            r["component"] = f.body.parentComponent.name
        except Exception:
            pass
        a = get(f, "area")
        if isinstance(a, float):
            r["area_mm2"] = round(a * MM * MM, 6)
        g = get(f, "geometry", "worldGeometry")
        if g is not None:
            r["surface"] = type(g).__name__
            n = get(g, "normal")
            if n is not None:
                try:
                    r["normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
                except Exception:
                    pass
        out.append(r)
    return out


def _draft_faces(ent):
    """The faces a draft or shell was given -- as POINTS and as full SIGNATURES.

    A point is not a face. Fusion's tool selects BRepFace objects; recording one point per face
    forces the consumer to work backwards from a coordinate, and that resolution is lossy -- a
    point can sit on an edge, name two faces at once ("`F3:face:42` names more than one face"),
    or land on a face a later step splits in half.

    Measured across a production part: (area, centroid) proved unique for every face on both
    bodies, so a signature identifies a face exactly where a point only
    hints at one. `solid.draft` takes face NAMES as well as points -- it says so itself,
    "`faces` must be a list of points or face names" -- so a consumer holding a signature can
    resolve it against model.faces and select the face, instead of probing geometry.

    The tangent chain is recorded per face for the same reason: every draft on that part has
    isTangentChain True, Fusion silently extends the selection along it, and solid.draft has no
    equivalent option, so a rebuild can only match the real selection if the chain travels with
    the pick.

    Called inside rolled_back_to(), so `ent`'s inputs exist. Returns the `tried` list even on
    failure: a selection that could not be read must say what was looked at.
    """
    coll, src, tried = first_nonempty(ent, "inputFaces", "faces")
    if coll is None:
        return {"points": [], "sigs": [], "source": None, "tried": tried}

    def _sig(f):
        sig = {}
        q = point_on(f, "pointOnFace")
        if q:
            sig["point"] = q
        a = get(f, "area")
        if isinstance(a, float):
            sig["area_mm2"] = round(a * MM * MM, 6)
        c = get(f, "centroid")
        if c is not None:
            try:
                sig["centroid"] = [round(c.x * MM, 6), round(c.y * MM, 6), round(c.z * MM, 6)]
            except Exception:
                pass
        g = get(f, "geometry", "worldGeometry")
        if g is not None:
            sig["surface"] = type(g).__name__
            n = get(g, "normal")
            if n is not None:
                try:
                    sig["normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
                except Exception:
                    pass
        return sig

    sigs = []
    for f in as_items(coll):
        sig = _sig(f)
        tc = get(f, "tangentiallyConnectedFaces")
        if tc is not None:
            chain = [c for c in (_sig(g2) for g2 in as_items(tc)) if c]
            if chain:
                sig["tangent_chain_faces"] = chain
        sigs.append(sig)
    return {"points": [s["point"] for s in sigs if s.get("point")],
            "sigs": sigs, "source": src, "tried": tried}


def describe_draft(ent):
    """A draft keeps its angle in draftDefinition, not on the feature itself.

    Read this rolled back wherever possible. The neutral plane is a BRepFace of the model
    (often a sliver face of a couple of mm2), and on the finished body it does not resolve at
    all -- `plane` comes back None and the record silently loses the one thing a rebuild needs
    to orient the draft.
    """
    rec = {}
    for holder in (get(ent, "draftDefinition"), ent):
        if holder is None:
            continue
        a = expr_of(holder, "angle", "draftAngle", "angleOne")
        if a:
            rec["angle"] = a
            break
    plane = get(ent, "plane")
    if plane is not None:
        q = point_on(plane, "pointOnFace")
        if q:
            rec["neutral_point"] = q
        # The pull is the neutral face's own normal, so record it rather than making the
        # consumer re-derive it from a point.
        g = get(plane, "geometry", "worldGeometry")
        n = get(g, "normal") if g is not None else None
        if n is not None:
            try:
                rec["neutral_normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
            except Exception:
                pass
        a = get(plane, "area")
        if isinstance(a, float):
            rec["neutral_area_mm2"] = round(a * MM * MM, 6)
    for key, attr in (("draft_type", "draftType"), ("flipped", "isDirectionFlipped"),
                      ("tangent_chain", "isTangentChain"),
                      ("parting_line_type", "partingLineType")):
        v = get(ent, attr)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = str(v)
    got = _draft_faces(ent)
    if got["points"]:
        rec["face_points"] = got["points"]
        rec["face_pick_source"] = got["source"]
    # SCHEMA 16: the signatures, which are what actually identify the faces. `face_points` is
    # kept so older consumers keep working, but a rebuild should prefer these -- (area,
    # centroid) is a unique key per body and resolves to a face NAME, which solid.draft
    # accepts directly.
    if got.get("sigs"):
        rec["face_sigs"] = got["sigs"]
    # Which bodies the draft was applied to. On this part `bodies` is populated only when
    # rolled back, and a body draft is a different rebuild from a face draft -- it tilts every
    # qualifying face rather than a named set, so the distinction has to survive.
    bcoll, bsrc, _ = first_nonempty(ent, "bodies", "inputBodies")
    if bcoll is not None:
        items = as_items(bcoll)
        rec["body_count"] = len(items)
        rec["body_source"] = bsrc
        areas = []
        for b in items:
            a = get(b, "area")
            if isinstance(a, float):
                areas.append(round(a * MM * MM, 6))
        if areas:
            rec["body_areas_mm2"] = areas
    return rec


def matrix_of(ent, *names):
    for n in names:
        m = get(ent, n)
        if m is None:
            continue
        try:
            return [round(v, 9) for v in m.asArray()]
        except Exception:
            continue
    return None


def describe_feature(ent, index=None):
    """Pull the parametric intent out of one timeline entity."""
    kind = type(ent).__name__
    rec = {"entity": kind, "name": get(ent, "name")}

    # The driving expressions, wherever this feature keeps them.
    for key, names in (
        ("distance", ("distanceOne", "distance")),
        ("distance_two", ("distanceTwo",)),
        ("taper", ("taperAngleOne", "taperAngle")),
        ("angle", ("angle", "draftAngle", "angleOne")),
        ("radius", ("radius",)),
        ("thickness", ("insideThickness", "thickness")),
    ):
        v = expr_of(ent, *names)
        if v:
            rec[key] = v

    # Extent / operation style, which decides how the feature is re-created.
    for key, names in (
        ("operation", ("operation",)),
        ("extent_type", ("extentType",)),
        ("is_symmetric", ("isSymmetric",)),
        ("direction", ("extentDirection",)),
    ):
        v = get(ent, *names)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = str(v)

    # The *second* side of a two-sided extrude. Reading only extentOne silently halves the
    # feature: a body 10 mm up and 30 mm back from an offset start, so taking extentOne
    # alone builds it 10 mm tall from the sketch plane instead of 40 mm spanning
    # z 0..40 -- the right expression, the wrong solid, and nothing in the record says so.
    ext2 = get(ent, "extentTwo")
    if ext2 is not None:
        rec["two_sided"] = True
        rec["extent_two_definition"] = type(ext2).__name__
        d2 = expr_of(ext2, "distance")
        if d2:
            rec["distance_two"] = d2
        off2 = expr_of(ext2, "offset")
        if off2:
            rec["to_offset_two"] = off2

    # Extrudes keep their extent in a definition object on newer builds.
    ext = get(ent, "extentOne", "extentDefinition")
    if ext is not None:
        d = expr_of(ext, "distance")
        if d:
            rec.setdefault("distance", d)
        rec["extent_definition"] = type(ext).__name__
        # "To object" extrudes have no distance at all -- they run until they reach something.
        # A point on that target is enough to say the same thing elsewhere, and unlike a measured
        # distance it keeps the intent: the extrude still lands on the face, not near where the
        # face used to be.
        target = get(ext, "entity", "toEntity", "target")
        if target is not None:
            q = point_on(target, "pointOnFace") or point_on(target, "pointOnEdge")
            if q is None:
                g = get(target, "geometry", "worldGeometry")
                q = pt(g) if g is not None else None
            if q:
                rec["to_point"] = q
        off = expr_of(ext, "offset")
        if off:
            rec["to_offset"] = off

        # Read rolled back when the finished body could not answer. A "to object" extrude
        # terminates on a FACE, and later features reshape or remove that face -- on
        # a to-object extrude runs to the top face of an earlier body, which later cuts
        # have since carved into, so `ext.entity` resolves to nothing by the end
        # and the step recorded an extent type with no target at all. The replay then had
        # no distance and no face: that step DID NOT BUILD, and every later step
        # inherited a negative volume. Same failure shape as the drafts above: the question was
        # asked of the wrong state, not of the wrong attribute.
        if "ToEntity" in rec["extent_definition"] and not rec.get("to_point"):
            def _late():
                e2 = get(ent, "extentOne", "extentDefinition")
                if e2 is None:
                    return None
                t2 = get(e2, "entity", "toEntity", "target")
                if t2 is None:
                    return None
                p2 = point_on(t2, "pointOnFace") or point_on(t2, "pointOnEdge")
                if p2 is None:
                    g2 = get(t2, "geometry", "worldGeometry")
                    p2 = pt(g2) if g2 is not None else None
                if not p2:
                    return None
                out = {"point": p2, "target_type": type(t2).__name__}
                g2 = get(t2, "geometry", "worldGeometry")
                if g2 is not None:
                    out["target_surface"] = type(g2).__name__
                    n2 = get(g2, "normal")
                    if n2 is not None:
                        try:
                            out["target_normal"] = [round(n2.x, 9), round(n2.y, 9),
                                                    round(n2.z, 9)]
                        except Exception:
                            pass
                return out
            late = rolled_back_to(index, _late)
            if late:
                rec["to_point"] = late["point"]
                rec["to_target_type"] = late.get("target_type")
                if late.get("target_surface"):
                    rec["to_target_surface"] = late["target_surface"]
                if late.get("target_normal"):
                    rec["to_target_normal"] = late["target_normal"]
                rec["to_point_via_rollback"] = True
            else:
                # An extent type that says "runs to something" with no something recorded is
                # exactly the silent gap this file exists to stop. Say so in the record.
                rec["to_point_missing"] = True

    # Where the extrude *starts*. Without this a feature lands on the sketch plane, which can
    # be the wrong place while still producing exactly the right volume -- a fill feature
    # adds the same volume whether it sits at z=0 or at an offset, so no volume check catches
    # it. What does catch it is the face it should have widened: that top face measures far
    # less when the fill lands low than when it lands where it belongs, and the chamfer that
    # follows then has nothing wide enough to cut into.
    start = get(ent, "startExtent", "startDefinition")
    if start is not None:
        rec["start_definition"] = type(start).__name__
        s_off = expr_of(start, "offset")
        if s_off:
            rec["start_offset"] = s_off
        s_target = get(start, "entity", "face", "target")
        if s_target is not None:
            q = point_on(s_target, "pointOnFace") or point_on(s_target, "pointOnEdge")
            if q is None:
                g = get(s_target, "geometry", "worldGeometry")
                q = pt(g) if g is not None else None
            if q:
                rec["start_point"] = q

    # What the feature was applied to. Fusion's internal edge and face ids mean nothing in
    # another tool, but a *point lying on* each edge or face is geometry, and geometry travels.
    # Most CAD tools (SolidCraft included) select exactly that way, so this is the bridge that
    # lets a fillet or a shell be re-created rather than merely reported as missing.
    sets = edge_sets(ent)
    if not sets and ("Fillet" in kind or "Chamfer" in kind):
        # Its own operation removed these edges, so they cannot be found in the finished body.
        sets = rolled_back_to(index, lambda: edge_sets(ent)) or []
        if sets:
            rec["edges_via_rollback"] = True
    if sets:
        rec["edge_sets"] = sets
        rec["edge_count"] = sum(len(s["edge_points"]) for s in sets)
        # Flattened too, so a consumer that only wants "every edge" need not walk the sets.
        rec["edge_points"] = [p for s in sets for p in s["edge_points"]]
    else:
        edges = get(ent, "edges")
        if edges is not None:
            rec["edge_count"] = count_of(edges)
        if "Fillet" in kind or "Chamfer" in kind:
            rec["edge_points_missing"] = True
            sizes = rolled_back_to(index, lambda: [
                {k: v for k, v in st.items() if k != "edge_points"} for st in edge_sets(ent)])
            if sizes:
                rec["edge_set_sizes"] = sizes

    # `inputFaces` BEFORE `faces`, and by whether it holds anything rather than whether it is
    # not None. A DraftFeature's `faces` is an empty-but-not-None BRepFaces, so the old
    # `get(ent, "faces", "inputFaces")` always answered the empty one and never looked at
    # inputFaces, where the picks are. Measured on a production design: every draft recorded
    # `face_count: 0` while every one of them had a populated `inputFaces` BRepFaceVector.
    faces, src, tried = first_nonempty(ent, "inputFaces", "faces")
    if faces is not None:
        pts = picks(faces, "pointOnFace")
        rec["face_count"] = len(pts) if pts else count_of(faces)
        if pts:
            rec["face_points"] = pts
            rec["face_pick_source"] = src

    if "Draft" in kind or "Shell" in kind:
        # ALWAYS roll these back, even when the finished body did answer. The feature reshaped
        # the faces it was given, so a point read afterwards lies on the tilted face, not the
        # one that was picked -- a fraction of a millimetre off on a shallow draft, enough to select
        # a neighbour. The rolled-back answer is the one the feature actually saw, so it wins.
        back = rolled_back_to(index, lambda: _draft_faces(ent))
        if back and back.get("points"):
            if rec.get("face_points") and back["points"] != rec["face_points"]:
                rec["face_points_final"] = rec["face_points"]   # kept, so the shift is visible
            rec["face_points"] = back["points"]
            rec["face_count"] = len(back["points"])
            rec["faces_via_rollback"] = True
            rec["face_pick_source"] = back.get("source")
            # The signatures must come from the SAME rolled-back read as the points, for the
            # reason above: after the feature runs, the faces have moved.
            if back.get("sigs"):
                rec["face_sigs"] = back["sigs"]
        elif not rec.get("face_points"):
            # Nothing, either way. Say what was looked at -- an empty selection that reports
            # success is the failure mode this whole block exists to stop.
            rec["face_points_missing"] = True
            rec["face_pick_tried"] = (back or {}).get("tried") or tried

    prof = get(ent, "profile")
    prof_back = None
    if prof is None and index is not None and hasattr(type(ent), "profile"):
        # Schema 22: an extrude made by pulling a face of the body (Press Pull) has that face
        # as its profile, and the face no longer exists once the extrude has used it: in the
        # finished model the profile cannot be read at all. 171 of 813 extrudes in schema 21
        # came out without any profile for that reason. Read it with the model rolled back.
        def _profile_before():
            p2 = get(ent, "profile")
            if p2 is None:
                return None
            info2, unmapped2 = profile_picks(p2)
            return {"count": count_of(p2), "info": info2, "unmapped": unmapped2,
                    "faces": _face_profiles(ent)}
        prof_back = rolled_back_to(index, _profile_before)
        if prof_back:
            rec["profile_read_rolled_back"] = True
    if prof is not None or prof_back:
        # The feature's OWN profile count, always -- never the number that happened to be
        # recorded. profile_picks drops any profile whose point it cannot compute, and taking
        # the count from its result made a partial record indistinguishable from a complete
        # one: a rebuild then selects too few regions and the extrude comes out light with
        # nothing saying why.
        if prof_back:
            info, unmapped = prof_back["info"], prof_back["unmapped"]
            rec["profile_count"] = prof_back["count"]
        else:
            info, unmapped = profile_picks(prof)
            rec["profile_count"] = count_of(prof)
        if info:
            rec["profiles"] = info
            rec["profiles_recorded"] = len(info)
            if rec["profile_count"] and len(info) != rec["profile_count"]:
                rec["profiles_incomplete"] = True
        # Whatever could NOT be read. Written whether or not anything else was: a step with
        # some picks and some unmapped ones is the case most likely to be misread as complete.
        if unmapped:
            rec["profiles_unmapped"] = unmapped
            # A profile that is a FACE, not a sketch region, has no sketch to take a 2D point
            # in, so profile_picks drops it. It still has to be selectable, and a point on the
            # face plus its normal is how another tool finds it. Read rolled back: the faces an
            # extrude was built ON are consumed by it, and `ent.faces` on the finished part is
            # the faces the feature CREATED -- a different set entirely, and reading it here is
            # what made a face-based extrude select nothing while reporting a profile count.
            if any((u.get("type") == "BRepFace") for u in unmapped):
                pf = (prof_back["faces"] if prof_back
                      else rolled_back_to(index, lambda: _face_profiles(ent)))
                if pf:
                    rec["profile_faces"] = pf
                    rec["profile_faces_recorded"] = len(pf)
                else:
                    rec["profile_faces_missing"] = True

    try:
        rec["suppressed"] = bool(ent.isSuppressed)
    except Exception:
        pass

    # Feature-specific geometry that would otherwise be unrecoverable.
    if "Revolve" in kind:
        a = axis_of(ent)
        if a:
            rec["axis"] = a
    if "Sweep" in kind:
        p = path_of(ent)
        if p:
            rec["path_points"] = p
    if "Thread" in kind:
        info = {}
        for key, names in (("designation", ("threadDesignation",)),
                           ("type", ("threadType",)),
                           ("class", ("threadClass",)),
                           ("is_internal", ("isInternal",)),
                           ("full_length", ("isFullLength",))):
            v = get(ent, *names)
            if v is not None and not hasattr(v, "objectType"):
                info[key] = str(v)
        d = expr_of(ent, "threadLength")
        if d:
            info["length"] = d
        if info:
            rec["thread"] = info

    if "Draft" in kind and not rec.get("angle"):
        a = rolled_back_to(index, lambda: expr_of(ent, "angle", "draftAngle", "angleOne"))
        if a:
            rec["angle"] = a

    # Sketches carry their whole contents.
    if kind == "Sketch":
        rec["sketch"] = describe_sketch(ent)

    if kind == "Occurrence":
        rec.update(describe_occurrence(ent))
    elif "Draft" in kind:
        # ALWAYS merge the rolled-back reading, not only when the angle is missing. On the
        # finished body a draft answers its angle happily while `plane` and `bodies` come back
        # None, so keying the rollback off the angle meant the neutral plane -- which is what
        # orients the whole feature -- was never recorded on any draft of this part.
        d = describe_draft(ent)
        back = rolled_back_to(index, lambda: describe_draft(ent)) or {}
        for k, v in back.items():
            if k not in d:
                d[k] = v
                d["draft_via_rollback"] = True
        rec.update(d)
    elif kind == "MoveFeature":
        m = matrix_of(ent, "transform")
        if m:
            rec["transform"] = m
    elif "Thread" in kind:
        info = get(ent, "threadInfo")
        if info is not None:
            t = rec.setdefault("thread", {})
            for key, attr in (("designation", "threadDesignation"), ("size", "threadSize"),
                              ("type", "threadType"), ("class", "threadClass"),
                              ("pitch", "threadPitch")):
                v = get(info, attr)
                if isinstance(v, (str, int, float)):
                    t[key] = v
        for key, names in (("length", ("threadLength",)), ("offset", ("threadOffset",))):
            v = expr_of(ent, *names)
            if v:
                rec.setdefault("thread", {})[key] = v
        pts = picks(get(ent, "inputCylindricalFaces") or [], "pointOnFace")
        if not pts:
            one = get(ent, "inputCylindricalFace")
            q = point_on(one, "pointOnFace") if one is not None else None
            pts = [q] if q else []
        if pts:
            rec["thread_faces"] = pts
    elif kind == "CombineFeature":
        tools = get(ent, "toolBodies")
        if tools is not None:
            rec["tool_body_count"] = count_of(tools)
        v = get(ent, "isKeepToolBodies")
        if v is not None:
            rec["keep_tools"] = bool(v)
    elif "Emboss" in kind:
        d = expr_of(ent, "depth")
        if d:
            rec["depth"] = d

    if PROBE_UNMAPPED and kind not in MAPPED_KINDS:
        rec["probe"] = probe(ent)

    return rec



# Feature kinds we already translate; anything else gets probed instead.
MAPPED_KINDS = {
    "Sketch", "ExtrudeFeature", "FilletFeature", "ChamferFeature", "ShellFeature",
    "RevolveFeature", "OffsetFacesFeature", "Snapshot", "Canvas",
    "ConstructionPlane", "ConstructionAxis", "ConstructionPoint",
}
# Occurrence is deliberately absent: assemblies are the largest unmapped group here, and an
# occurrence is a placement of *another design*, so what it exposes -- which design, and where --
# is exactly what needs probing.

# Never call these: they recompute, mutate, or walk the whole model.
PROBE_SKIP = ("delete", "save", "compute", "update", "parent", "nativeObject", "createForAssembly",
              "timelineObject", "healthState", "errorOrWarningMessage", "attributes")


def probe(ent):
    """Name every readable property of a feature, and say what kind of thing it holds.

    Values are deliberately not captured -- only shape -- so this stays cheap and cannot drag in
    half the document. It is enough to answer "where does a draft keep its angle", which is the
    question that has cost the most time here.
    """
    out = {}
    for name in dir(ent):
        if name.startswith("_") or any(k in name for k in PROBE_SKIP):
            continue
        try:
            v = getattr(ent, name)
        except Exception:
            continue
        if v is None or callable(v):
            continue
        try:
            tname = type(v).__name__
            if isinstance(v, (int, float, str, bool)):
                out[name] = "{}={}".format(tname, str(v)[:40])
            elif hasattr(v, "count"):
                out[name] = "{}[{}]".format(tname, v.count)
            elif hasattr(v, "expression"):
                out[name] = "{} expr={}".format(tname, str(v.expression)[:30])
            else:
                out[name] = tname
        except Exception:
            continue
        if len(out) >= 40:
            break
    return out


def describe_design(design, data_file, parts):
    doc = {
        "schema": SCHEMA,
        "name": get(data_file, "name"),
        "id": get(data_file, "id"),
        "version": get(data_file, "versionNumber"),
        "path": "/".join(parts),
        "units": None,
        "parameters": [],
        "timeline": [],
        "components": [],
        "stats": {},
    }
    try:
        doc["units"] = design.unitsManager.defaultLengthUnits
    except Exception:
        pass

    # Parameters first: the timeline's expressions refer to them by name.
    try:
        for i in range(design.userParameters.count):
            p = design.userParameters.item(i)
            doc["parameters"].append({
                "name": p.name,
                "expression": p.expression,
                "unit": get(p, "unit"),
                "value": round(p.value, 9),
                "comment": get(p, "comment") or "",
            })
    except Exception as e:
        doc["parameters_error"] = str(e)

    # User parameters routinely refer to *model* parameters -- Fusion's auto-named dimensions
    # like "d4" ("outerDiameter = d4 + 10 mm"). Those are driven by features rather than set
    # by hand, so they cannot be re-created as inputs elsewhere; capture their values so a
    # translator can at least resolve the reference instead of emitting an undefined name.
    try:
        user_names = {p["name"] for p in doc["parameters"]}
        model = []
        allp = design.allParameters
        for i in range(allp.count):
            p = allp.item(i)
            name = get(p, "name")
            if not name or name in user_names:
                continue
            model.append({
                "name": name,
                "expression": get(p, "expression"),
                "unit": get(p, "unit"),
                "value": round(p.value, 9),
            })
        doc["model_parameters"] = model
    except Exception as e:
        doc["model_parameters_error"] = str(e)

    try:
        for i in range(design.allComponents.count):
            c = design.allComponents.item(i)
            doc["components"].append({
                "name": get(c, "name"),
                "bodies": c.bRepBodies.count,
                "sketches": c.sketches.count,
                "features": c.features.count,
            })
    except Exception:
        pass

    unknown = {}
    try:
        tl = design.timeline
        _ROLLBACK["timeline"] = tl
        # Crosslink 2026-11-14: a design with mesh features (MeshConvert, MeshReduce...) is never
        # rolled back. Moving the marker makes Fusion redo the mesh conversion on the way back,
        # which took over 20 minutes at 100 % CPU on one 135-body design, and such a design has
        # no translatable history anyway (it goes the STEP route).
        _ROLLBACK["skip"] = None
        _ROLLBACK["min_index"] = 0
        mesh_base = None
        try:
            opened = expand_groups(tl)
            if opened:
                doc["groups_expanded"] = opened
        except Exception as e:
            doc["groups_error"] = str(e)[:120]
        try:
            kinds, _names = _B.timeline_kinds(tl)
            last = max((i for i, n in enumerate(kinds) if _B.is_mesh_step(n)), default=None)
            if last is not None:
                n = kinds[last]
                # Crosslink 2026-11-21 (schema 26; the designs that count are those that were
                # worked on in Fusion): many designs start from an STL that was converted
                # and then really modelled (offsets, splits, holes). When every mesh step comes
                # before the modelling and no mesh body is left at the end, the marker is only
                # moved behind the last mesh step, so the mesh steps are never redone. The state
                # after that step is written as the design's starting body ("mesh base"); the
                # steps after it are read like any other timeline. Steps before it that make
                # bodies are covered by that state. Anything else stays on the STEP route.
                # The decision itself is f2s_buckets.mesh_base, the rule InventoryHub sorts with.
                try:
                    left_meshes = len(meshes_now(design))
                except Exception:
                    left_meshes = None
                base, why = _B.mesh_base(kinds, left_meshes)
                if MESH_BASE and base:
                    mesh_base = last
                    _ROLLBACK["min_index"] = last + 1
                    doc["mesh_base"] = base
                else:
                    _ROLLBACK["skip"] = n
                    doc["rollback_skipped"] = "mesh feature in the timeline ({})".format(n)
                    if left_meshes and any(_B.is_work(k) for k in kinds[last + 1:]):
                        doc["mesh_base_refused"] = why
        except Exception:
            pass
        t_design = time.time()
        measuring = MEASURE_EACH_STEP
        wanted = wanted_steps() | _CHUNK.get("keep", set())
        if wanted:
            doc["states_wanted"] = len(wanted)
        _CHUNK.update(left=STATE_CHUNK, pending=0, new=0, reused=0)
        prev = {"bodies": [], "meshes": []}   # as they stood after the step before
        del _PENDING_EXTRA[:]
        _EXPORT_S["s"] = 0.0
        for i in range(tl.count):
            if i % 50 == 49:
                mem_over()
            tobj = tl.item(i)
            rec = {"index": i}
            try:
                rec["name"] = tobj.name
            except Exception:
                pass
            try:
                rec["rolled_back"] = not tobj.isSnapshot and False
            except Exception:
                pass
            ent = None
            if get(tobj, "isGroup"):
                # the group marker itself: its steps follow as ordinary timeline items
                rec["entity"] = "TimelineGroup"
                try:
                    rec["group_count"] = tobj.count
                except Exception:
                    pass
            try:
                ent = None if rec.get("entity") == "TimelineGroup" else tobj.entity
            except Exception as e:
                rec["entity_error"] = str(e)
            if ent is not None:
                del _TOO_BIG[:]
                try:
                    rec.update(describe_feature(ent, i))
                except Exception as e:
                    rec["describe_error"] = "{}: {}".format(type(e).__name__, e)
                    rec["entity"] = type(ent).__name__
                if _TOO_BIG:
                    rec["big_collections_skipped"] = sorted(set(_TOO_BIG))
            # What the model measures with the marker just after this step. This is the other
            # half of a lockstep check: a rebuild can roll its own timeline to the same
            # position and compare, which says *which* feature diverged instead of leaving a
            # wrong final volume with dozens of candidate causes.
            #
            # It is also the only reference that catches a feature built in the wrong place,
            # because the z extent moves even when the volume does not.
            got_x = False
            if measuring:
                def after_step(ent=ent, rec=rec):
                    # Schema 25: the body list first, the totals out of it; where a step's time
                    # goes is written down (`t`, seconds).
                    out, t = {}, {}
                    t0 = time.time()
                    try:
                        out["bodies"] = bodies_now(design)
                    except Exception:
                        pass
                    t["bodies"] = round(time.time() - t0, 2)
                    t0 = time.time()
                    m = measure_from(out["bodies"]) if isinstance(out.get("bodies"), list) else None
                    out["m"] = m if m is not None else measure(design)
                    t["measure"] = round(time.time() - t0, 2)
                    out["t"] = t
                    if ent is not None:
                        try:
                            out["x"] = feature_extra(ent)
                        except Exception as e:
                            out["x"] = {"error": str(e)[:120]}
                    # Schema 22: which solid bodies did this step make or change?
                    keys = None
                    if isinstance(out.get("bodies"), list) and isinstance(prev["bodies"], list):
                        keys = changed_body_keys(prev["bodies"], out["bodies"])
                    kind = type(ent).__name__ if ent is not None else ""
                    t0 = time.time()
                    if mesh_base is not None and i == mesh_base:
                        # schema 26: the whole model after the last mesh step, every solid body
                        allk = {(b.get("component"), b.get("name")) for b in out.get("bodies") or []
                                if b.get("solid") is not False}
                        out["base"] = export_state_step(design, i, None, allk or None)
                        out["base"]["frozen"] = True
                        out["base"]["mesh_base"] = True
                        t["export"] = round(time.time() - t0, 2)
                    elif kind in ("BaseFeature", "DeriveFeature"):
                        out["base"] = export_state_step(design, i, ent, keys)
                        t["export"] = round(time.time() - t0, 2)
                    elif i in wanted:
                        had = _CHUNK["old"].get(i)
                        if keys and had and had[0] == rec.get("name"):
                            out["base"] = dict(had[1], reused=True)
                            _CHUNK["reused"] += 1
                        elif keys and (_CHUNK["left"] <= 0 or _CHUNK["stop"] is not None):
                            _CHUNK["pending"] += 1      # next visit
                        elif keys:
                            out["base"] = export_state_step(design, i, None, keys)
                            out["base"]["frozen"] = True
                            t["export"] = round(time.time() - t0, 2)
                            _CHUNK["left"] -= 1
                            _CHUNK["new"] += 1
                            _CHUNK["n"] += 1
                            if _CHUNK["n"] % 10 == 0:
                                mem_over()
                        else:
                            out["base"] = {"frozen": True,
                                           "unchanged": keys is not None, "unknown": keys is None}
                    try:
                        out["meshes"] = meshes_now(design)
                        if out["meshes"] or prev["meshes"]:
                            got = export_new_meshes(design, i, prev["meshes"], out["meshes"])
                            if got:
                                out["mesh_files"] = got
                    except Exception as e:
                        out["mesh_error"] = str(e)[:120]
                    return out
                t_step = time.time()
                r = rolled_back_to(i + 1, after_step)
                if r:
                    if r.get("t"):
                        rec["t"] = dict(r["t"], step=round(time.time() - t_step, 2))
                    if r.get("m"):
                        rec["state"] = r["m"]
                    if r.get("bodies") is not None:
                        rec["bodies_after"] = r["bodies"]
                    if r.get("base"):
                        rec["state_step"] = r["base"]
                    if r.get("mesh_files"):
                        rec["mesh_files"] = r["mesh_files"]
                    if r.get("meshes"):
                        rec["meshes_after"] = len(r["meshes"])
                    prev["bodies"] = r.get("bodies") if isinstance(r.get("bodies"), list) else None
                    prev["meshes"] = r.get("meshes") or []
                    if "x" in r:
                        got_x = True
                        if r["x"]:
                            rec["x"] = r["x"]
                else:
                    prev["bodies"] = None
                if (mesh_base is not None and i < mesh_base and rec.get("entity") not in MESH_BASE_PASSIVE
                        and rec.get("entity") != "TimelineGroup"):
                    # schema 26: what this step made is in the mesh base state
                    rec["state_step"] = {"frozen": True, "covered_by": mesh_base}
                # Crosslink 2026-11-14: per-step measuring recomputes the rest of the timeline
                # for every step. Past the budget it stops for this design; the feature inputs
                # (which need their own short rollbacks) are still read.
                # Schema 22: writing state files is not measuring; its time does not count.
                if time.time() - t_design - _EXPORT_S["s"] > MEASURE_BUDGET_S:
                    measuring = False
                    doc["measure_stopped_at_step"] = i

            if ent is not None and type(ent).__name__ not in X_NO_INPUT_READ:
                try:
                    ins = (rolled_back_to(i, lambda ent=ent: feature_inputs(ent))
                           if time.time() - t_design < INPUT_BUDGET_S else None)
                    if ins:
                        rec.setdefault("x", {})["inputs"] = ins
                except Exception as e:
                    rec.setdefault("x", {})["inputs_error"] = str(e)[:120]

            if not got_x and ent is not None:
                # No rollback for this step (mesh design, budget used up): read the extras in the
                # finished model. Inputs are still right; results a later step consumed are not.
                try:
                    x = feature_extra(ent)
                    if x:
                        x["read_in_final_state"] = True
                        rec["x"] = x
                except Exception as e:
                    rec["x"] = {"error": str(e)[:120]}

            # Schema 22: which component the step belongs to (a sketch or feature made while a
            # component was active lives in that component).
            if ent is not None and "parent_component" not in rec:
                try:
                    pc = ent.parentComponent
                    if pc is not None:
                        rec["parent_component"] = pc.name
                except Exception:
                    pass

            k = rec.get("entity", "unknown")
            unknown[k] = unknown.get(k, 0) + 1
            doc["timeline"].append(rec)
    except Exception as e:
        doc["timeline_error"] = str(e)

    if _CHUNK["pending"]:
        doc["states_pending"] = _CHUNK["pending"]
    doc["states_visit"] = {"new": _CHUNK["new"], "reused": _CHUNK["reused"],
                           "fusion_mem_gb": fusion_mem_gb()}

    # Schema 22: the finished design with every body switched on, as one STEP file
    # (<design>.basefinal.step), and its mesh bodies as STL. The safety export of 14 Nov wrote
    # what was visible, so bodies that are hidden in a design are in no STEP file so far; this
    # one holds them too. For a design without a timeline it is the whole migration.
    try:
        fin = export_state_step(design, "final", None)
        if fin:
            doc["final_state"] = fin
        fb = bodies_now(design)          # every body, hidden ones too, to check an import by
        if isinstance(fb, list):
            doc["final_bodies"] = fb
        fm = export_new_meshes(design, "final", [], meshes_now(design))
        if fm:
            doc["final_meshes"] = fm
    except Exception as e:
        doc["final_state_error"] = str(e)[:160]

    # Schema 22: bodies a STEP export left out, written from their copies now that the timeline
    # is read and the marker is back at its end.
    try:
        extra = flush_extra_exports()
        if extra:
            doc["extra_exports"] = extra
    except Exception as e:
        doc["extra_exports"] = {"error": str(e)[:160]}

    doc["stats"] = {
        "export_seconds": round(_EXPORT_S["s"], 1),
        "timeline_steps": len(doc["timeline"]),
        "parameters": len(doc["parameters"]),
        "feature_types": unknown,
    }
    return doc


# ---------------------------------------------------------------------------

def collect(folder, rel_parts, found, stats=None):
    """Walk a folder for designs, recording what was seen so a miss can be explained."""
    if stats is None:
        stats = {}
    try:
        n = folder.dataFiles.count
    except Exception as e:
        stats.setdefault("folder_errors", []).append("{}: {}".format("/".join(rel_parts), e))
        return
    stats["folders"] = stats.get("folders", 0) + 1
    for i in range(n):
        try:
            f = folder.dataFiles.item(i)
        except Exception:
            continue
        stats["files"] = stats.get("files", 0) + 1
        try:
            ext = f.fileExtension
        except Exception as e:
            stats.setdefault("ext_errors", []).append(str(e))
            continue
        exts = stats.setdefault("extensions", {})
        exts[ext or "(none)"] = exts.get(ext or "(none)", 0) + 1
        if ext == "f3d":
            found.append((f, list(rel_parts)))
    try:
        for i in range(folder.dataFolders.count):
            sub = folder.dataFolders.item(i)
            collect(sub, rel_parts + [safe(sub.name)], found, stats)
    except Exception as e:
        stats.setdefault("folder_errors", []).append("{}: {}".format("/".join(rel_parts), e))


# Crosslink 2026-11-15 (night): set to True by the CrosslinkNightRun add-in, which starts this script
# without anyone at the screen. A message box would then wait for a click that never comes, so
# the text goes to OUT_DIR/last-message.txt instead.
QUIET = False


def _say(ui, text):
    if QUIET or ui is None:
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(os.path.join(OUT_DIR, "last-message.txt"), "w", encoding="utf-8") as fh:
                fh.write("{}\n{}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            pass
        return None
    return ui.messageBox(text)


class _Planned(object):
    """A design taken from InventoryHub's inventory.json; opened by its ID when its turn comes."""

    fileExtension = "f3d"

    def __init__(self, r):
        self.name = r.get("name")
        self.id = r.get("id")
        self.versionNumber = r.get("version")
        self.bucket = r.get("bucket")


def from_inventory(hubs, app):
    """-> (designs, note). designs: [(_Planned, parts)]; empty with a note when the hub must be walked."""
    if not USE_INVENTORY:
        return [], "USE_INVENTORY = False"
    if not os.path.exists(INVENTORY):
        return [], "no inventory at {} (run InventoryHub, step 1)".format(INVENTORY)
    if not hasattr(app.data, "findFileById"):
        return [], "this Fusion cannot open designs by ID"
    try:
        with open(INVENTORY, encoding="utf-8") as fh:
            inv = json.load(fh)
    except Exception as e:
        return [], "inventory unreadable ({})".format(e)
    if (inv.get("format") or 1) < 2:
        return [], "inventory from an older InventoryHub; run the new one once"
    names = {safe(get(h, "name") or "") for h in hubs}
    out = []
    for r in inv.get("designs") or []:
        if (r.get("ext") or "").lower() != "f3d" or not r.get("id") or not r.get("path"):
            continue
        parts = r["path"].split("/")
        if parts[0] not in names:
            continue
        out.append((_Planned(r), parts[:-1]))
    if not out:
        return [], "the inventory holds no designs of the hub(s) {}".format(", ".join(sorted(names)))
    return out, "read from the inventory of {}".format(inv.get("listed_at") or "?")


def bucket_of(info):
    """The bucket of a design as just read, by the inventory's rule (to notice a changed design)."""
    tl = info.get("timeline") or []
    kinds = [s.get("entity") or "" for s in tl]
    return _B.classify({"ext": "f3d", "parametric": bool(kinds) or None, "kinds": kinds,
                        "names": [s.get("name") or "" for s in tl],
                        "meshes_left": len(info.get("final_meshes") or [])})["bucket"]


def run(context):
    global _B, MESH_BASE_PASSIVE
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            _B = load_buckets()
            MESH_BASE_PASSIVE = _B.PASSIVE
        except Exception as e:
            _say(ui, str(e))
            return

        signed_in = getattr(app.data, "isSignedIn", None)
        if signed_in is False:
            _say(ui, "Not signed in to Autodesk. Sign in, then run this again.")
            return

        try:
            hubs = []
            if ALL_HUBS:
                for i in range(app.data.dataHubs.count):
                    hubs.append(app.data.dataHubs.item(i))
            else:
                hubs.append(app.data.activeHub)
            hubs = [h for h in hubs if h is not None]
        except Exception as e:
            _say(ui, "Could not reach your Autodesk hub ({}).\nSign in and try again.".format(e))
            return
        if not hubs:
            _say(ui, "No Autodesk hub available. Sign in and try again.")
            return

        stats = {"hubs": [], "projects": 0, "project_errors": []}
        designs, source = from_inventory(hubs, app)
        stats["source"] = source
        # Without a usable inventory: walk the hub, as before.
        for hub in ([] if designs else hubs):
            if len(hubs) > 1:
                try:
                    app.data.activeHub = hub
                except Exception:
                    pass
            adsk.doEvents()
            stats["hubs"].append(get(hub, "name") or "(unnamed)")
            try:
                projects = app.data.dataProjects
                pcount = projects.count
            except Exception as e:
                stats["project_errors"].append("listing projects: {}".format(e))
                continue
            stats["projects"] += pcount
            for p in range(pcount):
                try:
                    proj = projects.item(p)
                    root = proj.rootFolder
                except Exception as e:
                    stats["project_errors"].append("project {}: {}".format(p, e))
                    continue
                collect(root, [safe(hub.name), safe(get(proj, "name") or "project")],
                        designs, stats)

        if not designs:
            # Say what was actually seen. "No designs found" with no evidence is the kind of
            # answer that sends you looking in the wrong place.
            exts = stats.get("extensions") or {}
            lines = [
                "No Fusion designs (.f3d) found.",
                "",
                "hub(s):   {}".format(", ".join(stats["hubs"]) or "none"),
                "projects: {}".format(stats["projects"]),
                "folders:  {}".format(stats.get("folders", 0)),
                "files:    {}".format(stats.get("files", 0)),
                "file types seen: {}".format(
                    ", ".join("{} x{}".format(k, v) for k, v in sorted(exts.items()))
                    or "none"),
            ]
            for key, label in (("project_errors", "project errors"),
                               ("folder_errors", "folder errors"),
                               ("ext_errors", "file errors")):
                errs = stats.get(key) or []
                if errs:
                    lines += ["", "{}:".format(label)] + ["  " + e for e in errs[:5]]
                    if len(errs) > 5:
                        lines.append("  ... and {} more".format(len(errs) - 5))
            if stats["projects"] == 0:
                lines += ["", "No projects at all usually means the hub list has not loaded.",
                          "Open the Data Panel in Fusion once, then run this again."]
            _say(ui, "\n".join(lines))
            return

        found_all = len(designs)
        sel = (SELECT or "all").strip()
        key = sel.lower()
        if key == "all":
            pass
        elif key == "list":
            wanted = set()
            if SELECT_LIST and os.path.exists(SELECT_LIST):
                try:
                    with open(SELECT_LIST, encoding="utf-8") as fh:
                        wanted = {ln.strip().replace("\\", "/").lower()
                                  for ln in fh if ln.strip()}
                except Exception:
                    wanted = set()
            if not wanted:
                _say(ui, 'SELECT = "list", but {} is missing or empty.'.format(SELECT_LIST))
                return
            designs = [(f, p) for (f, p) in designs
                       if "/".join(p + [safe(get(f, "name") or "")]).lower() in wanted]
        elif key in GROUPS:
            match = GROUPS[key]
            designs = [(f, p) for (f, p) in designs if match(get(f, "name") or "", list(p))]
        else:
            designs = [(f, p) for (f, p) in designs
                       if key in (get(f, "name") or "").lower()
                       or key in "/".join(p).lower()]

        # Any branch above can come back empty, and an empty set used to reach
        # progress.show(..., 0, 0), which dies with "invalid argument minimumValue or
        # maximumValue" -- a crash that reads like a Fusion bug instead of "nothing matched".
        if not designs:
            _say(ui, 'SELECT = "{}" matched none of the {} designs found.'.format(
                SELECT, found_all))
            return

        if LIMIT and LIMIT > 0:
            designs = designs[:LIMIT]

        # Scope is reported in the progress dialog's title, never in a message box. This script
        # exists to be started and left alone, and a modal here blocks the whole run until
        # someone comes back and clicks it -- which is the one thing an unattended batch job
        # must never do. Modal dialogs below are only ever at the very end.
        scope = "{} of {} designs".format(len(designs), found_all) \
            if len(designs) != found_all else "{} designs".format(found_all)

        os.makedirs(OUT_DIR, exist_ok=True)
        # Do this BEFORE opening anything, or our own documents land in the set and never
        # get closed.
        remember_open_documents(app)
        total = len(designs)
        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show("Extracting timelines - " + scope, "%v / %m   %p%", 0, total, 0)

        started = time.time()
        _CHUNK.update(stop=None, n=0, old={})
        ok = skipped = failed = 0
        summary = []
        changed = []   # designs whose version or bucket differs from the inventory

        idx = -1
        again = 0          # visits to the same design in a row (states in portions)
        while True:
            if again:
                again_now, again = again, 0
            else:
                again_now = 0
                idx += 1
            if idx >= len(designs) or progress.wasCancelled or mem_over():
                break
            data_file, parts = designs[idx]
            progress.progressValue = idx
            progress.message = "%v / %m   %p%\n" + data_file.name[:60]
            adsk.doEvents()

            out_dir = os.path.join(OUT_DIR, *parts)
            os.makedirs(out_dir, exist_ok=True)
            dest = os.path.join(out_dir, safe(data_file.name) + ".timeline.json")
            if os.path.exists(dest):
                try:
                    with open(dest, encoding="utf-8") as fh:
                        seen = json.load(fh)
                        # Crosslink 2026-11-22: a design saved again since it was read is read again
                        same = seen.get("version") in (None, get(data_file, "versionNumber"))
                        if (same and still_current(seen) and not seen.get("states_pending")
                                and not states_missing(dest, seen)):
                            skipped += 1
                            continue
                except Exception:
                    pass  # unreadable or older: re-extract it

            # Crosslink 2026-11-18: a visit started close to the cap walks the whole timeline and
            # writes nothing (35 min lost on 17 Nov). Stop here instead and ask for the restart.
            if mem_over(MEM_START_GB):
                break

            # Crosslink 2026-11-14: say which design is being worked on, readable from outside.
            try:
                with open(os.path.join(OUT_DIR, "status.txt"), "w", encoding="utf-8") as fh:
                    fh.write("{}  {} / {}  {}s elapsed  opening: {}{}\n".format(
                        time.strftime("%H:%M:%S"), idx + 1, total, int(time.time() - started),
                        "/".join(list(parts)[1:] + [data_file.name]),
                        "  (visit {})".format(again_now + 1) if again_now else ""))
            except Exception:
                pass

            doc = None
            design = None
            planned = data_file if isinstance(data_file, _Planned) else None
            try:
                if planned is not None:
                    data_file = app.data.findFileById(planned.id)
                    if data_file is None:
                        data_file = planned
                        raise RuntimeError("not found by its ID: moved or deleted since the inventory? "
                                           "Run InventoryHub again.")
                for visible in (False, True):
                    try:
                        doc = app.documents.open(data_file, visible)
                    except Exception:
                        doc = None
                    if doc is None:
                        continue
                    adsk.doEvents()
                    design = adsk.fusion.Design.cast(
                        doc.products.itemByProductType("DesignProductType"))
                    if design is not None:
                        break
                    close_if_ours(doc)
                    doc = None

                if design is None:
                    raise RuntimeError("no Design product")

                if planned is not None and get(data_file, "versionNumber") != planned.versionNumber:
                    changed.append("{}: version {} -> {}".format(
                        data_file.name, planned.versionNumber, get(data_file, "versionNumber")))
                _DEST["base"] = dest[: -len(".timeline.json")]
                _CHUNK["old"] = old_states(dest)
                # Crosslink 2026-11-22: a design read again keeps the states it had. They were
                # asked for by an earlier wanted list that may no longer name the design; without
                # this a re-read dropped them and the rebuild lost its frozen steps.
                try:
                    with open(dest, encoding="utf-8") as fh:
                        _CHUNK["keep"] = frozen_steps(json.load(fh))
                except Exception:
                    _CHUNK["keep"] = set()
                _LINKED_CACHE.clear()      # per design, also for one without a timeline
                del _LINKED_SETS[:]
                info = describe_design(design, data_file, parts)
                compact_linked(info)
                try:
                    info["bucket"] = bucket_of(info)
                    if planned is not None and planned.bucket and info["bucket"] != planned.bucket:
                        changed.append("{}: {} -> {}".format(data_file.name, planned.bucket, info["bucket"]))
                except Exception:
                    pass
                with open(dest, "w", encoding="utf-8") as fh:
                    json.dump(info, fh, indent=2)
                summary.append({
                    "design": info["name"],
                    "path": info["path"],
                    "steps": info["stats"]["timeline_steps"],
                    "parameters": info["stats"]["parameters"],
                    "feature_types": info["stats"]["feature_types"],
                    "states": dict(info.get("states_visit") or {},
                                   pending=info.get("states_pending", 0)),
                })
                ok += 1
                # states left over: the same design again, as long as a visit gets somewhere
                if info.get("states_pending") and _CHUNK["new"] > 0 and again_now < 20:
                    again = again_now + 1
            except Exception as e:
                failed += 1
                summary.append({
                    "design": get(data_file, "name"),
                    "path": "/".join(parts),
                    "error": "{}: {}".format(type(e).__name__, e),
                })
            finally:
                _ROLLBACK["timeline"] = None
                _ROLLBACK["skip"] = None
                if doc is not None:
                    kept = close_if_ours(doc)
                    if kept:
                        summary.append({"left_open": get(doc, "name"), "because": kept})
                adsk.doEvents()

            with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fh:
                json.dump({"extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source,
                           "total_found": total, "changed_since_inventory": changed,
                           "designs": summary}, fh, indent=2)

        progress.hide()
        if _CHUNK["stop"] is not None:
            try:
                with open(os.path.join(OUT_DIR, "status.txt"), "a", encoding="utf-8") as fh:
                    fh.write("{}  STOPPED: Fusion at {} GB. Quit Fusion, start it, run again.\n".format(
                        time.strftime("%H:%M:%S"), _CHUNK["stop"]))
            except Exception:
                pass
            _say(ui, "Stopped on purpose: Fusion is using {} GB of memory.\n\n"
                     "Quit Fusion, start it again and run ExtractTimeline once more.\n"
                     "It carries on where it stopped.".format(_CHUNK["stop"]))
            return
        elapsed = int(time.time() - started)
        steps = sum(s.get("steps", 0) for s in summary)
        _say(ui, 
            "Timeline extraction finished.\n\n"
            "designs: {}\nskipped (already done): {}\nfailed: {}\n"
            "timeline steps captured: {}\nelapsed: {}m {}s\ndesigns: {}\n{}\n{}".format(
                ok, skipped, failed, steps, elapsed // 60, elapsed % 60, source,
                "\n{} design(s) changed since the inventory (see summary.json); run InventoryHub again "
                "to bring it up to date.\n".format(len(changed)) if changed else "", OUT_DIR))

    except Exception:
        if ui:
            _say(ui, "Script failed:\n{}".format(traceback.format_exc()))
