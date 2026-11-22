#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Translate a Fusion timeline (from ExtractTimeline.py) into a SolidCraft command script.

This is the part that makes a migrated part *editable* rather than a frozen solid. A STEP import
gives you geometry; this gives you the recipe -- parameters, sketches, and features that still
rebuild when you change a dimension.

    python timeline_to_solidcraft.py part.timeline.json -o part.json
    python timeline_to_solidcraft.py ~/cad/fusion-timeline --all      translate a whole tree
    python timeline_to_solidcraft.py part.timeline.json --verify      build it and report measures

Every step is either translated or recorded as untranslated with a reason. Nothing is dropped
quietly: a half-migrated part that claims to be whole is worse than one that admits the gap.
"""

import argparse
import collections
import math
import json
import re
import os
import shutil
import subprocess
import threading
import time
import sys

HOME = os.path.expanduser("~")


def find_cli():
    """Locate solidcraft-cli: $SOLIDCRAFT_CLI, PATH, a local cargo build, a portable unzip, then the
    places SolidCraft's installers use on macOS and Windows."""
    env = os.environ.get("SOLIDCRAFT_CLI")
    if env and os.path.exists(env):
        return env
    exe = "solidcraft-cli.exe" if os.name == "nt" else "solidcraft-cli"
    found = shutil.which(exe) or shutil.which("solidcraft-cli")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../target/release", "../../target/debug"):
        p = os.path.join(here, rel, exe)
        if os.path.exists(p):
            return os.path.abspath(p)
    import glob
    for p in sorted(glob.glob(os.path.join(
            os.path.expanduser("~"), "tools", "solidcraft", "*", exe)), reverse=True):
        return p
    # where the SolidCraft installers put it
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")]
        pats = [os.path.join(r, "Programs", "*olid*raft*", exe) for r in roots if r]
        pats += [os.path.join(r, "*olid*raft*", exe) for r in roots if r]
        pats += [os.path.join(r, "*olid*raft*", "*", exe) for r in roots if r]
    else:
        pats = ["/Applications/SolidCraft*.app/Contents/MacOS/" + exe,
                os.path.expanduser("~/Applications/SolidCraft*.app/Contents/MacOS/") + exe,
                "/opt/homebrew/bin/" + exe, "/usr/local/bin/" + exe]
    for pat in pats:
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    return exe  # let the OS report it, with a name the user recognises


DEFAULT_CLI = find_cli()


# Fusion's FeatureOperations enum arrives as a number; these are its documented values.
OPERATION = {
    "0": "join", "1": "cut", "2": "intersect", "3": "new", "4": "new",
    "JoinFeatureOperation": "join", "CutFeatureOperation": "cut",
    "IntersectFeatureOperation": "intersect", "NewBodyFeatureOperation": "new",
    "NewComponentFeatureOperation": "new",
}

# FeatureExtentTypes.
EXTENT = {"0": "one_side", "1": "two_sides", "2": "symmetric"}

ORIGIN_PLANES = {"XY", "XZ", "YZ"}

# The origin planes expressed as frames, so a sketch's plane can be reasoned about uniformly.
PLANE_FRAMES = {
    "XY": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "y_dir": [0, 1, 0]},
    "XZ": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "y_dir": [0, 0, 1]},
    "YZ": {"origin": [0, 0, 0], "x_dir": [0, 1, 0], "y_dir": [0, 0, 1]},
}

# Fusion stores lengths in centimetres; everything written here is millimetres.
CM_UNITS = {"cm"}
UNITS = {"mm", "cm", "m", "in", "ft", "deg", "rad"}


def order_parameters(params):
    """Define parameters before whatever uses them.

    Fusion lists user parameters in creation order, which is not dependency order -- a design can
    define "length = cutOut * 2" before "cutOut" exists. Emitting that order verbatim fails on the
    first forward reference, so sort dependencies first and leave genuine cycles in their original
    place for the engine to reject with its own message.
    """
    by_name = {p["name"]: p for p in params if p.get("name")}
    ordered, state = [], {}

    def visit(name):
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            return  # a cycle: leave it, the expression engine will report it properly
        state[name] = "visiting"
        p = by_name[name]
        for tok in re.findall(r"\b[A-Za-z_]\w*\b", p.get("expression") or ""):
            if tok in by_name and tok != name:
                visit(tok)
        state[name] = "done"
        ordered.append(p)

    for p in params:
        if p.get("name"):
            visit(p["name"])
    return ordered



SPLINE_POINTS = 33    # fit points drawn per spline (the timeline holds up to 400 samples)
LOCKSTEP_TIMEOUT_S = 300   # one build of the script so far (the step-by-step check)
VERIFY_TIMEOUT_S = 300     # the final build of the whole script; both set by --verify-timeout


class Skip(Exception):
    """This step cannot be rebuilt faithfully; say why rather than emit something wrong."""


def expr(v, default=None):
    """Prefer Fusion's expression over its number: the expression is what stays parametric."""
    if isinstance(v, dict):
        e = v.get("expression")
        if e:
            return e
        mm = v.get("value_mm")
        if mm is not None:
            return "{} mm".format(round(mm, 9))
    return default


def op_of(step, default="auto"):
    o = step.get("operation")
    return OPERATION.get(str(o), default) if o is not None else default


# ---------------------------------------------------------------------------
# sketches
# ---------------------------------------------------------------------------

def plane_frame(sk):
    """Rebuild a sketch's plane from its own coordinate frame.

    Most sketches in real designs sit on construction planes or faces rather than on XY/XZ/YZ,
    and their identity cannot be carried across. Their *frame* can: Fusion's sketch transform is
    sketch space to model space, so its axes and origin define the plane directly, and the target
    accepts a plane given that way. This is what rescues the majority of real sketches.

    Fusion's matrix is row-major with the translation in the last column, and its lengths are in
    centimetres.
    """
    t = sk.get("transform")
    if not t or len(t) < 12:
        return None
    x_dir = [t[0], t[4], t[8]]
    y_dir = [t[1], t[5], t[9]]
    origin = [t[3] * 10.0, t[7] * 10.0, t[11] * 10.0]
    # A degenerate frame would silently place the sketch somewhere arbitrary.
    for v in (x_dir, y_dir):
        if sum(c * c for c in v) < 0.5:
            return None
    return {"origin": [round(c, 9) for c in origin],
            "x_dir": [round(c, 9) for c in x_dir],
            "y_dir": [round(c, 9) for c in y_dir]}


def emit_sketch(step, out, ctx):
    sk = step.get("sketch") or {}
    plane = (sk.get("plane") or {}).get("name")
    if plane in ORIGIN_PLANES:
        spec = plane
    else:
        spec = plane_frame(sk)
        if spec is None:
            raise Skip(
                "sketch is on {} and carries no usable coordinate frame".format(
                    plane or "an unnamed plane"))

    first_cmd = len(out)
    ctx.setdefault("sketch_recs", {})[sk.get("name")] = sk
    ctx.setdefault("plane_specs", {})[sk.get("name")] = spec
    out.append({"command": "sketch.create", "params": {"plane": spec}})
    # The engine names sketches Sketch1, Sketch2, ... in the order they are created. Features
    # name their sketch, because the last one drawn is not always the one they use.
    ctx.setdefault("sc_sketch", {})[sk.get("name")] = "Sketch{}".format(
        sum(1 for c in out if c["command"] == "sketch.create"))

    curves = sk.get("curves") or []
    if not curves:
        if sk.get("texts"):
            # A sketch that holds only text: nothing to draw now, the text is drawn as its
            # outline when a feature uses it.
            del out[first_cmd:]
            ctx["sc_sketch"].pop(sk.get("name"), None)
            ctx["sketches"].add(sk.get("name"))
            return {"curves_made": 0, "texts": len(sk["texts"])}
        raise Skip("sketch has no readable curves")

    made = 0
    unsupported = {}
    # SolidCraft names sketch curves predictably and per type -- l1, l2 for lines, c1 for
    # circles, a1 for arcs -- and addresses their points as "l1.start", "c1.center". Counting
    # as we emit therefore tells us the id of every curve without having to read anything back,
    # which is what lets a constraint be written in the same script that creates its operands.
    seq = {"line": 0, "circle": 0, "arc": 0}
    prefix = {"line": "l", "circle": "c", "arc": "a"}
    ids = {}  # Fusion entity token -> SolidCraft curve id

    for c in curves:
        kind = c.get("type")
        cons = bool(c.get("construction"))
        try:
            if kind == "line" and c.get("start") and c.get("end"):
                out.append({"command": "sketch.line",
                            "params": {"points": [c["start"], c["end"]], "construction": cons}})
            elif kind == "circle" and c.get("center") and c.get("radius_mm") is not None:
                out.append({"command": "sketch.circle.center",
                            "params": {"center": c["center"], "radius": c["radius_mm"],
                                       "construction": cons}})
            elif kind == "arc" and c.get("center") and c.get("start") and c.get("end"):
                out.append({"command": "sketch.arc.center_point",
                            "params": {"center": c["center"], "start": c["start"],
                                       "end": c["end"], "construction": cons}})
            elif kind in ("fitted_spline", "fixed_spline", "control_spline", "conic") and \
                    c.get("samples"):
                # Crosslink 2026-11-15 (schema 18): points Fusion measured along the curve. A
                # fit spline through them follows the real shape closely; the original's own
                # control points would need knots and weights the target's spline does not take.
                pts = [q for q in c["samples"] if q]
                # 400 fit points per spline made the engine take minutes per build on logo
                # sketches; 33 evenly spread ones keep the shape (ends always included).
                if len(pts) > SPLINE_POINTS:
                    step = (len(pts) - 1) / float(SPLINE_POINTS - 1)
                    pts = [pts[int(round(k * step))] for k in range(SPLINE_POINTS)]
                if any(len(q) != 2 for q in pts):
                    raise ValueError("spline leaves the sketch plane")
                prm = {"points": pts, "construction": cons}
                if len(pts) > 3 and math.hypot(pts[0][0] - pts[-1][0],
                                               pts[0][1] - pts[-1][1]) < 1e-7:
                    prm = {"points": pts[:-1], "periodic": True, "construction": cons}
                out.append({"command": "sketch.spline.fit_point", "params": prm})
            elif kind == "fitted_spline" and c.get("points"):
                pts = [p for p in c["points"] if p]
                if len(pts) < 2:
                    raise ValueError("too few points")
                out.append({"command": "sketch.spline.fit_point",
                            "params": {"points": pts, "construction": cons}})
            elif kind == "ellipse" and c.get("center") and c.get("major_end") and \
                    c.get("minor_radius_mm") and not c.get("orientation_unrecorded") and \
                    not c.get("axis_labels_suspect"):
                out.append({"command": "sketch.ellipse",
                            "params": {"center": c["center"], "major": c["major_end"],
                                       "minor_radius": c["minor_radius_mm"], "axes": False,
                                       "construction": cons}})
            elif kind == "ellipse":
                raise ValueError("ellipse needs major/minor axes that were not captured")
            else:
                raise ValueError("no mapping")
            made += 1
            if kind in seq:
                seq[kind] += 1
                tok = c.get("token")
                if tok:
                    ids[tok] = "{}{}".format(prefix[kind], seq[kind])
        except Exception as e:
            unsupported[kind] = unsupported.get(kind, 0) + 1
            del e

    if not made:
        raise Skip("no curve in this sketch could be translated ({})".format(
            ", ".join("{}x{}".format(v, k) for k, v in unsupported.items()) or "empty"))

    ctx["sketches"].add(sk.get("name"))
    ctx.setdefault("fusion_frames", {})[sk.get("name")] = plane_frame(sk)
    ctx.setdefault("curve_bounds", {})[sk.get("name")] = curve_bounds(curves)
    ctx.setdefault("sketch_cmds", {})[sk.get("name")] = list(out[first_cmd:])
    ctx.setdefault("frames", {})[sk.get("name")] = (
        spec if isinstance(spec, dict) else PLANE_FRAMES.get(spec))

    extra = {}
    if ctx.get("constraints"):
        extra = emit_sketch_relations(sk, ids, out)
    return dict({"curves_made": made}, **extra) if not unsupported else \
        dict({"curves_made": made, "curves_skipped": unsupported}, **extra)

    return {"curves_made": made, "curves_skipped": unsupported} if unsupported else {"curves_made": made}



# Fusion constraint name -> SolidCraft command, for the ones that take two operands the same way.
CONSTRAINT_CMDS = {
    "parallel": "sketch.constraint.parallel",
    "perpendicular": "sketch.constraint.perpendicular",
    "collinear": "sketch.constraint.collinear",
    "concentric": "sketch.constraint.concentric",
    "equal": "sketch.constraint.equal",
    "tangent": "sketch.constraint.tangent",
    "symmetry": "sketch.constraint.symmetry",
    "midpoint": "sketch.constraint.midpoint",
}

# Dimension class name -> the kind of dimension to place.
DIMENSION_KINDS = {
    "linear": "distance", "offset": "distance", "lineardiameter": "diameter",
    "radial": "radius", "diameter": "diameter", "angular": "angle",
    "distancebetweenlines": "distance", "distancebetweenpointandline": "distance",
}


def emit_sketch_relations(sk, ids, out):
    """Re-apply Fusion's constraints and dimensions to the curves just created.

    Without these the geometry lands in the right place but holds no relationships, so changing a
    parameter moves nothing in the sketch. With them, a dimension driven by "width" still drives
    the part. Coincidence is left to the curve commands, which already join points that meet;
    re-stating it would only risk an over-constrained sketch for no gain.
    """
    made = {"constraints": 0, "dimensions": 0, "skipped": 0}

    for k in sk.get("constraints") or []:
        cmd = CONSTRAINT_CMDS.get((k.get("type") or "").lower())
        refs = [ids.get(t) for t in (k.get("refs") or [])]
        refs = [r for r in refs if r]
        if not cmd or len(refs) < 2:
            made["skipped"] += 1
            continue
        out.append({"command": cmd, "params": {"a": refs[0], "b": refs[1]}})
        made["constraints"] += 1

    for d in sk.get("dimensions") or []:
        kind = DIMENSION_KINDS.get((d.get("type") or "").lower())
        refs = [ids.get(t) for t in (d.get("refs") or [])]
        refs = [r for r in refs if r]
        value = d.get("expression")
        if not kind or not refs or not value:
            made["skipped"] += 1
            continue
        params = {"entities": refs[:2], "type": kind, "value": value}
        out.append({"command": "sketch.dimension", "params": params})
        made["dimensions"] += 1

    return {"relations": made} if (made["constraints"] or made["dimensions"]) else {}


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

def curve_bounds(curves):
    """2D extent of a sketch's curves in sketch coordinates (mm), or None."""
    xs, ys = [], []
    for c in curves:
        r = c.get("radius_mm") or 0.0
        ctr = c.get("center")
        if ctr and c.get("type") in ("circle", "arc"):
            if not r and c.get("start"):
                r = math.hypot(c["start"][0] - ctr[0], c["start"][1] - ctr[1])
            xs += [ctr[0] - r, ctr[0] + r]
            ys += [ctr[1] - r, ctr[1] + r]
        for k in ("start", "end"):
            if c.get(k):
                xs.append(c[k][0]); ys.append(c[k][1])
        for q in c.get("points") or []:
            if q:
                xs.append(q[0]); ys.append(q[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def profile_point(prof, ctx):
    """The 2D pick point of a profile, in its sketch's coordinates.

    Crosslink 2026-11-15. The extractor's `point` is `modelToSketchSpace(centroid)`, but Fusion
    returns a profile's centroid and bounding box in SKETCH space for most sketches (1,624 of
    1,740 profiles on rotated sketches across the hub have z == 0 there), so the conversion is
    applied to a point that is already local and lands somewhere else: every sketch off XY gave
    "unknown profile". The raw `centroid_model` is kept in the timeline, so the right point can
    be recovered here without going back to Fusion: test both readings against where the
    sketch's curves actually are and take the one that fits.
    """
    pt = prof.get("point")
    c = prof.get("centroid_model")
    if not c or len(c) < 3:
        return pt
    name = prof.get("sketch")
    frame = (ctx.get("fusion_frames") or {}).get(name)
    bounds = (ctx.get("curve_bounds") or {}).get(name)
    bb = prof.get("bbox_model") or [c, c]

    def inside(q, tol):
        return bounds is None or (bounds[0] - tol <= q[0] <= bounds[2] + tol
                                  and bounds[1] - tol <= q[1] <= bounds[3] + tol)

    tol = 1e-3 + (0.01 * max(bounds[2] - bounds[0], bounds[3] - bounds[1]) if bounds else 0.0)

    # reading A: the values are already sketch coordinates
    a_ok = all(abs(q[2]) < 1e-6 for q in [c] + list(bb)) and inside(c, tol)
    a_pt = [round(c[0], 9), round(c[1], 9)]

    # reading B: the values are model coordinates and must be projected onto the sketch plane
    b_ok, b_pt = False, None
    if frame:
        o, x, y = frame["origin"], frame["x_dir"], frame["y_dir"]
        n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]

        def local(q):
            d = [q[i] - o[i] for i in range(3)]
            return (sum(d[i] * x[i] for i in range(3)), sum(d[i] * y[i] for i in range(3)),
                    sum(d[i] * n[i] for i in range(3)))
        lc = local(c)
        b_pt = [round(lc[0], 9), round(lc[1], 9)]
        b_ok = all(abs(local(q)[2]) < 1e-4 for q in [c] + list(bb)) and inside(lc, tol)

    if a_ok and not b_ok:
        return a_pt
    if b_ok and not a_ok:
        return b_pt
    if a_ok and b_ok:
        return a_pt
    return pt or a_pt


def target_profiles(name, ctx):
    """The profiles SolidCraft itself finds in a translated sketch (index, area, centroid).

    Crosslink 2026-11-15. Builds the sketch on its own with the CLI and asks `sketch.inspect`.
    Cached per sketch; None when no CLI is at hand or the sketch does not build alone.
    """
    cache = ctx.setdefault("target_profiles", {})
    if name in cache:
        return cache[name]
    cache[name] = None
    cli, cmds = ctx.get("cli"), (ctx.get("sketch_cmds") or {}).get(name)
    if not cli or not cmds:
        return None
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"commands": cmds + [{"command": "sketch.inspect", "params": {}}]}, fh)
        p = subprocess.run([cli, "run", tmp, "--results"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, cwd=os.path.dirname(cli) or None)
        res = json.loads(p.stdout)
        res = res.get("results", res) if isinstance(res, dict) else res
        last = res[-1]
        last = last.get("result", last) if isinstance(last, dict) else last
        cache[name] = last.get("profiles")
    except Exception:
        cache[name] = None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return cache[name]


def match_profiles(profs, ctx):
    """Indices of the SolidCraft profiles that are the same regions as these Fusion profiles.

    A centroid is not a pick: for an L, a ring or a crescent it lies outside its own region, and
    two nested regions can share one. Area plus centroid identifies the region itself, so the
    feature can name it by index (SolidCraft then stores it by its curves). Returns None unless
    every profile finds its own region: the engine does not take indices and points mixed.
    """
    used, out = set(), []
    for prof in profs:
        found = target_profiles(prof.get("sketch"), ctx)
        area, c = prof.get("area_mm2"), profile_point(prof, ctx)
        if not found or not area or not c:
            return None
        best, best_d = None, None
        for f in found:
            fa, fc = f.get("area_mm2"), f.get("centroid_sketch")
            if fa is None or not fc or (prof.get("sketch"), f.get("index")) in used:
                continue
            if abs(fa - area) > 0.01 * max(area, fa) + 1e-6:
                continue
            d = math.hypot(fc[0] - c[0], fc[1] - c[1])
            if d <= 0.02 * math.sqrt(max(area, fa)) + 1e-4 and (best_d is None or d < best_d):
                best, best_d = f.get("index"), d
        if best is None:
            return None
        used.add((prof.get("sketch"), best))
        out.append(best)
    return out


def profiles_param(step, ctx):
    """Re-find each profile by a point inside it, which survives parameter changes.

    If the profile's own sketch was not translated, the feature cannot be built either: emitting
    it anyway produces a script that fails halfway, which is worse than one that stops short and
    says why.
    """
    profs = step.get("profiles")
    # Crosslink 2026-11-15: a text (or anything else the extractor could not read) used as a
    # profile used to fall through to "all", which extruded the sketch's other regions instead:
    # the family sign came out as a second plate, 78 % too much volume.
    if profs and all(p.get("kind") == "text" for p in profs):
        if len(profs) != 1:
            raise Skip("several sketch texts in one feature")
        text_outline(profs[0], step, ctx, ctx["out"])
        # No profile list at all: the engine then takes every outline and treats a loop inside
        # another as its hole, which is what keeps the insides of a, e and o open. Naming
        # "all" fills them (the family sign came out 2.6 % too heavy).
        return None
    if step.get("profiles_unmapped"):
        raise Skip("a profile of this feature could not be read in Fusion")
    if not profs:
        return "all"
    wanted = {p.get("sketch") for p in profs if p.get("sketch")}
    missing = {w for w in wanted if w and w not in ctx["sketches"]}
    if missing:
        raise Skip("needs sketch {}, which was not translated".format(
            ", ".join(sorted(missing))))
    if len(wanted) == 1:
        ctx["use_sketch"] = (ctx.get("sc_sketch") or {}).get(next(iter(wanted)))
        idx = match_profiles(profs, ctx)
        if idx:
            return idx
    pts = [{"point": q} for q in (profile_point(p, ctx) for p in profs) if q]
    return pts or "all"


def emit_extrude(step, out, ctx):
    """One extrude per kind of profile: Fusion takes regions and texts in one feature, here
    each text is drawn as its own outline sketch and extruded with the same settings."""
    profs = step.get("profiles") or []
    texts = [p for p in profs if p.get("kind") == "text"]
    rest = [p for p in profs if p.get("kind") != "text"]
    if not texts or (len(texts) == 1 and not rest):
        return _emit_extrude_one(step, out, ctx)
    detail = {}
    if rest:
        detail = _emit_extrude_one(dict(step, profiles=rest), out, ctx) or {}
    for t in texts:
        one = dict(step, profiles=[t])
        if rest and str(step.get("operation")) in ("3", "4"):
            one["operation"] = "3"
        _emit_extrude_one(one, out, ctx)
    return dict(detail, texts=len(texts))


def _emit_extrude_one(step, out, ctx):
    d = expr(step.get("distance"))
    to = step.get("to_point")
    if not d and not to:
        raise Skip("no distance expression and no target ({})".format(
            step.get("extent_definition") or "unknown extent"))

    if d:
        params = {"distance": d}
    else:
        # "To object": the extrude runs until it meets something. Saying where that something is
        # keeps the intent -- it still lands on the face rather than near where the face was.
        params = {"to": to}
        off = expr(step.get("to_offset"))
        if off and not str(off).startswith("0"):
            params["to_offset"] = off
    ctx.pop("use_sketch", None)
    faces = step.get("profile_faces") or []
    if not step.get("profiles") and faces:
        # Crosslink 2026-11-15 (evening): the profile is a face of a body (Press Pull).
        if step.get("profile_count") and len(faces) != step["profile_count"]:
            raise Skip("only {} of {} profile faces were recorded".format(len(faces), step["profile_count"]))
        params.update({"faces": [f["point"] for f in faces], "operation": op_of(step)})
        owners = {f.get("body") for f in faces if f.get("body")}
        if len(owners) == 1:
            got = (ctx.get("names") or {}).get(owners.pop())
            if got:
                params["face_body"] = got
    elif not step.get("profiles"):
        # Without a recorded profile the extrude would take whatever sketch came last: in run 6
        # none of the 171 such extrudes came out right. It is a face of the body that Fusion
        # could not report after the extrude had used it (schema 22 reads it rolled back).
        raise Skip("extrude starts from a face of the body, which was not recorded")
    else:
        params.update({"profiles": profiles_param(step, ctx), "operation": op_of(step)})
    if ctx.get("use_sketch"):
        params["sketch"] = ctx.pop("use_sketch")
    if "profiles" in params and params.get("profiles") is None:
        del params["profiles"]
    t = expr(step.get("taper"))
    if t and not str(t).startswith("0"):
        params["taper"] = t
    if str(step.get("extent_type")) == "2" or step.get("is_symmetric") in (True, "True"):
        params["direction"] = "symmetric"
    d2 = expr(step.get("distance_two"))
    if d2:
        params["distance2"] = d2
    out.append({"command": "solid.extrude", "params": params})
    return {}


def _sets_of(step, size_keys):
    """One record per edge set. A single Fusion fillet can round groups to different radii, so
    each set becomes its own command rather than being flattened to one size."""
    sets = step.get("edge_sets")
    if sets:
        return [(s["edge_points"], next((expr(s.get(k)) for k in size_keys if s.get(k)), None))
                for s in sets if s.get("edge_points")]
    pts = step.get("edge_points")
    if pts:
        return [(pts, next((expr(step.get(k)) for k in size_keys if step.get(k)), None))]
    return []


def _only_body(step, ctx):
    """The rebuild's name of the one body a feature works on, or None.

    Crosslink 2026-11-15 (evening): where two bodies touch, a point on an edge lies on edges of
    both and SolidCraft asks which body is meant. Fusion recorded the bodies the feature
    touched; when that is exactly one, has one name of its own and one counterpart, name it.
    """
    x = step.get("x") or {}
    bs = x.get("bodies") or []
    if x.get("body_count") != 1 or len(bs) != 1:
        return None
    name = bs[0].get("name")
    if sum(1 for b in ctx.get("fusion_bodies") or [] if b.get("name") == name) != 1:
        return None
    if name in (ctx.get("groups") or {}):
        return None
    return (ctx.get("names") or {}).get(name)


def emit_fillet(step, out, ctx):
    sets = _sets_of(step, ("radius",))
    if not sets:
        raise Skip("no edge geometry captured ({} edges referenced)".format(
            step.get("edge_count", "?")))
    made = 0
    for pts, size in sets:
        size = size or expr(step.get("radius"))
        if not size:
            continue
        params = {"edges": pts, "radius": size}
        if _only_body(step, ctx):
            params["body"] = _only_body(step, ctx)
        out.append({"command": "solid.fillet", "params": params})
        made += 1
    if not made:
        raise Skip("no radius expression on any edge set")
    return {"edge_sets": made, "edges": sum(len(p) for p, _ in sets)}


def emit_chamfer(step, out, ctx):
    sets = _sets_of(step, ("distance", "radius"))
    if not sets:
        raise Skip("no edge geometry captured ({} edges referenced)".format(
            step.get("edge_count", "?")))
    made = 0
    for pts, size in sets:
        size = size or expr(step.get("distance")) or expr(step.get("radius"))
        if not size:
            continue
        params = {"edges": pts, "distance": size}
        if _only_body(step, ctx):
            params["body"] = _only_body(step, ctx)
        out.append({"command": "solid.chamfer", "params": params})
        made += 1
    if not made:
        raise Skip("no chamfer distance on any edge set")
    return {"edge_sets": made, "edges": sum(len(p) for p, _ in sets)}


def _opened_faces(step, ctx):
    """Where a shell was opened, worked out from the cavity it left.

    Crosslink 2026-11-15. The faces a shell removes are gone after the step and were not read
    before it, so the recorded points lie on the new inner walls (the engine: "no face at").
    The cavity's box reaches the body's own box exactly on the opened sides; a point in the
    middle of each such side lies on the removed face for the box-like bodies this covers. The
    lockstep check after the step catches the cases where it does not.
    """
    x = step.get("x") or {}
    inner = [f.get("bbox_mm") for f in x.get("faces") or [] if f.get("bbox_mm")]
    bodies = x.get("bodies") or []
    if not inner or len(bodies) != 1 or not bodies[0].get("bbox_mm") or \
            len(inner) != x.get("face_count"):
        return None
    lo = [min(b[0][k] for b in inner) for k in range(3)]
    hi = [max(b[1][k] for b in inner) for k in range(3)]
    blo, bhi = bodies[0]["bbox_mm"]
    mid = [(lo[k] + hi[k]) / 2.0 for k in range(3)]
    pts = []
    for k in range(3):
        for edge, body_edge in ((lo[k], blo[k]), (hi[k], bhi[k])):
            if abs(edge - body_edge) < 1e-6:
                q = list(mid)
                q[k] = body_edge
                pts.append([round(v, 9) for v in q])
    return pts or None


def emit_shell(step, out, ctx):
    pts = _opened_faces(step, ctx) or step.get("face_points") or []
    t = expr(step.get("thickness"))
    if not t:
        raise Skip("no thickness expression")
    out.append({"command": "solid.shell", "params": {"faces": pts, "thickness": t,
                                                     "direction": "inside"}})
    return {"faces": len(pts)}


def emit_draft(step, out, ctx):
    pts = step.get("face_points")
    if not pts:
        raise Skip("no face geometry captured")
    a = expr(step.get("angle"))
    if not a:
        raise Skip("no draft angle expression")
    out.append({"command": "solid.draft",
                "params": {"faces": pts, "angle": a, "neutral": "XY"}})
    return {"faces": len(pts)}


def world_axis(axis):
    """Name the world axis a revolve turns about, or None if it is not one of them.

    Only X, Y and Z through the origin can be named. A parallel axis offset from the origin is
    a different axis entirely, so it has to be refused rather than rounded to the nearest name --
    the earlier version guessed "x" unconditionally, which silently produced the wrong solid.
    """
    if not axis or not axis.get("start") or not axis.get("end"):
        return None
    s, e = axis["start"], axis["end"]
    d = [e[i] - s[i] for i in range(3)]
    length = sum(c * c for c in d) ** 0.5
    if length < 1e-9:
        return None
    d = [c / length for c in d]
    for i, name in enumerate("XYZ"):
        if abs(abs(d[i]) - 1.0) > 1e-6:
            continue
        # Aligned with this axis; it must also pass through the origin to *be* that axis.
        off = [s[j] for j in range(3) if j != i]
        if max(abs(v) for v in off) <= 1e-6:
            return name
        return None
    return None


def axis_lies_in(frame, name):
    """Is world axis `name` contained in this sketch plane?

    A revolve turns a profile about an axis in its own plane; naming an axis that merely exists
    in the world gets the right-sounding answer and the wrong solid, which the engine then
    rejects with "the Y axis does not lie in the sketch plane".
    """
    if not frame:
        return True  # unknown plane: let the engine judge it rather than refuse wrongly
    i = "XYZ".index(name)
    d = [1.0 if j == i else 0.0 for j in range(3)]
    x, y, o = frame["x_dir"], frame["y_dir"], frame["origin"]
    # Plane normal, then: the direction must be perpendicular to it and the origin must be on it.
    n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    if abs(sum(n[k] * d[k] for k in range(3))) > 1e-6:
        return False
    return abs(sum(n[k] * o[k] for k in range(3))) <= 1e-6


def emit_revolve(step, out, ctx):
    axis = step.get("axis")
    # Crosslink 2026-11-15 (evening): an axis that is a sketch line is recorded in that
    # sketch's own coordinates. Example: a line along the sketch's y on the XZ plane is the
    # world Z axis, and was read as "Y". Bring it into the world through the sketch's frame.
    ax_sketch = (((step.get("x") or {}).get("props") or {}).get("axis") or {}).get("sketch")
    frame = (ctx.get("frames") or {}).get(ax_sketch) if ax_sketch else None
    if axis and axis.get("kind") == "SketchLine" and frame and axis.get("start") and axis.get("end"):
        def world(q):
            return [frame["origin"][k] + q[0] * frame["x_dir"][k] + q[1] * frame["y_dir"][k]
                    for k in range(3)]
        axis = dict(axis, start=world(axis["start"]), end=world(axis["end"]))
    name = world_axis(axis)
    if not name:
        if not axis or not axis.get("start") or not axis.get("end"):
            raise Skip("axis is not a world axis through the origin (no axis captured)")
        d = [axis["end"][k] - axis["start"][k] for k in range(3)]
        ln = sum(c * c for c in d) ** 0.5
        if ln < 1e-9:
            raise Skip("revolve axis has no length")
        name = {"origin": [round(v, 9) for v in axis["start"]], "dir": [round(c / ln, 12) for c in d]}
    else:
        for prof in step.get("profiles") or []:
            pframe = (ctx.get("frames") or {}).get(prof.get("sketch"))
            if not axis_lies_in(pframe, name):
                raise Skip("the {} axis does not lie in sketch {}'s plane".format(
                    name, prof.get("sketch")))
    ctx.pop("use_sketch", None)
    params = {"axis": name, "angle": expr(step.get("angle"), "360 deg"),
              "profiles": profiles_param(step, ctx), "operation": op_of(step)}
    if ctx.get("use_sketch"):
        params["sketch"] = ctx.pop("use_sketch")
    a2 = expr(step.get("angle2"))
    if a2:
        params["angle2"] = a2
    if str(step.get("extent_type")) == "2" or step.get("is_symmetric") in (True, "True"):
        params["direction"] = "symmetric"
    out.append({"command": "solid.revolve", "params": params})
    return {"axis": name}


def emit_offset_faces(step, out, ctx):
    pts = step.get("face_points")
    # Crosslink 2026-11-15: prefer the faces read before the step. The older points lie on the
    # faces after they moved, where the rebuild has nothing yet.
    ins = ((step.get("x") or {}).get("inputs") or {}).get("inputFaces")
    if isinstance(ins, dict) and ins.get("_") == "list" and ins.get("items") and \
            ins.get("count") == len(ins["items"]):
        before = [it["face"].get("point") for it in ins["items"]
                  if isinstance(it, dict) and it.get("_") == "face"]
        if len(before) == len(ins["items"]) and all(before):
            pts = before
    if not pts:
        raise Skip("no face geometry captured ({} faces referenced)".format(
            step.get("face_count", "?")))
    d = expr(step.get("distance"))
    if not d:
        raise Skip("no offset distance expression")
    out.append({"command": "solid.offset_face", "params": {"faces": pts, "distance": d}})
    return {"faces": len(pts)}


# ---------------------------------------------------------------------------
# Crosslink 2026-11-15: features that need the schema 18 extras (`x`) and body names
# ---------------------------------------------------------------------------

def _x(step):
    x = step.get("x")
    if not x:
        raise Skip("timeline has no feature data for this step (extracted before schema 18)")
    return x


def _unwrap(v):
    """A parameter record -> its expression; a point/vector record -> its numbers."""
    if isinstance(v, dict):
        k = v.get("_")
        if k == "parameter":
            return v.get("expression")
        if k in ("point", "vertex", "construction_point"):
            return v.get("mm")
        if k == "vector":
            return v.get("v")
    return v


def _body_names(v):
    """Fusion body names out of a described body, list of bodies, or collection record."""
    if v is None:
        return []
    if isinstance(v, dict) and v.get("_") == "body":
        return [v["body"].get("name")]
    if isinstance(v, dict) and v.get("_") == "list":
        if v.get("count", 0) > len(v.get("items") or []):
            raise Skip("selection of {} entities is larger than what was recorded".format(v["count"]))
        out = []
        for it in v.get("items") or []:
            if not (isinstance(it, dict) and it.get("_") == "body"):
                raise Skip("selection holds {} rather than bodies".format(
                    (it or {}).get("_") or (it or {}).get("type") if isinstance(it, dict) else it))
            out.append(it["body"].get("name"))
        return out
    raise Skip("no bodies recorded")


def sc_body(name, ctx):
    """The rebuild's name for a Fusion body (matched by extent and volume after each step)."""
    got = (ctx.get("names") or {}).get(name)
    if not got:
        raise Skip("body {} has no counterpart in the rebuild".format(name))
    return got


def sc_bodies(name, ctx):
    """All rebuilt bodies that make up one Fusion body (several when it is separate lumps)."""
    first = sc_body(name, ctx)
    return (ctx.get("groups") or {}).get(name) or [first]


def _new_or_join(step, ctx):
    """Did this step add a body, or grow/cut an existing one? Fusion's primitives do not say."""
    before = ctx.get("fusion_bodies") or []
    after = step.get("bodies_after")
    if not isinstance(after, list):
        return "new"
    if len(after) > len(before):
        return "new"
    vb = sum(b.get("volume_mm3") or 0 for b in before)
    va = sum(b.get("volume_mm3") or 0 for b in after)
    return "join" if va >= vb else "cut"


AXES = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])


def emit_box(step, out, ctx):
    x = _x(step)
    planes = [f["surface"] for f in x.get("faces") or [] if f["surface"].get("type") == "Plane"]
    if len(planes) != len(x.get("faces") or []) or x.get("face_count") not in (5, 6) or \
            len(planes) != x.get("face_count"):
        raise Skip("box faces are not the 5 or 6 planes of a plain box")
    lo, hi = [None] * 3, [None] * 3
    for pl in planes:
        n = pl["normal"]
        k = max(range(3), key=lambda i: abs(n[i]))
        if abs(abs(n[k]) - 1.0) > 1e-9:
            raise Skip("box is turned against the world axes")
        v = pl["origin"][k]
        lo[k] = v if lo[k] is None else min(lo[k], v)
        hi[k] = v if hi[k] is None else max(hi[k], v)
    op = _new_or_join(step, ctx)
    if any(a is None for a in lo) or any(hi[k] - lo[k] < 1e-9 for k in range(3)):
        # a joined box loses the face it shares; the body it made tells the rest only when new
        bodies = x.get("bodies") or []
        if op != "new" or len(bodies) != 1 or not bodies[0].get("bbox_mm"):
            raise Skip("box extent cannot be read from its faces")
        lo, hi = bodies[0]["bbox_mm"]
    size = [round(hi[k] - lo[k], 9) for k in range(3)]
    out.append({"command": "solid.box", "params": {
        "length": "{} mm".format(size[0]), "width": "{} mm".format(size[1]),
        "height": "{} mm".format(size[2]), "corner": [round(v, 9) for v in lo],
        "operation": op}})
    return {"size_mm": size, "operation": op}


def emit_cylinder(step, out, ctx):
    x = _x(step)
    faces = [f["surface"] for f in x.get("faces") or []]
    cyl = [f for f in faces if f.get("type") == "Cylinder"]
    planes = [f for f in faces if f.get("type") == "Plane"]
    if len(cyl) != 1 or len(faces) != x.get("face_count"):
        raise Skip("cylinder faces are not one wall with its caps")
    o, a, r = cyl[0]["origin"], cyl[0]["axis"], cyl[0]["radius_mm"]
    if len(planes) == 2 and x.get("face_count") == 3:
        t = sorted(sum((pl["origin"][k] - o[k]) * a[k] for k in range(3)) for pl in planes)
    else:
        # Joined to another body, a cap is swallowed. The wall still spans the full height
        # when the cylinder stands along a world axis: read it from the wall's own extent.
        k = max(range(3), key=lambda i: abs(a[i]))
        wall = next((f for f in x.get("faces") or [] if f["surface"].get("type") == "Cylinder"), {})
        bb = wall.get("bbox_mm")
        if abs(abs(a[k]) - 1.0) > 1e-9 or not bb:
            raise Skip("joined cylinder is turned against the world axes")
        t = sorted([(bb[0][k] - o[k]) * a[k], (bb[1][k] - o[k]) * a[k]])
    height = t[1] - t[0]
    if height < 1e-9:
        raise Skip("cylinder has no height")
    base = [round(o[k] + t[0] * a[k], 9) for k in range(3)]
    op = _new_or_join(step, ctx)
    out.append({"command": "solid.cylinder", "params": {
        "radius": "{} mm".format(round(r, 9)), "height": "{} mm".format(round(height, 9)),
        "base": base, "axis": [round(v, 9) for v in a], "operation": op}})
    return {"radius_mm": r, "height_mm": round(height, 9), "operation": op}


def emit_sphere(step, out, ctx):
    x = _x(step)
    sph = [f["surface"] for f in x.get("faces") or [] if f["surface"].get("type") == "Sphere"]
    if len(sph) != 1 or not sph[0].get("origin") or not sph[0].get("radius_mm"):
        raise Skip("sphere face is not recorded")
    op = _new_or_join(step, ctx)
    out.append({"command": "solid.sphere", "params": {
        "radius": "{} mm".format(round(sph[0]["radius_mm"], 9)),
        "center": [round(v, 9) for v in sph[0]["origin"]], "operation": op}})
    return {"radius_mm": sph[0]["radius_mm"], "operation": op}


def emit_occurrence(step, out, ctx):
    """Crosslink 2026-11-15 (evening): a component placed in the design.

    A new, empty component becomes a SolidCraft component of the same name. The features that
    follow are still built in the design's own space (Fusion's timeline does not say which
    component a feature belongs to, and all recorded geometry is in world coordinates); at the
    end of the script the bodies are moved into the components Fusion had them in.
    A component that arrives with bodies (pasted, inserted or linked from another design) has
    no recipe in this timeline and no exported state yet.
    """
    comp = step.get("component")
    if not comp:
        raise Skip("component has no recorded name")
    after, before = step.get("bodies_after"), ctx.get("fusion_bodies") or []
    if isinstance(after, list):
        solid = [b for b in after if b.get("solid") is not False]
        if len(solid) != len(before):
            raise Skip("component arrives with {} bodies and no recipe for them{}".format(
                len(solid) - len(before), " (linked design)" if step.get("referenced") else ""))
    elif step.get("bodies") or step.get("referenced"):
        raise Skip("component arrives with bodies and no recipe for them")
    made = ctx.setdefault("components", set())
    if comp in made:
        return {"carried": True}
    out.append({"command": "component.create", "params": {"name": comp, "activate": False}})
    made.add(comp)
    return {"component": comp}


def emit_move(step, out, ctx):
    x = _x(step)
    ins = x.get("inputs") or {}
    names = _body_names(ins.get("inputEntities"))
    if not names:
        raise Skip("move has no recorded bodies")
    m = ((x.get("props") or {}).get("transform") or {}).get("row_major_cm") or step.get("transform")
    if not m or len(m) < 12:
        raise Skip("move has no transform")
    m = [round(v, 12) for v in m[:12]]
    for k in (3, 7, 11):
        m[k] = round(m[k] * 10.0, 9)   # centimetres to millimetres
    out.append({"command": "solid.move", "params": {
        "bodies": [b for n in names for b in sc_bodies(n, ctx)], "matrix": m + [0.0, 0.0, 0.0, 1.0]}})
    return {"bodies": names}


def emit_combine(step, out, ctx):
    x = _x(step)
    ins = x.get("inputs") or {}
    target = _body_names(ins.get("targetBody"))
    tools = _body_names(ins.get("toolBodies"))
    if len(target) != 1 or not tools:
        raise Skip("combine has no recorded target and tools")
    op = {"0": "join", "1": "cut", "2": "intersect"}.get(str(step.get("operation")))
    if not op:
        raise Skip("combine operation {} unknown".format(step.get("operation")))
    if len(sc_bodies(target[0], ctx)) > 1:
        raise Skip("combine target {} is several separate lumps in the rebuild".format(target[0]))
    params = {"target": sc_body(target[0], ctx),
              "tools": [b for n in tools for b in sc_bodies(n, ctx)], "operation": op}
    if step.get("keep_tools"):
        params["keep_tools"] = True
    if (x.get("props") or {}).get("isNewComponent"):
        raise Skip("combine into a new component")
    out.append({"command": "solid.combine", "params": params})
    return {"target": target[0], "tools": tools, "operation": op}


def emit_hole(step, out, ctx):
    x = _x(step)
    p = x.get("props") or {}
    pos, d = _unwrap(p.get("position")), _unwrap(p.get("holeDiameter"))
    if not pos or not d:
        raise Skip("hole has no recorded position or diameter")
    params = {"position": pos, "diameter": d, "type": "simple"}
    # Fusion's hole types: 0 simple, 1 counterbore, 2 countersink.
    kind = p.get("holeType") or 0
    if kind == 1:
        cd, ch = _unwrap(p.get("counterboreDiameter")), _unwrap(p.get("counterboreDepth"))
        if not cd or not ch:
            raise Skip("counterbore has no recorded diameter or depth")
        params.update(type="counterbore", cb_diameter=cd, cb_depth=ch)
    elif kind == 2:
        cd, ca = _unwrap(p.get("countersinkDiameter")), _unwrap(p.get("countersinkAngle"))
        if not cd or not ca:
            raise Skip("countersink has no recorded diameter or angle")
        params.update(type="countersink", cs_diameter=cd, cs_angle=ca)
    elif kind != 0:
        raise Skip("hole type {} unknown".format(kind))
    # Tap types: 0 none, 1 clearance (a plain hole of that diameter), 2 tapped, 3 taper tapped.
    tap = p.get("holeTapType") or 0
    if tap == 2:
        des = ((p.get("tappedHoleInfo") or {}).get("threadDesignation") or "").strip()
        if des:
            params["thread"] = des
    elif tap not in (0, 1):
        raise Skip("taper tapped holes are not translated yet")
    direction = _unwrap(p.get("direction"))
    if direction:
        params["direction"] = direction
    ext = p.get("extentDefinition") or {}
    depth = _unwrap(ext.get("distance")) if isinstance(ext, dict) else None
    etype = ext.get("type") if isinstance(ext, dict) else None
    if depth:
        params["depth"] = depth
    elif etype not in ("ThroughAllExtentDefinition", "AllExtentDefinition"):
        raise Skip("hole extent {} is not translated yet".format(etype))
    tip = _unwrap(p.get("tipAngle"))
    if depth and tip and not str(tip).startswith("180"):
        if params["type"] == "simple":
            params["type"] = "drilled"
        params["tip_angle"] = tip
    out.append({"command": "solid.hole", "params": params})
    return {"diameter": d, "type": params["type"], "thread": params.get("thread")}


def emit_remove(step, out, ctx):
    x = _x(step)
    names = []
    try:
        names = _body_names((x.get("inputs") or {}).get("body"))
    except Skip:
        names = []
    if not names:
        # The removed body is the one that was there before the step and is not after it.
        after = step.get("bodies_after")
        before = ctx.get("fusion_bodies")
        if isinstance(after, list) and before:
            left = {b.get("name") for b in after}
            names = [b.get("name") for b in before if b.get("name") not in left]
            if not names and ctx.get("built") is not None:
                # Two bodies of one name (the halves of a split): tell them apart by shape.
                still = [_sig(b) for b in after]
                paired = match_bodies(before, ctx["built"])[0]
                sc = [n for i, b in enumerate(before) if _sig(b) not in still
                      for n in (paired.pairs[i] or [])]
                if sc:
                    out.append({"command": "solid.remove", "params": {"bodies": sc}})
                    return {"bodies": sc, "found_by": "shape"}
    if not names:
        raise Skip("remove has no recorded body")
    out.append({"command": "solid.remove", "params": {"bodies": [b for n in names for b in sc_bodies(n, ctx)]}})
    return {"bodies": names}


def emit_scale(step, out, ctx):
    x = _x(step)
    p, ins = x.get("props") or {}, x.get("inputs") or {}
    names = _body_names(ins.get("inputEntities"))
    if not names:
        raise Skip("scale has no recorded bodies")
    origin = _unwrap(ins.get("point"))
    if not origin:
        raise Skip("scale has no recorded base point")
    params = {"bodies": [b for n in names for b in sc_bodies(n, ctx)], "origin": origin}
    if p.get("scaleFactor"):
        params["factor"] = _unwrap(p["scaleFactor"])
    elif p.get("xScale"):
        params["factors"] = [_unwrap(p.get(k)) for k in ("xScale", "yScale", "zScale")]
    else:
        raise Skip("scale has no recorded factor")
    out.append({"command": "solid.scale", "params": params})
    return {"bodies": names}


def expand_linked(doc):
    """Schema 25 writes the bodies of a linked design once (`linked_sets`) and a marker
    {"linked_set": n} where they stand in a body list; put the records back."""
    sets = doc.get("linked_sets")
    if not isinstance(sets, dict):
        return doc

    def unpack(lst):
        out = []
        for b in lst:
            if isinstance(b, dict) and "linked_set" in b and "name" not in b:
                out.extend(sets.get(str(b["linked_set"])) or [])
            else:
                out.append(b)
        return out

    for rec in doc.get("timeline") or []:
        if isinstance(rec.get("bodies_after"), list):
            rec["bodies_after"] = unpack(rec["bodies_after"])
    if isinstance(doc.get("final_bodies"), list):
        doc["final_bodies"] = unpack(doc["final_bodies"])
    return doc


class Names(dict):
    """Fusion body name -> rebuilt body name. `matched` counts paired Fusion bodies, which is
    more than the entries when Fusion has two bodies of one name in one component (a split
    leaves "Body1" and "Body1"); the name then stands for the one paired last."""
    matched = 0
    pairs = None    # per Fusion body, in list order: the rebuilt bodies paired with it, or None


# 20 Nov: bodies that came in from one of Fusion's state files are Fusion's geometry, but the two
# kernels measure them apart (an IO shield in an assembly: same extent, 1510.3 against 1506.2 mm3,
# 0.27 %). When such a body is first paired, the difference is written down per rebuilt body name;
# later checks subtract it, so a step that really changes the body is still held to the strict
# limit. Thread id -> {rebuilt body name: SolidCraft volume minus Fusion volume}.
vol_offset = {}
IMPORT_VOLUME_TOL = 5e-3     # pairing a body straight out of Fusion's state file


def _sv(sb):
    """A rebuilt body's volume, less the measuring difference noted when it came from Fusion."""
    v = sb.get("volume_mm3")
    if v is None:
        return None
    return v - (vol_offset.get(threading.get_ident()) or {}).get(sb.get("name"), 0.0)


def match_bodies(fusion, built, fresh=()):
    """Pair Fusion's bodies with the rebuild's by extent and volume. -> ({fusion: built}, ok)

    fresh: names of rebuilt bodies that just came in from Fusion's state file (looser volume)."""
    names, used = Names(), set()
    names.pairs = [None] * len(fusion)
    for fi, fb in enumerate(fusion):
        bb, vol = fb.get("bbox_mm"), fb.get("volume_mm3")
        if not bb:
            continue
        size = max(max(bb[1][k] - bb[0][k] for k in range(3)), 1e-6)
        best, best_d = None, None
        for sb in built:
            if sb.get("name") in used:
                continue
            sbb = sb.get("bbox") or {}
            if not sbb.get("min"):
                continue
            d = max(max(abs(sbb["min"][k] - bb[0][k]), abs(sbb["max"][k] - bb[1][k]))
                    for k in range(3))
            sv = _sv(sb)
            if d > 1e-3 * size + 1e-3:
                continue
            # 19 Nov: the two kernels do not measure every body alike (an imported
            # extruder part: 0.37 % apart; a turntable and an extrusion mount: 0.21 %). With the
            # same extent and the same number of faces half a percent is allowed; a missing
            # hole or fillet changes the face count and keeps the strict limit.
            vtol = 5e-3 if fb.get("faces") and fb.get("faces") == sb.get("faces") else 2e-3
            if sb.get("name") in fresh:
                vtol = max(vtol, IMPORT_VOLUME_TOL)
            if vol and sv is not None and abs(sv - vol) > max(vtol * abs(vol), BODY_VOLUME_FLOOR_MM3):
                continue
            if best_d is None or d < best_d:
                best, best_d = sb.get("name"), d
        if best:
            names[fb.get("name")] = best
            names.matched += 1
            names.pairs[fi] = [best]
            used.add(best)
    # Crosslink 2026-11-16: Fusion's box of a body inside a rotated component is the box of the
    # turned local box, so it is too large (a fan turned 45 degrees reads 162 mm wide instead
    # of 115). Such a body is paired by volume and centre of mass, with the rebuilt body's
    # extent lying inside Fusion's.
    for fi, fb in enumerate(fusion):
        if names.pairs[fi] is not None:
            continue
        bb, vol, com = fb.get("bbox_mm"), fb.get("volume_mm3"), fb.get("center_of_mass")
        if not bb or not vol or not com:
            continue
        size = max(max(bb[1][k] - bb[0][k] for k in range(3)), 1e-6)
        tol = 1e-3 * size + 1e-2
        best, best_d = None, None
        for sb in built:
            sbb, sc, sv = sb.get("bbox") or {}, sb.get("center_of_mass"), _sv(sb)
            if sb.get("name") in used or not sbb.get("min") or not sc or sv is None:
                continue
            vt = IMPORT_VOLUME_TOL if sb.get("name") in fresh else 2e-3
            if abs(sv - vol) > max(vt * abs(vol), BODY_VOLUME_FLOOR_MM3):
                continue
            if not all(sbb["min"][k] >= bb[0][k] - tol and sbb["max"][k] <= bb[1][k] + tol
                       for k in range(3)):
                continue
            d = max(abs(sc[k] - com[k]) for k in range(3))
            # 19 Nov: Fusion's centre of mass is not exact (a lead screw nut turned 45
            # degrees: 0.21 mm off on a 31 mm box, volume equal to 0.004 %). Volume and extent
            # are already checked above, so one percent of the size is allowed here.
            if d <= max(tol, 1e-2 * size) and (best_d is None or d < best_d):
                best, best_d = sb.get("name"), d
        if best:
            names[fb.get("name")] = best
            names.matched += 1
            names.pairs[fi] = [best]
            used.add(best)
    # Crosslink 2026-11-15: one Fusion body can be several separate lumps (an imported QR code
    # is 68 modules in one body). A STEP import brings each lump in as a body of its own, so a
    # Fusion body left without a partner may be a group of rebuilt bodies: those inside its
    # extent whose volumes add up to its volume.
    groups = {}
    if names.matched < len(names):
        names.matched = len(names)
    for fi, fb in enumerate(fusion):
        if names.pairs[fi] is not None:
            continue
        bb, vol = fb.get("bbox_mm"), fb.get("volume_mm3")
        if not bb or not vol:
            continue
        size = max(max(bb[1][k] - bb[0][k] for k in range(3)), 1e-6)
        tol = 1e-3 * size + 1e-3
        inside = []
        for sb in built:
            sbb = sb.get("bbox") or {}
            if sb.get("name") in used or not sbb.get("min"):
                continue
            if all(sbb["min"][k] >= bb[0][k] - tol and sbb["max"][k] <= bb[1][k] + tol
                   for k in range(3)):
                inside.append(sb)
        if len(inside) < 2:
            continue

        def whole(group):
            total = sum(sb.get("volume_mm3") or 0 for sb in group)
            lo = [min(sb["bbox"]["min"][k] for sb in group) for k in range(3)]
            hi = [max(sb["bbox"]["max"][k] for sb in group) for k in range(3)]
            return (all(abs(lo[k] - bb[0][k]) <= tol and abs(hi[k] - bb[1][k]) <= tol
                        for k in range(3))
                    and abs(total - vol) <= max(2e-3 * abs(vol), BODY_VOLUME_FLOOR_MM3))

        fits = whole(inside)
        if not fits and len(inside) <= 12:
            # Crosslink 2026-11-16: lumps of a neighbouring body can lie inside this body's
            # extent as well (two mirrored halves overlap); look for the lumps that make it up.
            import itertools
            for n in range(len(inside) - 1, 1, -1):
                hit = next((g for g in itertools.combinations(inside, n) if whole(g)), None)
                if hit:
                    inside, fits = list(hit), True
                    break
        if fits:
            members = [sb.get("name") for sb in inside]
            names[fb.get("name")] = members[0]
            names.matched += 1
            names.pairs[fi] = members
            groups[fb.get("name")] = members
            used.update(members)
    ok = names.matched == len(fusion) and len(used) == len(built)
    return names, ok, groups


def explain_mismatch(fusion, built, names, groups):
    """Why the bodies do not pair up, in a few words per body (for the coverage report)."""
    # Crosslink 2026-11-16: by `pairs`, not by name: an imported assembly has "Body1" in every
    # component, and counting names made 130 paired bodies of a board look unpaired.
    pairs = getattr(names, "pairs", None) or [None] * len(fusion)
    used = {b for pr in pairs if pr for b in pr}
    left = [b for b in built if b.get("name") not in used and (b.get("bbox") or {}).get("min")]
    out = []
    for fi, fb in enumerate(fusion):
        if pairs[fi] or not fb.get("bbox_mm"):
            continue
        if fb.get("solid") is False and not left:
            out.append("{} ({}): surface body, not in what the STEP import brought in".format(
                fb.get("name"), fb.get("component")))
            if len(out) >= 3:
                break
            continue
        bb, vol = fb["bbox_mm"], fb.get("volume_mm3") or 0.0
        best = None
        for sb in left:
            d = max(max(abs(sb["bbox"]["min"][k] - bb[0][k]), abs(sb["bbox"]["max"][k] - bb[1][k]))
                    for k in range(3))
            if best is None or d < best[0]:
                best = (d, sb)
        if best is None:
            out.append("{}: no rebuilt body left for it".format(fb.get("name")))
        else:
            sv = best[1].get("volume_mm3") or 0.0
            out.append("{}: nearest is {}, extent off by {:.3f} mm, volume {:.3f} vs {:.3f} mm3 ({:+.2f} %)".format(
                fb.get("name"), best[1].get("name"), best[0], sv, vol,
                (sv - vol) / vol * 100.0 if vol else 0.0))
        if len(out) >= 3:
            break
    extra = len(built) - len(used)
    if extra > 0 and len(out) < 3:
        out.append("{} rebuilt bodies have no Fusion partner".format(extra))
    return out


def _plane_of(v):
    """{origin, normal} out of a described construction plane or planar face."""
    if isinstance(v, dict) and v.get("_") == "construction_plane" and v.get("origin") and v.get("normal"):
        return {"origin": v["origin"], "normal": v["normal"]}
    if isinstance(v, dict) and v.get("_") == "face":
        sf = v["face"].get("surface") or {}
        if sf.get("type") == "Plane" and sf.get("origin") and sf.get("normal"):
            return {"origin": sf["origin"], "normal": sf["normal"]}
    return None


def emit_mirror(step, out, ctx):
    x = _x(step)
    ins = x.get("inputs") or {}
    names = _body_names(ins.get("inputEntities"))
    plane = _plane_of(ins.get("mirrorPlane")) or _plane_of((x.get("props") or {}).get("mirrorPlane"))
    if not names or not plane:
        raise Skip("mirror has no recorded bodies or plane")
    params = {"bodies": [b for n in names for b in sc_bodies(n, ctx)], "plane": plane}
    if (x.get("props") or {}).get("isCombine"):
        params["combine"] = True
    out.append({"command": "solid.mirror", "params": params})
    return {"bodies": names}


def emit_split(step, out, ctx):
    x = _x(step)
    ins = x.get("inputs") or {}
    names = _body_names(ins.get("splitBodies"))
    plane = _plane_of(ins.get("splittingTool"))
    if not names:
        raise Skip("split has no recorded body")
    tool = None
    t = ins.get("splittingTool")
    if not plane and isinstance(t, dict) and t.get("_") == "face" and (t["face"].get("point")
                                                                      and t["face"].get("body")):
        # a curved face: SolidCraft splits along that face's surface, extended
        tool = {"body": sc_body(t["face"]["body"], ctx), "point": t["face"]["point"]}
    if not plane and not tool:
        raise Skip("split tool is not a plane or a face of a body")
    for n in names:
        params = {"body": sc_body(n, ctx)}
        if plane:
            params["plane"] = plane
        else:
            params["tool"] = tool
        out.append({"command": "solid.split_body", "params": params})
    return {"bodies": names, "tool": "plane" if plane else "face"}


def emit_copy_body(step, out, ctx):
    x = _x(step)
    names = _body_names((x.get("inputs") or {}).get("sourceBody") or (x.get("props") or {}).get("sourceBody"))
    if not names:
        raise Skip("copy has no recorded source body")
    out.append({"command": "solid.move", "params": {
        "bodies": [b for n in names for b in sc_bodies(n, ctx)], "copy": True,
        "matrix": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]}})
    return {"bodies": names}


def _pattern_subject(x, ctx):
    """What a pattern repeats -> {"features": [...]} or {"bodies": [...]} in the rebuild."""
    ie = (x.get("inputs") or {}).get("inputEntities") or (x.get("props") or {}).get("inputEntities")
    if not isinstance(ie, dict) or not ie.get("items"):
        raise Skip("pattern has no recorded inputs")
    if ie.get("count", 0) > len(ie["items"]):
        raise Skip("pattern of {} entities is larger than what was recorded".format(ie["count"]))
    kinds = {it.get("_") if isinstance(it, dict) else None for it in ie["items"]}
    if kinds == {"body"}:
        return {"bodies": [b for it in ie["items"] for b in sc_bodies(it["body"].get("name"), ctx)]}
    if kinds == {"object"}:
        feats = []
        for it in ie["items"]:
            nm = it.get("name")
            if not nm:
                raise Skip("pattern repeats a feature whose name was not recorded (before schema 21)")
            got = (ctx.get("sc_features") or {}).get(nm)
            if not got:
                raise Skip("pattern repeats {}, which is not in the rebuild".format(nm))
            feats.extend(got)
        return {"features": feats}
    raise Skip("pattern of {} is not translated yet".format(
        "/".join(sorted(str(k) for k in kinds))))


def _count_of(v):
    e = _unwrap(v)
    if e is None:
        raise Skip("pattern has no recorded quantity")
    return e


def emit_rect_pattern(step, out, ctx):
    x = _x(step)
    pr = x.get("props") or {}
    params = _pattern_subject(x, ctx)
    extent = pr.get("patternDistanceType") == 0      # 0: distance is first to last, 1: spacing
    for n, (dk, qk, sk, sym) in enumerate((
            ("directionOne", "quantityOne", "distanceOne", "isSymmetricInDirectionOne"),
            ("directionTwo", "quantityTwo", "distanceTwo", "isSymmetricInDirectionTwo")), 1):
        q = pr.get(qk) or {}
        if n == 2 and (q.get("value") or 1) <= 1:
            continue
        d = _unwrap(pr.get(dk))
        dist = _unwrap(pr.get(sk))
        if not d or dist is None:
            raise Skip("pattern direction {} was not recorded".format(n))
        count = _count_of(q)
        tag = "" if n == 1 else "2"
        params["dir{}".format(n)] = [round(c, 9) for c in d]
        params["count{}".format(n)] = count
        params["spacing{}".format(n)] = "({}) / (({}) - 1)".format(dist, count) if extent else dist
        if pr.get(sym):
            params["alignment" + tag] = "center"
    out.append({"command": "solid.pattern.rectangular", "params": params})
    return {"pattern": sorted(k for k in params if k in ("features", "bodies"))[0]}


def emit_circ_pattern(step, out, ctx):
    x = _x(step)
    pr = x.get("props") or {}
    params = _pattern_subject(x, ctx)
    ax = pr.get("axis") or (x.get("inputs") or {}).get("axis")
    if not isinstance(ax, dict):
        raise Skip("pattern axis was not recorded")
    kind = ax.get("_")
    if kind == "construction_axis" and ax.get("origin") and ax.get("direction"):
        params["axis"] = {"origin": ax["origin"], "dir": ax["direction"]}
    elif kind == "face" and (ax.get("face") or {}).get("point"):
        params["axis"] = {"face": ax["face"]["point"]}
    elif kind == "edge" and (ax.get("edge") or {}).get("point"):
        params["axis"] = {"edge": ax["edge"]["point"]}
    else:
        raise Skip("pattern axis {} is not translated yet".format(kind))
    params["count"] = _count_of(pr.get("quantity"))
    ang = _unwrap(pr.get("totalAngle"))
    if ang:
        params["angle"] = ang
    if pr.get("isSymmetric"):
        params["alignment"] = "center"
    out.append({"command": "solid.pattern.circular", "params": params})
    return {"pattern": sorted(k for k in params if k in ("features", "bodies"))[0]}


def _import_name(step, k=0):
    """A name of its own for the component an import makes. Left alone it is named after the
    file's product, which is the design or one of its components: several imports then share a
    name with each other and with the components the script creates (Skadis Tray: "ambiguous
    component", 7 matches)."""
    return "{} (Fusion state, step {}{})".format(step.get("name") or "import", step.get("index"),
                                                  "" if not k else ", part {}".format(k + 1))


def state_files(st, src_dir):
    """The STEP files that hold a recorded state: the export itself, unless Fusion left every
    body out of it (one design: 14 of 14, the file is an empty shell), and the file with the
    bodies written from their copies."""
    paths = []
    main = os.path.join(src_dir or "", st.get("file") or "")
    all_missing = st.get("missing") and len(st["missing"]) >= len(st.get("bodies") or [])
    if st.get("file") and os.path.exists(main) and not all_missing:
        paths.append(main)
    extra = os.path.join(src_dir or "", st.get("extra_file") or "")
    if st.get("extra_file") and os.path.exists(extra):
        paths.append(extra)
    return paths


def emit_state_step(step, out, ctx):
    """Crosslink 2026-11-15: bodies that came into Fusion from outside (base feature, derive).

    There is no recipe for them, so the extractor (schema 19) wrote the model as it stood right
    after the step as a STEP file. As the first modelling step that file is the imported bodies
    and nothing else, and it goes in as SolidCraft's Import base feature. Later in a timeline
    the file also holds everything built before it, and inserting it would double those bodies.
    """
    st = step.get("state_step") or {}
    if not st.get("file"):
        raise Skip("no exported state for this imported body ({})".format(
            st.get("error") or "extracted before schema 19"))
    path = os.path.join(ctx.get("src_dir") or "", st["file"])
    if not os.path.exists(path):
        raise Skip("exported state file {} is missing".format(st["file"]))
    detail = {"state_step": st["file"]}
    if ctx.get("fusion_bodies") and st.get("scope") not in ("feature", "changed"):
        # The file holds the whole model, not only the import (schema 19 files, and derive
        # steps, which do not name their bodies). With the engine at hand the doubles are
        # taken out again right after the insert; without it the step cannot be trusted.
        if not ctx.get("cli"):
            raise Skip("imported body arrives after other bodies; the exported state holds those too")
        detail["drop_doubles"] = True
    # Schema 22: bodies Fusion left out of that file are in a second one, written from copies.
    for k, one in enumerate(state_files(st, ctx.get("src_dir"))):
        out.append({"command": "file.insert_step", "params": {
            "path": one, "name": _import_name(step, k)}})
    if st.get("extra_file"):
        detail["extra_file"] = st["extra_file"]
    return detail


def stl_extent(path):
    """Largest extent of an STL's bounding box in the file's own units, or None."""
    import struct
    try:
        size = os.path.getsize(path)
        lo, hi = [float("inf")] * 3, [float("-inf")] * 3
        with open(path, "rb") as fh:
            head = fh.read(84)
            n = struct.unpack("<I", head[80:84])[0] if len(head) == 84 else 0
            if len(head) == 84 and size == 84 + n * 50 and n:
                for _ in range(n):
                    rec = fh.read(50)
                    v = struct.unpack("<12f", rec[:48])
                    for q in (v[3:6], v[6:9], v[9:12]):
                        for k in range(3):
                            lo[k] = min(lo[k], q[k])
                            hi[k] = max(hi[k], q[k])
            else:
                fh.seek(0)
                for line in fh.read().decode("ascii", "replace").splitlines():
                    w = line.split()
                    if len(w) == 4 and w[0] == "vertex":
                        for k in range(3):
                            lo[k] = min(lo[k], float(w[k + 1]))
                            hi[k] = max(hi[k], float(w[k + 1]))
        ext = max(hi[k] - lo[k] for k in range(3))
        return ext if ext == ext and ext not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def emit_untyped(step, out, ctx):
    """A step Fusion's API gives no type ("Feature"). A mesh import (schema 22 writes the mesh
    as a file) goes in as a mesh body; anything else has nothing to translate from."""
    files = [m for m in step.get("mesh_files") or [] if m.get("file")]
    if not files:
        raise Skip("no mapping for Feature yet")
    for m in files:
        path = os.path.join(ctx.get("src_dir") or "", m["file"])
        if not os.path.exists(path):
            raise Skip("mesh file {} is missing".format(m["file"]))
        params = {"path": path}
        want = m.get("extent_mm")
        got = stl_extent(path) if path.lower().endswith(".stl") else None
        if want and got:
            # which unit the file is written in: the one that gives the mesh its size in Fusion
            unit = min((("mm", 1.0), ("cm", 10.0), ("m", 1000.0), ("in", 25.4)),
                       key=lambda u: abs(got * u[1] - want))
            if abs(got * unit[1] - want) > 0.01 * want + 0.01:
                raise Skip("mesh file {} does not have the size the mesh had in Fusion".format(m["file"]))
            params["units"] = unit[0]
        out.append({"command": "file.insert_mesh", "params": params})
    return {"meshes": len(files)}


def emit_carried(step, out, ctx):
    """Construction planes, axes and points: no command of their own. The features that use
    them (sketches, mirrors, splits) carry the geometry itself, so the model is the same; what
    is lost is the named object to edit. Reported as `carried`, not as translated."""
    return {"carried": True}


# Fit points per curved piece of a letter outline. 17 instead of 9 changed no letter's volume
# on a business card design and made the build several times slower.
TEXT_SPLINE_POINTS = 9

# Two bodies count as the same when their volumes differ by no more than 0.2 %, or by this
# much for very small bodies: a 2 mm3 letter is 0.5 % off at a 0.01 mm3 difference.
BODY_VOLUME_FLOOR_MM3 = 0.02


def text_outline(prof, step, ctx, out):
    """Draw a sketch text as the outline curves Fusion made of it, in a sketch of its own.

    The words and the font are in the timeline too, but an outline is the same shape on any
    machine with any fonts. The text is then geometry, not editable text.
    """
    sk = (ctx.get("sketch_recs") or {}).get(prof.get("sketch")) or {}
    text = next((t for t in sk.get("texts") or [] if t.get("token") == prof.get("text_token")), None)
    spec = (ctx.get("plane_specs") or {}).get(prof.get("sketch"))
    if not text or not spec or not text.get("outline"):
        raise Skip("sketch text has no recorded outline")
    if text.get("outline_count", 0) > len(text["outline"]):
        raise Skip("sketch text outline is larger than what was recorded")
    out.append({"command": "sketch.create", "params": {"plane": spec}})
    ctx["use_sketch"] = "Sketch{}".format(sum(1 for c in out if c["command"] == "sketch.create"))
    for c in text["outline"]:
        t = c.get("type")
        if t == "Line3D":
            if math.hypot(c["start"][0] - c["end"][0], c["start"][1] - c["end"][1]) < 1e-7:
                continue
            out.append({"command": "sketch.line", "params": {"points": [c["start"][:2], c["end"][:2]]}})
        elif t == "Arc3D":
            a, b = (c["start"], c["end"]) if (c.get("normal") or [0, 0, 1])[2] >= 0 else (c["end"], c["start"])
            out.append({"command": "sketch.arc.center_point", "params": {
                "center": c["center"][:2], "start": a[:2], "end": b[:2]}})
        elif t == "Circle3D":
            out.append({"command": "sketch.circle.center", "params": {
                "center": c["center"][:2], "radius": c["radius_mm"]}})
        elif c.get("samples"):
            pts = [q[:2] for q in c["samples"]]
            if len(pts) > TEXT_SPLINE_POINTS:
                stp = (len(pts) - 1) / (TEXT_SPLINE_POINTS - 1.0)
                pts = [pts[int(round(k * stp))] for k in range(TEXT_SPLINE_POINTS)]
            out.append({"command": "sketch.spline.fit_point", "params": {"points": pts}})
        else:
            raise Skip("sketch text outline holds a {} that is not drawn yet".format(t))


lockstep_timeline = {}   # thread id -> SolidCraft's timeline of the last successful build
lockstep_snaps = {}      # thread id -> the last builds: [(commands, bodies as measured, features per body)]
lockstep_stats = {}      # thread id -> {"full": n, "fast": n, "measured": bodies measured on the fast path}
LOCKSTEP_FAST_BODIES = 150   # from this many bodies on, a check measures only what changed
LOCKSTEP_SNAPS = 4


def _cli_json(cli, args, cmds):
    """Run the CLI on a script. -> (parsed JSON, None) or (None, error)"""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"commands": cmds}, fh)
        p = subprocess.run([cli, args[0], tmp] + list(args[1:]), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=LOCKSTEP_TIMEOUT_S, cwd=os.path.dirname(cli) or None)
        txt = (p.stdout or "").strip()
        if not txt[:1] in ("{", "["):
            err = (p.stderr or p.stdout or "no output").strip().splitlines()
            return None, (err[0] if err else "no output")[:300]
        return json.loads(txt), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _snap(cmds, bodies, feats):
    snaps = lockstep_snaps.setdefault(threading.get_ident(), [])
    snaps.append((list(cmds), bodies, feats))
    del snaps[:-LOCKSTEP_SNAPS]


def _lockstep_fast(cli, cmds, snap):
    """Crosslink 2026-11-16: the check for a big assembly.

    Measuring is what a check costs: SolidCraft's eval takes the mass properties of every body,
    47 s on all cores for the 2,400 bodies of a motherboard, at every step (one assembly: 85 min
    for 55 steps). A step changes a handful of them. So from LOCKSTEP_FAST_BODIES bodies on, the
    script is run with two read-only commands at its end (the body names, and for each body the
    features that made or changed it), the engine's own word on what each new command created
    or changed is taken from its results, and only those bodies are measured in a second run.
    Every other body keeps the measurement of the last build this script is a continuation of.
    -> (bodies, None), (None, error), or None when the full check has to do it
    """
    old_cmds, old_bodies, old_feats = snap
    n = len(old_cmds)
    if any(c.get("command", "").startswith("component.") for c in cmds[n:]):
        return None          # a body in another component: names and places may differ
    res, err = _cli_json(cli, ["run", "--results"], cmds + [
        {"command": "document.inspect", "params": {"bodies": "names"}},
        {"command": "model.body_features", "params": {}}])
    if err:
        return None, err
    if not isinstance(res, list) or len(res) != len(cmds) + 2:
        return None
    doc, bf = res[-2] or {}, res[-1] or {}
    names = doc.get("bodies")
    if not isinstance(names, list) or len(set(names)) != len(names):
        return None
    feats = {b.get("body"): [f.get("id") for f in b.get("features") or []] for b in bf.get("bodies") or []}
    have = {b.get("name"): b for b in old_bodies}
    need = set()
    for r in res[n:len(cmds)]:
        if isinstance(r, dict):
            need.update(r.get("created_bodies") or [])
            need.update(r.get("changed_bodies") or [])
    for nm in names:
        if nm not in have or (old_feats is not None and feats.get(nm) != old_feats.get(nm)):
            need.add(nm)
    need = [nm for nm in names if nm in need]
    if len(need) * 2 > len(names):
        return None
    got = {}
    if need:
        res2, err2 = _cli_json(cli, ["run", "--results"], cmds + [
            {"command": "inspect.measure", "params": {"bodies": need}}])
        if err2 or not isinstance(res2, list) or not res2:
            return None
        got = {b.get("name"): b for b in (res2[-1] or {}).get("bodies") or []}
        if any(nm not in got for nm in need):
            return None
    bodies = [got.get(nm) or have[nm] for nm in names]
    lockstep_timeline[threading.get_ident()] = doc.get("timeline") or []
    _snap(cmds, bodies, feats)
    st = lockstep_stats.setdefault(threading.get_ident(), {})
    st["fast"] = st.get("fast", 0) + 1
    st["measured"] = st.get("measured", 0) + len(need)
    return [b for b in bodies if b.get("measured_by") != "mesh"], None


USE_SESSION = os.environ.get("CROSSLINK_SESSION", "1") != "0"
lockstep_sessions = {}   # thread id -> Session
READ_ONLY = ("sketch.inspect", "document.", "inspect.", "model.")
QUIET_COMMANDS = ("sketch.", "parameters.add", "component.create")   # change no body by themselves


class Session(object):
    """Crosslink 2026-11-16 (evening): one open SolidCraft design per translation thread.

    `solidcraft-cli eval` builds the whole script from nothing at every check and measures every
    body: 47 s for an assembly of 2,400 bodies, 35 s even when only the changed bodies are
    measured, at each of 55 steps. SolidCraft's headless session (`solidcraft-cli mcp
    --headless`) keeps the design open instead: a check applies only the commands that are new,
    takes back with undo the ones the script no longer has, and measures the bodies the engine
    says those commands created or changed. A step then costs milliseconds.

    What is kept: the commands applied, the design's fingerprint after each (timeline, body and
    sketch names), how many undo steps each took, and the last measurement of every body. If
    undo does not lead back to the recorded fingerprint, the session starts a new design and
    applies the script again. The finished script is still built from nothing by `run --save`
    and measured in full by `verify`, so the session only speeds up the steps in between.
    """

    def __init__(self, cli):
        self.cli = cli
        self.proc = subprocess.Popen([cli, "mcp", "--headless", "--allow-any-path"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=os.path.dirname(cli) or None)
        self.buf, self.rid = b"", 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "crosslink-translator", "version": "1"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
        self.proc.stdin.flush()
        self.fresh()

    def fresh(self):
        self.applied, self.undos, self.touched = [], [], []
        self.cache, self.stale, self.all_stale = {}, set(), False
        self.fps = [None]
        self.fps[0] = self.fingerprint()[0]

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass

    def rpc(self, method, params, timeout=None):
        import select
        import time
        timeout = timeout or LOCKSTEP_TIMEOUT_S   # read at call time: --verify-timeout changes it
        self.rid += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.rid, "method": method,
                                          "params": params}).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        end, fd = time.time() + timeout, self.proc.stdout.fileno()
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line.decode("utf-8"))
                if msg.get("id") == self.rid:
                    return msg
            left = end - time.time()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise TimeoutError("timeout")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise RuntimeError("the SolidCraft session ended")
            self.buf += chunk

    def call(self, tool, args):
        """-> (result, None) or (None, the engine's error text)"""
        msg = self.rpc("tools/call", {"name": tool, "arguments": args})
        if "error" in msg:
            return None, str((msg["error"] or {}).get("message"))
        res = msg.get("result") or {}
        text = "".join(c.get("text") or "" for c in res.get("content") or [])
        if res.get("isError"):
            return None, text
        try:
            return json.loads(text), None
        except ValueError:
            return {"text": text}, None

    def ex(self, command, params=None):
        return self.call("execute", {"command": command, "params": params or {}})

    def fingerprint(self):
        doc, err = self.ex("document.inspect", {"bodies": "names", "timeline": "summary"})
        if err:
            raise RuntimeError("document.inspect: " + err)
        fp = (tuple((t.get("id"), t.get("name")) for t in doc.get("timeline") or []),
              tuple(doc.get("bodies") or []),
              tuple(sk.get("name") if isinstance(sk, dict) else sk for sk in doc.get("sketches") or []),
              doc.get("active_sketch") if not isinstance(doc.get("active_sketch"), dict)
              else doc["active_sketch"].get("name"), len(doc.get("params") or []))
        return fp, doc.get("undo") or 0

    def touch(self, i):
        t = self.touched[i]
        if t is None:
            self.all_stale = True
        else:
            self.stale |= t

    def back_to(self, k):
        """Undo down to the first k commands. -> True, or False when undo did not get there"""
        steps = 0
        for i in range(len(self.applied) - 1, k - 1, -1):
            if self.undos[i] is None:
                return False
            steps += self.undos[i]
            self.touch(i)
        for _ in range(steps):
            _r, err = self.call("undo", {})
            if err:
                return False
        if self.fingerprint()[0] != self.fps[k]:
            return False
        del self.applied[k:], self.undos[k:], self.touched[k:], self.fps[k + 1:]
        return True

    def apply(self, c):
        """One more command. -> None or the error, worded as the CLI words a script's"""
        name = c.get("command") or ""
        before = self.fingerprint()
        res, err = self.ex(name, c.get("params") or {})
        if err:
            if self.fingerprint()[0] != before[0]:
                raise RuntimeError("a refused command left something behind")
            return "error: step {} ({}): {}".format(len(self.applied) + 1, name, err)
        after = self.fingerprint()
        if name.startswith(READ_ONLY):
            n, t = 0, set()
        else:
            n = after[1] - before[1]
            if n < 1:
                # the undo history is full (it holds 200 steps) or the count did not move
                n = 1 if after[1] >= 200 else (0 if after[0] == before[0] else None)
            res = res if isinstance(res, dict) else {}
            if name == "body.rename":
                t = {(c.get("params") or {}).get("body"), (c.get("params") or {}).get("name")}
            elif any(k in res for k in ("created_bodies", "changed_bodies", "removed_bodies")):
                t = set(res.get("created_bodies") or []) | set(res.get("changed_bodies") or []) \
                    | set(res.get("removed_bodies") or [])
            elif name.startswith(QUIET_COMMANDS):
                t = set()
            else:
                t = None          # not known what it changed: measure everything
        self.applied.append(c)
        self.undos.append(n)
        self.touched.append(t)
        self.fps.append(after[0])
        self.touch(len(self.applied) - 1)
        return None

    def build(self, cmds):
        """Bring the open design to this script and measure. -> (bodies, timeline, None) or (None, None, error)"""
        k = 0
        while k < len(self.applied) and k < len(cmds) and self.applied[k] == cmds[k]:
            k += 1
        if k < len(self.applied) and not self.back_to(k):
            _r, err = self.call("new_design", {})
            if err:
                raise RuntimeError("new_design: " + err)
            self.fresh()
        for c in cmds[len(self.applied):]:
            err = self.apply(c)
            if err:
                return None, None, err
        got, err = self.ex("inspect.measure", {"names_only": True})
        if err:
            raise RuntimeError("inspect.measure: " + err)
        world = got.get("bodies") or []
        names = [b.get("name") for b in world]
        if len(set(names)) != len(names):
            raise RuntimeError("two bodies of one name")
        if self.all_stale:
            self.cache = {}
        need = [b["name"] for b in world if b["name"] not in self.cache or b["name"] in self.stale
                or b.get("definition") in self.stale]
        for i in range(0, len(need), 400):
            got, err = self.ex("inspect.measure", {"bodies": need[i:i + 400]})
            if err:
                raise RuntimeError("inspect.measure: " + err)
            for b in got.get("bodies") or []:
                self.cache[b.get("name")] = b
        if any(nm not in self.cache for nm in names):
            raise RuntimeError("a body was not measured")
        self.cache = {nm: self.cache[nm] for nm in names}
        self.stale, self.all_stale = set(), False
        doc, err = self.ex("document.inspect", {"bodies": False})
        if err:
            raise RuntimeError("document.inspect: " + err)
        st = lockstep_stats.setdefault(threading.get_ident(), {})
        st["session"] = st.get("session", 0) + 1
        st["measured"] = st.get("measured", 0) + len(need)
        return [self.cache[nm] for nm in names], doc.get("timeline") or [], None


def end_session():
    s = lockstep_sessions.pop(threading.get_ident(), None)
    if s:
        s.close()


def _lockstep_session(cli, cmds):
    """-> (bodies, None), (None, error), or None when the session could not do it"""
    tid = threading.get_ident()
    try:
        s = lockstep_sessions.get(tid)
        if s is None:
            s = lockstep_sessions[tid] = Session(cli)
        bodies, tl, err = s.build(cmds)
        if err:
            return None, err.splitlines()[0][:300] if err.strip() else "no output"
        lockstep_timeline[tid] = tl
        return [b for b in bodies if b.get("measured_by") != "mesh"], None
    except Exception as e:
        end_session()
        st = lockstep_stats.setdefault(tid, {})
        st["session_failed"] = st.get("session_failed", 0) + 1
        st["session_error"] = "{}: {}".format(type(e).__name__, e)[:200]
        return None


def lockstep(cli, cmds):
    """Build the script so far. -> (bodies, None) or (None, error)"""
    if USE_SESSION:
        got = _lockstep_session(cli, cmds)
        if got is not None:
            return got
    snaps = lockstep_snaps.get(threading.get_ident()) or []
    snap = next((s for s in reversed(snaps) if len(s[0]) <= len(cmds) and cmds[:len(s[0])] == s[0]), None)
    if snap and len(snap[1]) >= LOCKSTEP_FAST_BODIES:
        fast = _lockstep_fast(cli, cmds, snap)
        if fast is not None:
            return fast
    res, err = _cli_json(cli, ["eval"], cmds)
    if err:
        return None, err
    if not isinstance(res, dict):
        return None, "no output"
    lockstep_timeline[threading.get_ident()] = res.get("timeline") or []
    bodies = res.get("bodies") or []
    if len(bodies) >= LOCKSTEP_FAST_BODIES:
        _snap(cmds, bodies, None)
    st = lockstep_stats.setdefault(threading.get_ident(), {})
    st["full"] = st.get("full", 0) + 1
    # An inserted mesh is listed among the bodies; Fusion's body lists hold solids only.
    return [b for b in bodies if b.get("measured_by") != "mesh"], None


def _sig(b):
    return (b.get("faces"), round(b.get("volume_mm3") or 0.0, 4), round(b.get("area_mm2") or 0.0, 4),
            tuple(round(v, 5) for c in (b.get("bbox_mm") or []) for v in c))


def use_frozen_state(step, cmds, mark, ctx, cli, rec):
    """Crosslink 2026-11-15 (evening, schema 22): take a step's result from Fusion.

    Where a step could not be translated, did not build, or built something else, and the
    extractor wrote the bodies that step made or changed as a STEP file, those bodies replace
    their counterparts in the rebuild: the old ones are removed, the file is inserted, and the
    script carries on from Fusion's own state. The step is then geometry without a recipe
    ("frozen"); the steps before and after it stay editable. -> True if the state was taken
    """
    st = step.get("state_step") or {}
    after = step.get("bodies_after")
    if st.get("frozen") and st.get("unchanged") and isinstance(after, list) and ctx.get("built") is not None \
            and rec.get("status") in ("untranslated", "build-failed", "error"):
        # Fusion says the step changed no solid body (a sketch, a position, a face moved by
        # nothing): there is no state to take over. If the rebuild still pairs with Fusion's
        # bodies, the step is left out and counted with the frozen ones.
        solid = [b for b in after if b.get("solid") is not False]
        names0, ok0, groups0 = match_bodies(solid, ctx["built"])
        if ok0:
            rec.update(status="frozen", frozen_because=rec.pop("reason", None), frozen_file=None,
                       left_out="changes no solid body")
            return True
        return False
    if not (st.get("frozen") and st.get("file")) or not isinstance(after, list):
        return False
    path = os.path.join(ctx.get("src_dir") or "", st["file"])
    if not os.path.exists(path):
        return False
    after = [b for b in after if b.get("solid") is not False]
    before = ctx.get("fusion_bodies") or []
    names, groups = ctx.get("names") or {}, ctx.get("groups") or {}
    now = {}
    for b in after:
        now.setdefault(b.get("name"), []).append(_sig(b))
    # counterparts of every body the step changed or used up
    old = []
    for b in before:
        if _sig(b) not in now.get(b.get("name"), []) and b.get("name") in names:
            old.extend(groups.get(b["name"]) or [names[b["name"]]])
    # 19 Nov: a step that built but did not match was paired after it was built, so its own new
    # bodies ("Body1 (2)" of a mirror) are among the counterparts. The step is undone below and
    # renaming them failed ("no body"): keep only bodies that stood before the step.
    if ctx.get("built_before") is not None:
        there = {b.get("name") for b in ctx["built_before"]}
        old = [o for o in old if o in there]
    saved = cmds[mark:]
    del cmds[mark:]
    if old:
        # The import that follows may give a new body the very name a removed one had, and a
        # name then means two bodies to every feature that uses it (Hose crank: the final
        # rename brought the removed bodies back). So the bodies leave under a name of their own.
        gone = []
        for n, o in enumerate(sorted(set(old))):
            # 19 Nov: a short name of its own. Bodies from an earlier Fusion state already carry
            # long names, and "<name> (replaced at step 8)" went past SolidCraft's 128 characters
            # (in one assembly the whole step was refused).
            tomb = "replaced at step {} #{}".format(step.get("index"), n)
            cmds.append({"command": "body.rename", "params": {"body": o, "name": tomb}})
            gone.append(tomb)
        cmds.append({"command": "solid.remove", "params": {"bodies": gone}})
    for k, one in enumerate(state_files(st, ctx.get("src_dir"))):
        cmds.append({"command": "file.insert_step", "params": {
            "path": one, "name": _import_name(step, k)}})
    built, err = lockstep(cli, cmds)
    ok = False
    if not err:
        had = {b.get("name") for b in (ctx.get("built_before") or [])} | \
              {b.get("name") for b in (ctx.get("built") or [])}
        fresh = {b.get("name") for b in built} - had
        names2, ok, groups2 = match_bodies(after, built, fresh)
        if not ok and names2.matched == len(after):
            # every Fusion body has its partner; what is left over are remains of steps that
            # went wrong earlier. Fusion's state is now in the model, so they can go.
            # 19 Nov: by pair, not by Fusion name. A file with a new copy of a component also
            # holds the copies already there, and in an assembly many bodies are called "Body1":
            # by name only one of them counted as used and paired bodies were removed.
            used = {b for pair in (names2.pairs or []) if pair for b in pair}
            left = [b.get("name") for b in built if b.get("name") not in used]
            cmds.append({"command": "solid.remove", "params": {"bodies": left}})
            built2, err2 = lockstep(cli, cmds)
            if err2:
                cmds.pop()
            else:
                built = built2
                names2, ok, groups2 = match_bodies(after, built, fresh)
                rec["leftovers_removed"] = len(left)
    if not ok:
        del cmds[mark:]
        cmds.extend(saved)
        rec["frozen_state_tried"] = err or "bodies still do not pair up"
        if not err:
            # 20 Nov: say which bodies did not pair, as the lockstep check does
            try:
                rec["frozen_state_why"] = explain_mismatch(after, built, names2, groups2)[:6]
                rec["frozen_state_counts"] = {"fusion": len(after), "built": len(built),
                                              "matched": names2.matched}
            except Exception as e:
                rec["frozen_state_why"] = ["explain failed: {}".format(e)]
        return False
    was = rec.get("status")
    rec.update(status="frozen", commands=len(cmds) - mark, frozen_file=st["file"],
               frozen_because=(rec.get("reason") or "built, but not what Fusion had") if was != "translated"
               else "built, but not what Fusion had")
    rec.pop("reason", None)
    # note how far SolidCraft measures each newly imported body from Fusion (see vol_offset)
    off = vol_offset.setdefault(threading.get_ident(), {})
    by_name = {b.get("name"): b for b in built}
    for fb, pair in zip(after, names2.pairs or []):
        if pair and len(pair) == 1 and pair[0] in fresh and fb.get("volume_mm3"):
            v = (by_name.get(pair[0]) or {}).get("volume_mm3")
            if v is not None:
                # also 0: a new body may take the name of one that had an offset
                off[pair[0]] = v - fb["volume_mm3"]
                if abs(off[pair[0]]) > 1e-9:
                    rec["volume_offsets"] = rec.get("volume_offsets", 0) + 1
    rec["lockstep"] = {"fusion_bodies": len(after), "built_bodies": len(built),
                       "matched": len(names2), "ok": True}
    ctx["built"], ctx["names"], ctx["groups"] = built, names2, groups2
    ctx["sc_tl_len"] = len(lockstep_timeline.get(threading.get_ident()) or [])
    return True


def finish_structure(cmds, ctx, cli):
    """Crosslink 2026-11-15 (evening): give the finished rebuild Fusion's structure.

    After the last step, every rebuilt body that is paired with a Fusion body goes into the
    component Fusion had it in (if the script made that component) and takes Fusion's body
    name. Done at the end, not along the way, so no step in between has to find a body in
    another component. Each of the two is built once and dropped again if the engine refuses.
    """
    fusion, names, groups = ctx.get("fusion_bodies") or [], ctx.get("names") or {}, ctx.get("groups") or {}
    built = {b.get("name") for b in ctx.get("built") or []}
    count = collections.Counter(b.get("name") for b in fusion)
    comps = ctx.get("components") or set()
    report = {"components": sorted(comps)}

    # Crosslink 2026-11-16: a rename at the end can change the bodies. The script names bodies
    # in its earlier commands, and with the new name in place one of them finds another body
    # (a pillar: one body of 5,073 mm3 became two after `body.rename` to "Body1"). So
    # the bodies are measured before, and a move or rename that changes them is taken back.
    def shapes(bodies):
        return sorted((b.get("faces") or 0, round(b.get("volume_mm3") or 0.0, 2),
                       tuple(round(v, 3) for k in ("min", "max")
                             for v in ((b.get("bbox") or {}).get(k) or [])))
                      for b in bodies or [])

    before, err0 = lockstep(cli, cmds)
    if err0:
        return report
    want_shapes = shapes(before)

    def unchanged(now):
        return shapes(now) == want_shapes

    by_comp = {}
    for fb in fusion:
        n = fb.get("name")
        if count[n] == 1 and n in names and fb.get("component") in comps:
            by_comp.setdefault(fb["component"], []).extend(groups.get(n) or [names[n]])
    # A component the engine cannot tell apart by name (an imported state brought one of the
    # same name) is left out and the others tried again.
    for _ in range(6):
        if not by_comp:
            break
        mark = len(cmds)
        for comp in sorted(by_comp):
            cmds.append({"command": "component.move_bodies",
                         "params": {"bodies": by_comp[comp], "component": comp}})
        now, err = lockstep(cli, cmds)
        if not err and not unchanged(now):
            del cmds[mark:]
            report["move_error"] = "moving bodies into components changed the bodies"
            break
        if not err:
            report["bodies_moved"] = sum(len(v) for v in by_comp.values())
            report.pop("move_error", None)
            break
        del cmds[mark:]
        report["move_error"] = err
        bad = set(re.findall(r"`([^`]+)`", err.split("component.move_bodies", 1)[-1]))
        hit = [c for c in by_comp if c in bad]
        if not hit:
            break
        for c in hit:
            by_comp.pop(c)

    # A name a removed body had is still held by its Remove feature and cannot be given again.
    gone = {b for c in cmds if c["command"] == "solid.remove" for b in c["params"].get("bodies") or []}
    renames = [(names[n], n) for n in sorted(count) if count[n] == 1 and n in names
               and n not in groups and names[n] != n and n not in built and n not in gone
               and names[n] not in gone]
    # One refused name must not cost the others: the engine names the body it refuses (a name
    # an earlier, removed body still holds, for one), that rename is left out and the rest tried
    # again, a few times at most.
    for _ in range(5):
        if not renames:
            break
        mark = len(cmds)
        for old, new in renames:
            cmds.append({"command": "body.rename", "params": {"body": old, "name": new}})
        now, err = lockstep(cli, cmds)
        if not err and not unchanged(now):
            # find the renames that leave the bodies alone, one at a time
            del cmds[mark:]
            kept = []
            for old, new in renames[:12]:
                cmds.append({"command": "body.rename", "params": {"body": old, "name": new}})
                now, err = lockstep(cli, cmds)
                if err or not unchanged(now):
                    cmds.pop()
                else:
                    kept.append((old, new))
            report["bodies_renamed"] = len(kept)
            report["renames_dropped_changing_bodies"] = len(renames) - len(kept)
            break
        if not err:
            report["bodies_renamed"] = len(renames)
            report.pop("rename_error", None)
            break
        del cmds[mark:]
        report["rename_error"] = err
        bad = set(re.findall(r"`([^`]+)`", err.split("body.rename", 1)[-1])) - {"body.rename"}
        kept = [(o, n) for o, n in renames if o not in bad and n not in bad]
        if len(kept) == len(renames):
            break
        report["names_kept_from_rebuild"] = report.get("names_kept_from_rebuild", 0) + len(renames) - len(kept)
        renames = kept
    return report


HANDLERS = {
    "Sketch": emit_sketch,
    "ExtrudeFeature": emit_extrude,
    "FilletFeature": emit_fillet,
    "ChamferFeature": emit_chamfer,
    "ShellFeature": emit_shell,
    "DraftFeature": emit_draft,
    "RevolveFeature": emit_revolve,
    "OffsetFacesFeature": emit_offset_faces,
    "BoxFeature": emit_box,
    "CylinderFeature": emit_cylinder,
    "MoveFeature": emit_move,
    "CombineFeature": emit_combine,
    "HoleFeature": emit_hole,
    "RemoveFeature": emit_remove,
    "ScaleFeature": emit_scale,
    "MirrorFeature": emit_mirror,
    "SplitBodyFeature": emit_split,
    "CopyPasteBody": emit_copy_body,
    "RectangularPatternFeature": emit_rect_pattern,
    "CircularPatternFeature": emit_circ_pattern,
    "SphereFeature": emit_sphere,
    "Feature": emit_untyped,
    "Occurrence": emit_occurrence,
    "BaseFeature": emit_state_step,
    "DeriveFeature": emit_state_step,
    "ConstructionPlane": emit_carried,
    "ConstructionAxis": emit_carried,
    "ConstructionPoint": emit_carried,
}


def disambiguate_bodies(doc):
    """Crosslink 2026-11-15 (evening): give bodies that share a name across components a name
    of their own ("Body1 [Lid]"). Fusion only keeps body names apart inside a component; the
    rebuild pairs bodies by name, and "Body1" of two components was one entry there
    (Skadis Tray: "a body cannot be combined with itself"). -> how many names were split"""
    where = {}

    def walk(o, fn):
        if isinstance(o, dict):
            if isinstance(o.get("name"), str) and isinstance(o.get("component"), str) and \
                    ("bbox_mm" in o or "solid" in o) and "entity" not in o:
                fn(o)
            for v in o.values():
                walk(v, fn)
        elif isinstance(o, list):
            for v in o:
                walk(v, fn)

    walk(doc.get("timeline") or [], lambda b: where.setdefault(b["name"], set()).add(b["component"]))
    shared = {n for n, comps in where.items() if len(comps) > 1}

    def rename(b):
        if b["name"] in shared:
            b["name"] = "{} [{}]".format(b["name"], b["component"])

    if shared:
        walk(doc.get("timeline") or [], rename)
    return len(shared)


def translate(doc, with_constraints=False, cli=None):
    """Timeline document -> (command script, coverage report)."""
    shared_names = disambiguate_bodies(doc)
    cmds = []
    steps = []
    ctx = {"sketches": set(), "constraints": with_constraints, "cli": cli,
           "src_dir": doc.get("_dir")}
    vol_offset[threading.get_ident()] = {}

    # Fusion's auto-named model parameters ("d4") are driven by features, not set by hand, so
    # they cannot be re-created as inputs. Where a user parameter leans on one, substitute its
    # measured value: the number survives even though the link to the feature does not.
    model = {m["name"]: m for m in doc.get("model_parameters", []) if m.get("name")}
    user_names = {p["name"] for p in doc.get("parameters", [])}
    substituted = {}

    def resolve(expression):
        if not expression:
            return expression, []
        used = []

        def sub(m):
            tok = m.group(0)
            if tok in user_names or tok in UNITS or tok not in model:
                return tok
            val = model[tok].get("value")
            if val is None:
                return tok
            used.append(tok)
            # Fusion reports lengths in centimetres; parameters here are written in mm.
            unit = (model[tok].get("unit") or "").strip()
            return "({} {})".format(round(val * (10.0 if unit in CM_UNITS else 1.0), 9),
                                    "mm" if unit in CM_UNITS else unit).rstrip() \
                if unit else "({})".format(round(val, 9))

        return re.sub(r"\b[A-Za-z_]\w*\b", sub, expression), used

    for p in order_parameters(doc.get("parameters", [])):
        expression, used = resolve(p["expression"])
        prm = {"name": p["name"], "expression": expression}
        if p.get("unit"):
            prm["unit"] = p["unit"]
        if p.get("comment"):
            prm["comment"] = p["comment"]
        if used:
            substituted[p["name"]] = used
        cmds.append({"command": "parameters.add", "params": prm})

    # Crosslink 2026-11-15 (night): a parameter the engine refuses ("cannot add a length and
    # a ...") made every later build of the script fail, so not one step of such a design could
    # be checked (LCD_ender_mount: 2 of 25). Build the parameters alone first and leave out the
    # ones that are refused; what used them falls back to its measured value or fails by itself.
    dropped_params = []
    if cli and cmds:
        for _ in range(600):
            _b, err = lockstep(cli, cmds)
            m = re.search(r"step (\d+) \(parameters\.add\)", err or "")
            if not m or not (1 <= int(m.group(1)) <= len(cmds)):
                break
            bad = cmds.pop(int(m.group(1)) - 1)
            dropped_params.append({"name": bad["params"].get("name"),
                                   "expression": bad["params"].get("expression"), "error": err[:160]})

    for step in doc.get("timeline", []):
        kind = step.get("entity") or "unknown"
        rec = {"index": step.get("index"), "name": step.get("name"), "entity": kind}

        if step.get("suppressed"):
            rec.update(status="skipped", reason="suppressed in Fusion")
            steps.append(rec)
            continue

        st0 = step.get("state_step") or {}
        if st0.get("covered_by") is not None:
            # Crosslink 2026-11-21 (schema 26, mesh base): a step before the last mesh step of
            # a design that was modelled on top of a converted mesh. Fusion's state after that
            # step holds what this one made; the step itself has no recipe here.
            rec.update(status="frozen", frozen_file=None,
                       left_out="in the mesh base state of step {}".format(st0["covered_by"]))
            steps.append(rec)
            continue

        handler = None if st0.get("mesh_base") else HANDLERS.get(kind)
        mark = len(cmds)
        # what stands before this step: a frozen state undoes the step, and then only these exist
        ctx["built_before"] = ctx.get("built")
        detail = {}
        checked_now = False
        try:
            ctx["out"] = cmds
            if handler is None:
                # no recipe for this kind of step; a frozen state may still stand in for it below
                if st0.get("mesh_base"):
                    raise Skip("mesh base: the model after the mesh steps comes from Fusion")
                raise Skip("no mapping for {} yet".format(kind))
            detail = handler(step, cmds, ctx) or {}
            rec.update(status="carried" if detail.get("carried") else "translated",
                       commands=len(cmds) - mark, **detail)
        except Skip as e:
            del cmds[mark:]  # never leave half a feature behind
            rec.update(status="untranslated", reason=str(e))
        except Exception as e:
            del cmds[mark:]
            rec.update(status="error", reason="{}: {}".format(type(e).__name__, e))

        # Crosslink 2026-11-15, lockstep: build after every translated step and compare with
        # what Fusion had at the same point. That names the step that goes wrong instead of
        # leaving a wrong final volume, tells which rebuilt body is which Fusion body (later
        # steps refer to bodies by name), and a step the engine refuses is taken out again so
        # the script as a whole still builds.
        if cli and rec.get("status") == "translated" and len(cmds) > mark:
            built, err = lockstep(cli, cmds)
            if err:
                del cmds[mark:]
                rec.update(status="build-failed", reason=err)
                if kind == "Sketch":
                    # a later feature must not reach for a sketch that is not in the script
                    nm = (step.get("sketch") or {}).get("name")
                    ctx["sketches"].discard(nm)
                    (ctx.get("sc_sketch") or {}).pop(nm, None)
            else:
                # Which of the rebuild's features this step became: a later pattern names them.
                tl = lockstep_timeline.get(threading.get_ident()) or []
                seen = ctx.setdefault("sc_tl_len", 0)
                made = [t.get("name") for t in tl[seen:] if t.get("type") != "Sketch" and t.get("name")]
                ctx["sc_tl_len"] = len(tl)
                if made:
                    ctx.setdefault("sc_features", {})[step.get("name")] = made
                if detail.get("drop_doubles"):
                    # A late import whose file holds the whole model: of the bodies that just
                    # came in, those that are bodies Fusion already had before this step are
                    # doubles of what the script built itself. Take them out again.
                    old = {b.get("name") for b in ctx.get("built") or []}
                    fresh = [b for b in built if b.get("name") not in old]
                    dn, _, dg = match_bodies(ctx.get("fusion_bodies") or [], fresh)
                    doubles = sorted({b for n in dn for b in (dg.get(n) or [dn[n]])})
                    if doubles:
                        cmds.append({"command": "solid.remove", "params": {"bodies": doubles}})
                        built2, err2 = lockstep(cli, cmds)
                        if err2:
                            cmds.pop()
                        else:
                            built = built2
                            rec["doubles_removed"] = len(doubles)
                ctx["built"] = built
                fb = step.get("bodies_after")
                if isinstance(fb, list):
                    checked_now = True
                    fb = [b for b in fb if b.get("solid") is not False]
                    names, ok, groups = match_bodies(fb, built)
                    if (not ok and kind.endswith("PatternFeature") and len(built) > len(fb)
                            and names.matched == len(fb)):
                        # Fusion lets single instances of a pattern be switched off, and the
                        # timeline does not say which. Every Fusion body has its partner, so
                        # the bodies left over are those instances: take them out again.
                        used = {b for n in names for b in (groups.get(n) or [names[n]])}
                        extra = [b.get("name") for b in built if b.get("name") not in used]
                        cmds.append({"command": "solid.remove", "params": {"bodies": extra}})
                        built2, err2 = lockstep(cli, cmds)
                        if err2:
                            cmds.pop()
                        else:
                            built = built2
                            names, ok, groups = match_bodies(fb, built)
                            rec["pattern_instances_removed"] = len(extra)
                    ctx["names"] = names
                    ctx["groups"] = groups
                    rec["lockstep"] = {"fusion_bodies": len(fb), "built_bodies": len(built),
                                       "matched": names.matched, "ok": ok}
                    if not ok:
                        rec["lockstep"]["why"] = explain_mismatch(fb, built, names, groups)
        if cli and (rec.get("status") in ("untranslated", "build-failed", "error")
                    or (rec.get("lockstep") and not rec["lockstep"]["ok"])):
            if use_frozen_state(step, cmds, mark, ctx, cli, rec):
                checked_now = True
        if isinstance(step.get("bodies_after"), list):
            ctx["fusion_bodies"] = [b for b in step["bodies_after"] if b.get("solid") is not False]
            if not checked_now and ctx.get("built") is not None:
                # No build at this step, but Fusion may have renamed a body or moved it into
                # a component: pair the names again against what stands in the rebuild.
                ctx["names"], _, ctx["groups"] = match_bodies(ctx["fusion_bodies"], ctx["built"])
        steps.append(rec)

    finish = finish_structure(cmds, ctx, cli) if cli and ctx.get("built") else {}

    done = sum(1 for s in steps if s["status"] in ("translated", "carried"))
    frozen = sum(1 for s in steps if s["status"] == "frozen")
    checked = [s["lockstep"]["ok"] for s in steps if s.get("lockstep")]
    report = {
        "lockstep_steps": len(checked),
        "lockstep_ok": sum(1 for v in checked if v),
        "first_divergence": next((s["name"] for s in steps
                                  if s.get("lockstep") and not s["lockstep"]["ok"]), None),
        "design": doc.get("name"),
        "path": doc.get("path"),
        "units": doc.get("units"),
        "parameters": len(doc.get("parameters", [])),
        "steps_total": len(steps),
        "steps_translated": done,
        "steps_frozen": frozen,
        "coverage": round(done / len(steps), 4) if steps else 0.0,
        "commands": len(cmds),
        "substituted_model_params": substituted,
        "structure": finish,
        "parameters_refused": dropped_params,
        # Fusion's manifest volume is of the visible bodies; the rebuild holds the hidden ones
        # too. With hidden bodies the last body comparison is the judge, not the volume.
        "fusion_hidden_bodies": sum(1 for b in ctx.get("fusion_bodies") or [] if b.get("visible") is False),
        "final_bodies_match": bool(ctx.get("built") is not None and ctx.get("fusion_bodies")
                                   and match_bodies(ctx["fusion_bodies"], ctx["built"])[1]),
        "body_names_shared_across_components": shared_names,
        "steps": steps,
    }
    return {"commands": cmds}, report


# ---------------------------------------------------------------------------
# sketches on faces
# ---------------------------------------------------------------------------
# Crosslink 2026-11-18: a sketch drawn on a face came across as a plane fixed in space (its
# frame), so the model was right but did not behave like Fusion's: make the coin thicker and
# the logo stays behind inside it. SolidCraft can attach a sketch to a face
# (`sketch.create {plane: {face: point}}`); the attached sketch then has the face's own axes,
# and `plane.anchor.x_axis` / `plane.anchor.coordinates` turn and move it back to the frame the
# sketch was drawn in. Done as a pass over the finished script: each sketch with a frame is
# tried, and the script is only kept when it builds the same bodies as before.
FACE_ATTACH = True
FACE_ATTACH_BUDGET_S = 150
FACE_ATTACH_MAX = 30


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _body_sig(cli, cmds):
    d, err = _cli_json(cli, ["eval"], cmds)
    if err or not isinstance(d, dict):
        return None
    sig = []
    for b in d.get("bodies") or []:
        bb = b.get("bbox") or {}
        sig.append((b.get("name"), b.get("volume_mm3") or 0.0, b.get("area_mm2") or 0.0,
                    list(bb.get("min") or []) + list(bb.get("max") or [])))
    return sorted(sig, key=lambda t: (str(t[0]), t[1]))


def _same_sig(a, b):
    if a is None or b is None or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x[0] != y[0] or len(x[3]) != len(y[3]):
            return False
        for u, v in ((x[1], y[1]), (x[2], y[2])):
            if abs(u - v) > max(1e-4, 1e-6 * abs(u)):
                return False
        if any(abs(u - v) > 1e-4 for u, v in zip(x[3], y[3])):
            return False
    return True


def _sketch_points(cmds, i, frame, limit=3):
    """World points of what is drawn in the sketch made by command i (first few)."""
    pts = []
    for c in cmds[i + 1:]:
        if not c["command"].startswith("sketch.") or c["command"] == "sketch.create":
            break
        p = c.get("params") or {}
        for q in [p.get("center")] + list((p.get("points") or [])[:1]) + [p.get("start")]:
            if isinstance(q, (list, tuple)) and len(q) >= 2 and all(isinstance(v, (int, float)) for v in q[:2]):
                pts.append([frame["origin"][k] + q[0] * frame["x_dir"][k] + q[1] * frame["y_dir"][k]
                            for k in range(3)])
        if len(pts) >= limit:
            break
    return pts[:limit]


def attach_faces(cmds, cli):
    """-> (commands, {"tried", "attached", ...}). The commands are the input when nothing held."""
    stats = {"tried": 0, "attached": 0}
    cand = [i for i, c in enumerate(cmds)
            if c["command"] == "sketch.create" and isinstance((c.get("params") or {}).get("plane"), dict)
            and "x_dir" in c["params"]["plane"] and "face" not in c["params"]["plane"]
            and len(c["params"]) == 1]
    if not cand or not any(not c["command"].startswith("sketch.") for c in cmds[:cand[-1]]):
        return cmds, stats
    if len(cand) > FACE_ATTACH_MAX:
        stats["skipped"] = "{} sketches".format(len(cand))
        return cmds, stats
    t0 = time.time()
    before = _body_sig(cli, cmds)
    if before is None:
        return cmds, stats
    if time.time() - t0 > FACE_ATTACH_BUDGET_S / 6.0:
        stats["skipped"] = "build takes {:.0f} s".format(time.time() - t0)
        return cmds, stats
    new = [dict(c) for c in cmds]
    why = collections.Counter()
    stats["why_not"] = why
    shift = 0
    done = []
    for i0 in cand:
        i = i0 + shift
        if time.time() - t0 > FACE_ATTACH_BUDGET_S:
            stats["out_of_time"] = True
            break
        if not any(not c["command"].startswith("sketch.") for c in new[:i]):
            continue                      # nothing built yet: there is no face to sit on
        fr = new[i]["params"]["plane"]
        xf, yf, of = fr["x_dir"], fr["y_dir"], fr.get("origin") or [0.0, 0.0, 0.0]
        nf = _cross(xf, yf)
        stats["tried"] += 1
        # which faces lie in the sketch's plane and look the same way: ask for the bodies there
        # are at this point of the script, then for their faces
        res, err = _cli_json(cli, ["run", "--results"],
                             new[:i] + [{"command": "inspect.measure", "params": {"names_only": True}}])
        try:
            bodies = [b["name"] for b in res[-1]["bodies"]][:40]
        except Exception:
            why[re.sub(r"step \d+ ", "", str(err or "no body list"))[:200]] += 1
            continue
        if not bodies:
            continue
        res, err = _cli_json(cli, ["run", "--results"], new[:i] + [
            {"command": "model.faces", "params": {"body": b}} for b in bodies])
        if err or not isinstance(res, list):
            why[re.sub(r"step \d+ ", "", str(err or "no face list"))[:200]] += 1
            continue
        near = _sketch_points(new, i, fr, 8) or [list(of)]
        mid = [sum(q[k] for q in near) / len(near) for k in range(3)]
        faces = []
        for rb in res[-len(bodies):]:
            for f in (rb or {}).get("faces") or []:
                n, q = f.get("plane_normal"), f.get("point")
                if not n or not q or _dot(n, nf) < 0.999999:
                    continue
                if abs(_dot([q[k] - of[k] for k in range(3)], nf)) > 1e-5:
                    continue
                c = f.get("centroid") or q
                faces.append((sum((c[k] - mid[k]) ** 2 for k in range(3)), rb.get("body"), f))
        if not faces:
            why["no face in the sketch's plane"] += 1
            continue
        faces.sort(key=lambda t: t[0])
        for _d, body, f in faces[:2]:
            probe = new[:i] + [{"command": "sketch.create",
                                "params": {"plane": {"face": f["point"], "body": body, "index": f["index"]},
                                           "project_edges": False}}]
            res1, err = _cli_json(cli, ["run", "--results"], probe)
            if err or not isinstance(res1, list) or not isinstance(res1[-1], dict):
                why[re.sub(r"step \d+ ", "", str(err or "no reply"))[:200]] += 1
                continue
            if res1[-1].get("attachment") != "exact_face" or res1[-1].get("sketch") is None:
                why["attachment: {}".format(res1[-1].get("attachment"))] += 1
                continue
            sid = res1[-1]["sketch"]
            res2, err = _cli_json(cli, ["run", "--results"],
                                  probe + [{"command": "timeline.inspect", "params": {"feature": sid}}])
            if err or not isinstance(res2, list):
                why[re.sub(r"step \d+ ", "", str(err or "no reply"))[:200]] += 1
                continue
            try:
                pl = res2[-1]["definition"]["plane"]["plane"]
                xa, ya, oa = pl["x_dir"], pl["y_dir"], pl["origin"]
            except Exception:
                why["no plane in the reply"] += 1
                continue
            na = _cross(xa, ya)
            d = [of[k] - oa[k] for k in range(3)]
            if _dot(nf, na) < 0.999999 or abs(_dot(d, na)) > 1e-6:
                why["other side of the face" if _dot(nf, na) < 0 else "not this plane"] += 1
                continue
            edit = {"command": "timeline.edit", "params": {"feature": sid, "set": {
                "plane.anchor.x_axis": [round(_dot(xf, xa), 12), round(_dot(xf, ya), 12)],
                "plane.anchor.coordinates": [round(_dot(d, xa), 9), round(_dot(d, ya), 9)]}}}
            trial = new[:i] + [probe[-1], edit] + new[i + 1:]
            # each attachment on its own must leave the bodies as they were (one that does not
            # used to take all the others of the design down with it)
            if not _same_sig(before, _body_sig(cli, trial)):
                why["bodies differ with it"] += 1
                continue
            new = trial
            shift += 1
            done.append(i)
            break
    if not done:
        return cmds, stats
    if _same_sig(before, _body_sig(cli, new)):
        stats["attached"] = len(done)
        return new, stats
    # the bodies differ with the attachments in: keep the script as it was
    stats["reverted"] = len(done)
    return cmds, stats


def verify(cli, script_path):
    """Build the translated script and report what SolidCraft actually made of it."""
    # The CLI runs from its own directory, so the script must be addressed absolutely.
    script_path = os.path.abspath(script_path)
    try:
        p = subprocess.run([cli, "eval", script_path], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=VERIFY_TIMEOUT_S, cwd=os.path.dirname(cli) or None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    txt = (p.stdout or "").strip()
    if not txt.startswith("{"):
        err = (p.stderr or p.stdout or "no output").strip().splitlines()
        return {"ok": False, "error": (err[0] if err else "no output")[:300]}
    d = json.loads(txt)
    t = d.get("total") or {}
    meshes = [b for b in d.get("bodies") or [] if b.get("measured_by") == "mesh"]
    if meshes:
        # Fusion's volume is of its solid bodies; leave inserted meshes out of the comparison.
        solid = [b for b in d.get("bodies") or [] if b.get("measured_by") != "mesh"]
        t = {"volume_mm3": sum(b.get("volume_mm3") or 0.0 for b in solid),
             "area_mm2": sum(b.get("area_mm2") or 0.0 for b in solid),
             "faces": sum(b.get("faces") or 0 for b in solid)}
        d = dict(d, body_count=len(solid))
    # Getting measurements back at all means every command ran: a failing step makes the CLI
    # report the error instead. Zero bodies is a legitimate result -- plenty of real designs are
    # sketch-only references with no solid in them -- so it is reported, not treated as failure.
    return {"ok": True, "bodies": d.get("body_count") or 0, "meshes": len(meshes),
            "solids": bool(d.get("body_count")),
            "volume_mm3": t.get("volume_mm3"), "area_mm2": t.get("area_mm2"),
            "faces": t.get("faces")}


def fusion_volumes(export_dir):
    """Fusion's own volume per design, from the export manifest.

    Building without error only proves a script ran; it says nothing about whether the result is
    the part you started with. A rebuilt timeline and the STEP export of the same design should
    agree on how much material there is, and that is the only check here that can actually catch
    a translation that is quietly wrong -- a missed cut, a fillet on the wrong edge, an extrude
    that went the other way.
    """
    manifest = os.path.join(export_dir, "manifest.json")
    if not os.path.exists(manifest):
        return {}
    out = {}
    try:
        with open(manifest, encoding="utf-8") as fh:
            for r in json.load(fh).get("designs", []):
                m = r.get("measure") or {}
                if m.get("volume_mm3") and r.get("step"):
                    stem = os.path.splitext(r["step"].replace("\\", "/"))[0]
                    out[stem.lower()] = m["volume_mm3"]
    except Exception:
        return {}
    return out


# Volume on curved faces is measured from a tessellation; a plain cylinder reads 1.1e-4 under
# analytic, so this sits above that floor and far below anything a real mistranslation costs.
REBUILD_TOL = 1e-3

# Most steps per design the next extraction is asked to write a state for.
STATE_WANTED_CAP = 150


def compare_to_fusion(report, rel_stem, volumes):
    """Judge a rebuilt design against Fusion's measurement of the original."""
    want = report.get("fusion_final_volume_mm3") or volumes.get(rel_stem.lower())
    v = report.get("verify") or {}
    if v.get("error") == "timeout":
        # Crosslink 2026-11-21: a build that ran out of time measured nothing; no grade (an
        # import was judged "volume-mismatch" here, and the retry at the end of a run missed it)
        report.pop("fidelity", None)
        return
    if report.get("import_bodies"):
        # imported with its hidden bodies: Fusion's volume in the manifest is of the visible
        # ones only, so this is judged body by body instead
        im = report.get("import_meshes") or {}
        meshes_ok = (not im or (im["fusion"] == im["inserted"]
                                and (v.get("meshes") or 0) == im["fusion"]))
        report["fidelity"] = ("imported-as-is" if report["import_bodies"]["ok"] and meshes_ok
                              else "volume-mismatch")
        return
    if (v.get("ok") and report.get("fusion_final_bodies") == 0 and report.get("fusion_final_meshes")
            and not v.get("bodies")):
        # Crosslink 2026-11-16: a design with history whose result is meshes only (a mesh
        # inserted as its one step) has no volume to compare; it is judged by its meshes.
        if (v.get("meshes") or 0) == report["fusion_final_meshes"]:
            total = report.get("steps_total") or 0
            report["judged_by"] = "meshes (design has no solid or surface body)"
            if report["coverage"] >= 1.0:
                report["fidelity"] = "exact"
            elif total and report["steps_translated"] + (report.get("steps_frozen") or 0) >= total:
                report["fidelity"] = "exact-with-frozen-steps"
            else:
                report["fidelity"] = "partial-but-matching"
        else:
            report["fidelity"] = "volume-mismatch"
        return
    if want is None or not v.get("ok"):
        return
    if not v.get("volume_mm3"):
        if v.get("bodies"):
            # bodies stand there but SolidCraft measures no volume for them
            report["fusion_volume_mm3"] = want
            report["fidelity"] = "volume-mismatch"
        return
    got = v["volume_mm3"]
    drift = abs(got - want) / want if want else None
    report["fusion_volume_mm3"] = want
    report["volume_drift"] = drift
    if drift is not None and drift <= REBUILD_TOL:
        # Only a design whose every step translated can be called a faithful rebuild; a partial
        # one matching by luck is still partial. In between (schema 22): every step is either
        # translated or taken from Fusion as a frozen state.
        total = report.get("steps_total") or 0
        if report.get("direct_import"):
            report["fidelity"] = "imported-as-is"
        elif report["coverage"] >= 1.0:
            report["fidelity"] = "exact"
        elif total and report["steps_translated"] + (report.get("steps_frozen") or 0) >= total:
            report["fidelity"] = "exact-with-frozen-steps"
        else:
            report["fidelity"] = "partial-but-matching"
    elif (report.get("fusion_hidden_bodies") and report.get("final_bodies_match")
          and report.get("lockstep_steps") and report["lockstep_ok"] == report["lockstep_steps"]):
        total = report.get("steps_total") or 0
        report["judged_by"] = "bodies (design has hidden bodies)"
        if report["coverage"] >= 1.0:
            report["fidelity"] = "exact"
        elif total and report["steps_translated"] + (report.get("steps_frozen") or 0) >= total:
            report["fidelity"] = "exact-with-frozen-steps"
        else:
            report["fidelity"] = "partial-but-matching"
    else:
        report["fidelity"] = "volume-mismatch"


# Grades whose .sc is the design as Fusion has it (with history, or as exact geometry).
GOOD_GRADES = ("exact", "exact-with-frozen-steps", "partial-but-matching", "imported-as-is")
GEOMETRY_TOL = 5e-3      # a STEP import measured by the other kernel (see IMPORT_VOLUME_TOL)


def geometry_fallback(timeline_path, out_path, report, cli, export_step=None):
    """Crosslink 2026-11-21: every design gets a SolidCraft file that is right.

    A rebuild that does not match Fusion is still saved as <design>.sc (it may be close, and its
    history is worth looking at), but it is not the design. Next to it goes <design>.geometry.sc:
    Fusion's complete final state (every body, hidden ones too, and the meshes) imported as it is.
    No history, but the exact shape. The report says which file to trust. A design that matches
    gets no such file (an old one is removed)."""
    stem = os.path.splitext(out_path)[0]
    geo_json, geo_sc = stem + ".geometry.json", stem + ".geometry.sc"
    if report.get("direct_import") or report.get("fidelity") in GOOD_GRADES:
        for f in (geo_json, geo_sc):
            if os.path.exists(f):
                os.remove(f)
        report.pop("geometry_sc", None)
        return
    if not cli or not os.path.exists(cli):
        return
    try:
        with open(timeline_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return
    folder = os.path.dirname(os.path.abspath(timeline_path))
    final = os.path.join(folder, (doc.get("final_state") or {}).get("file") or "")
    if (doc.get("final_state") or {}).get("file") and os.path.exists(final):
        source, whole = "final state", final
    elif export_step and os.path.exists(export_step):
        source, whole = "safety export (visible bodies only)", export_step
    else:
        report["geometry_sc"] = {"error": "no STEP file of the design"}
        return
    cmds = []
    if not (source == "final state" and doc.get("final_bodies") == []):
        cmds.append({"command": "file.insert_step", "params": {"path": whole}})
    if source == "final state":
        for m in doc.get("final_meshes") or []:
            mp = os.path.join(folder, m.get("file") or "")
            if m.get("file") and os.path.exists(mp):
                cmds.append({"command": "file.insert_mesh", "params": {"path": mp}})
    if not cmds:
        return
    with open(geo_json, "w", encoding="utf-8") as fh:
        json.dump({"commands": cmds}, fh, indent=2)
    v = verify(cli, geo_json)
    rec = {"source": source}
    if not v.get("ok"):
        rec["error"] = v.get("error")
    else:
        try:
            subprocess.run([cli, "run", os.path.abspath(geo_json), "--save", os.path.abspath(geo_sc),
                            "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                           cwd=os.path.dirname(cli) or None)
        except Exception as e:
            rec["error"] = "save: {}".format(e)
        want = report.get("fusion_final_volume_mm3") or report.get("fusion_volume_mm3")
        if want and v.get("volume_mm3"):
            rec["volume_drift"] = abs(v["volume_mm3"] - want) / want
            rec["matches"] = rec["volume_drift"] <= GEOMETRY_TOL
        rec["bodies"], rec["meshes"] = v.get("bodies"), v.get("meshes")
    rec["file"] = os.path.basename(geo_sc) if os.path.exists(geo_sc) else None
    report["geometry_sc"] = rec
    with open(stem + ".coverage.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


def one(path, out_path, cli, do_verify, with_constraints=False, export_step=None):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    expand_linked(doc)
    lockstep_snaps.pop(threading.get_ident(), None)
    lockstep_stats.pop(threading.get_ident(), None)
    end_session()
    doc["_dir"] = os.path.dirname(os.path.abspath(path))
    script, report = translate(doc, with_constraints,
                               cli if cli and os.path.exists(cli) else None)
    # Crosslink 2026-11-15 (evening): a design without a timeline (direct modelling, or an
    # imported file kept as it came) has no history in Fusion either, and one with mesh
    # features has a timeline Fusion cannot be rolled back through, so none of its steps
    # could be read. The migration of both is the geometry itself, as one Import feature: the
    # complete final state the extractor wrote (schema 22, hidden bodies included), or else
    # the STEP file of the safety export (visible bodies).
    if isinstance(doc.get("final_bodies"), list) and doc["final_bodies"]:
        # Crosslink 2026-11-16: the volume of all bodies at the end of the timeline, hidden ones
        # included (schema 23). The export manifest's volume is of the visible bodies only.
        report["fusion_final_volume_mm3"] = sum(b.get("volume_mm3") or 0.0 for b in doc["final_bodies"])
        report["fusion_final_bodies"] = len(doc["final_bodies"])
    elif isinstance(doc.get("final_bodies"), list):
        report["fusion_final_bodies"] = 0
    if isinstance(doc.get("final_meshes"), list):
        report["fusion_final_meshes"] = len(doc["final_meshes"])
    if doc.get("mesh_base"):
        report["mesh_base"] = doc["mesh_base"]
    final = os.path.join(doc["_dir"], (doc.get("final_state") or {}).get("file") or "")
    whole = final if (doc.get("final_state") or {}).get("file") and os.path.exists(final) else \
        (export_step if export_step and os.path.exists(export_step) else None)
    if whole and (not doc.get("timeline") or doc.get("rollback_skipped")):
        fb = doc.get("final_bodies")
        # Crosslink 2026-11-16: a design of mesh bodies only has a final STEP file without a
        # single solid, which SolidCraft refuses; its migration is the meshes alone.
        mesh_only = whole == final and isinstance(fb, list) and not fb
        cmds = [] if mesh_only else [{"command": "file.insert_step", "params": {"path": whole}}]
        meshes_wanted = 0
        for m in doc.get("final_meshes") or []:
            mp = os.path.join(doc["_dir"], m.get("file") or "")
            if m.get("file") and os.path.exists(mp):
                cmds.append({"command": "file.insert_mesh", "params": {"path": mp}})
                meshes_wanted += 1
        if whole == final and isinstance(doc.get("final_meshes"), list):
            report["import_meshes"] = {"fusion": len(doc["final_meshes"]), "inserted": meshes_wanted}
        script = {"commands": cmds}
        report.update(direct_import=True, commands=len(cmds),
                      import_source="final state" if whole == final else "safety export",
                      no_history_because=doc.get("timeline_error") or doc.get("rollback_skipped")
                      or "empty timeline")
        if not doc.get("timeline"):
            report["coverage"] = 1.0
        if whole == final and isinstance(fb, list) and cli and os.path.exists(cli) and cmds:
            built, err = lockstep(cli, cmds)
            if not err:
                # surface bodies (a sheet of an imported assembly) come through STEP as well,
                # so all of Fusion's bodies are compared here, not only the solids
                names, ok, _g = match_bodies(fb, built)
                unmeasured = 0
                if not ok:
                    # A surface body SolidCraft imports but cannot measure (no extent, no
                    # volume) has nothing to be paired by; it counts as there when the bodies
                    # left over on both sides are of that kind and equally many.
                    used = {b for pr in names.pairs or [] if pr for b in pr}
                    f_left = [b for k, b in enumerate(fb) if not (names.pairs or [None] * len(fb))[k]]
                    s_left = [b for b in built if b.get("name") not in used]
                    if (f_left and len(f_left) == len(s_left)
                            and all(b.get("solid") is False for b in f_left)
                            and all(not (b.get("bbox") or {}).get("min") for b in s_left)):
                        ok, unmeasured = True, len(f_left)
                if not ok:
                    report["import_why"] = explain_mismatch(fb, built, names, _g)
                report["import_bodies"] = {"fusion": len(fb), "built": len(built),
                                           "matched": names.matched, "ok": ok}
                if unmeasured:
                    report["import_bodies"]["surface_bodies_unmeasured"] = unmeasured

    if (FACE_ATTACH and cli and os.path.exists(cli) and not report.get("direct_import")
            and isinstance(script, dict) and script.get("commands")):
        try:
            script["commands"], fa = attach_faces(script["commands"], cli)
            if fa.get("tried") or fa.get("skipped"):
                report["face_sketches"] = fa
        except Exception as e:
            report["face_sketches"] = {"error": "{}: {}".format(type(e).__name__, e)}

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(script, fh, indent=2)
    with open(os.path.splitext(out_path)[0] + ".coverage.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if do_verify:
        report["verify"] = verify(cli, out_path)
        # Crosslink 2026-11-15: keep the design itself, not only the script that makes it, so
        # it can be opened in SolidCraft: <design>.sc next to the script. Partial rebuilds are
        # saved too; the report says which they are.
        sc_path = os.path.splitext(out_path)[0] + ".sc"
        if (report["verify"] or {}).get("bodies") or (report["verify"] or {}).get("meshes"):
            try:
                # Crosslink 2026-11-16: --results instead of --quiet: file.insert_step answers
                # with warnings (a body imported as surfaces, a body left out and why), which
                # are SolidCraft's own reasons for an import that differs from Fusion.
                r = subprocess.run([cli, "run", os.path.abspath(out_path), "--save", os.path.abspath(sc_path),
                                    "--results"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                   cwd=os.path.dirname(cli) or None)
                if r.returncode != 0 and not os.path.exists(sc_path):
                    # SolidCraft writes nothing when a feature fails on replay; keep the design
                    # anyway (the bodies are what was measured) and say so in the report.
                    report["sc_replay_errors"] = (r.stderr or "").strip()[:800]
                    r = subprocess.run([cli, "run", os.path.abspath(out_path), "--save",
                                        os.path.abspath(sc_path), "--results", "--allow-errors"],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                       cwd=os.path.dirname(cli) or None)
                warned = []
                try:
                    results = json.loads(r.stdout)
                except Exception:
                    results = []
                for k, res in enumerate(results if isinstance(results, list) else []):
                    if isinstance(res, dict) and res.get("warnings"):
                        warned.append({"command": k, "file": os.path.basename(res.get("path") or ""),
                                       "count": len(res["warnings"]),
                                       "warnings": [str(w)[:400] for w in res["warnings"][:8]]})
                if warned:
                    report["import_warnings"] = warned
            except Exception:
                pass
            report["sc_file"] = os.path.basename(sc_path) if os.path.exists(sc_path) else None
        elif os.path.exists(sc_path):
            os.remove(sc_path)      # a design file from an earlier run that no longer builds
        with open(os.path.splitext(out_path)[0] + ".coverage.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if lockstep_stats.get(threading.get_ident()):
        report["checks"] = lockstep_stats.pop(threading.get_ident())
    lockstep_snaps.pop(threading.get_ident(), None)
    end_session()
    return report


def main():
    global LOCKSTEP_TIMEOUT_S, VERIFY_TIMEOUT_S
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".timeline.json file, or a folder with --all")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--all", action="store_true", help="translate every timeline under a folder")
    ap.add_argument("--outdir", default=os.path.join(HOME, "cad", "rebuilt"))
    ap.add_argument("--verify", action="store_true", help="build each script and measure it")
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--constraints", action="store_true",
                    help="also re-apply sketch constraints and dimensions "
                         "(parametric sketches, but an over-constrained one fails "
                         "the whole script)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="designs translated at the same time (with --all)")
    ap.add_argument("--against", default=os.path.join(HOME, "cad", "fusion-export"),
                    help="export tree whose manifest holds Fusion's own volumes")
    ap.add_argument("--verify-timeout", type=int, default=VERIFY_TIMEOUT_S, metavar="SECONDS",
                    help="longest a build check may take, for the final build and for each step "
                         "(default %(default)s). Raise it for big assemblies or a slow machine.")
    ap.add_argument("--no-retry", action="store_true",
                    help="with --all: do not try designs that timed out again one at a time")
    args = ap.parse_args()

    LOCKSTEP_TIMEOUT_S = VERIFY_TIMEOUT_S = max(1, args.verify_timeout)

    if args.verify and not os.path.exists(args.cli):
        sys.exit("solidcraft-cli not found at {}".format(args.cli))

    if args.all:
        root = os.path.abspath(os.path.expanduser(args.input))
        files = []
        for dp, _d, names in os.walk(root):
            for n in names:
                if n.endswith(".timeline.json"):
                    files.append(os.path.join(dp, n))
        if not files:
            sys.exit(
                "no .timeline.json files under {}\n\n"
                "{}\n"
                "Run the ExtractTimeline script inside Fusion first:\n"
                "  Utilities > ADD-INS > Scripts and Add-Ins > ExtractTimeline > Run\n"
                "It writes one .timeline.json per design to ~/cad/fusion-timeline.".format(
                    root,
                    "That folder does not exist yet." if not os.path.isdir(root)
                    else "That folder exists but is empty.",
                )
            )
        files.sort()

        volumes = fusion_volumes(args.against) if args.verify else {}
        if args.verify:
            print("{} designs have a Fusion volume to check against\n".format(len(volumes))
                  if volumes else
                  "no export manifest at {} -- builds will be checked for success only,\n"
                  "not for correctness. Run ExportAllDesigns in Fusion to enable that.\n"
                  .format(args.against))

        reports = []

        def work(f):
            rel = os.path.relpath(f, root).replace("\\", "/")
            stem = rel[: -len(".timeline.json")]
            out_path = os.path.join(args.outdir, stem + ".json")
            try:
                r = one(f, out_path, args.cli, args.verify, args.constraints,
                        os.path.join(args.against, stem + ".step"))
                # The document's own "path" is its folder, so two designs in one folder share it.
                # Carry the full stem as well, which is what identifies a design everywhere else
                # (the export manifest, the rebuilt scripts, this report).
                r["stem"] = stem
                if args.verify:
                    if volumes:
                        compare_to_fusion(r, stem, volumes)
                    geometry_fallback(f, out_path, r, args.cli, os.path.join(args.against, stem + ".step"))
                print("done {}".format(stem), file=sys.stderr, flush=True)
                return rel, stem, r, None
            except Exception as e:
                return rel, stem, None, e

        # Crosslink: the time goes into the engine's own processes, so threads are enough.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            results = list(pool.map(work, files))

        # Crosslink 2026-11-21: a build that runs out of time under load (several jobs, a busy
        # machine) usually finishes when the design is built alone. Try those designs again one
        # at a time with twice the time, so a full run needs no manual re-runs.
        def timed_out(r):
            return r is not None and ((r.get("verify") or {}).get("error") == "timeout"
                                      or any(s.get("reason") == "timeout" for s in r.get("steps") or []))
        again = [k for k, (_rel, _stem, r, _e) in enumerate(results) if timed_out(r)]
        if again and args.verify and not args.no_retry:
            LOCKSTEP_TIMEOUT_S = VERIFY_TIMEOUT_S = 2 * VERIFY_TIMEOUT_S
            print("{} design(s) ran out of time; trying them again one at a time ({} s per build)"
                  .format(len(again), VERIFY_TIMEOUT_S), file=sys.stderr, flush=True)
            end_session()
            for k in again:
                rel = results[k][0]
                res = work(os.path.join(root, rel))
                if res[2] is not None:
                    res[2]["retried_alone"] = True
                    if timed_out(res[2]):
                        res[2]["retried_alone"] = "timed out again"
                results[k] = res
            end_session()
        for rel, stem, r, err in results:
            if err is not None:
                print("FAIL {} - {}".format(rel, err))
                continue
            if args.verify and volumes:
                compare_to_fusion(r, stem, volumes)
            reports.append(r)
            v = r.get("verify") or {}
            vs = ""
            if args.verify:
                if not v.get("ok"):
                    vs = " | BUILD FAILED"
                elif r.get("fidelity") == "exact":
                    vs = " | MATCHES FUSION"
                elif r.get("fidelity") == "exact-with-frozen-steps":
                    vs = " | MATCHES FUSION ({} steps frozen)".format(r.get("steps_frozen"))
                elif r.get("fidelity") == "imported-as-is":
                    vs = " | no history in Fusion: imported as is, matches"
                elif r.get("fidelity") == "partial-but-matching":
                    vs = " | volume matches (partial)"
                elif r.get("fidelity") == "volume-mismatch":
                    vs = " | VOLUME OFF {:.2%}".format(r.get("volume_drift") or 0)
                else:
                    vs = " | built {} bodies".format(v.get("bodies"))
                if v.get("error") == "timeout":
                    vs = " | TIMED OUT (raise --verify-timeout)"
                if r.get("retried_alone"):
                    vs += " (built alone)" if r["retried_alone"] is True else " (alone too)"
            print("{:>5.0%}  {:>3}/{:<3} steps  {}{}".format(
                r["coverage"], r["steps_translated"], r["steps_total"], stem[:48], vs))

        os.makedirs(args.outdir, exist_ok=True)
        with open(os.path.join(args.outdir, "rebuild-report.json"), "w", encoding="utf-8") as fh:
            json.dump({"designs": reports}, fh, indent=2)

        # Crosslink 2026-11-15 (evening): what to ask Fusion for in the next extraction. Every
        # step that was not translated, did not build or built something else, and from the
        # first such step on every later step too (a step after a wrong one may only look
        # right). The extractor (schema 22) writes the bodies each listed step made or changed
        # as a STEP file; steps that change no body cost it nothing.
        wanted = {}
        for r in reports:
            if r.get("fidelity") in ("exact", "imported-as-is") or r.get("direct_import"):
                continue
            first = next((k for k, s in enumerate(r["steps"])
                          if s["status"] in ("untranslated", "build-failed", "error")
                          or (s.get("lockstep") and not s["lockstep"]["ok"])), None)
            if first is None:
                continue
            idx = [s["index"] for s in r["steps"][first:] if s["status"] != "skipped"
                   and s["index"] is not None]
            if idx:
                wanted[r["stem"]] = idx[:STATE_WANTED_CAP]
        with open(os.path.join(args.outdir, "state-wanted.json"), "w", encoding="utf-8") as fh:
            json.dump(wanted, fh, indent=1, ensure_ascii=False)
        print("\nstates to ask Fusion for: {} steps in {} designs (state-wanted.json)".format(
            sum(len(v) for v in wanted.values()), len(wanted)))

        tot = sum(r["steps_total"] for r in reports)
        got = sum(r["steps_translated"] for r in reports)
        print("\n{} designs, {}/{} timeline steps translated ({:.0%})".format(
            len(reports), got, tot, (got / tot) if tot else 0))

        checked = [r for r in reports if r.get("fidelity")]
        if checked:
            exact = sum(1 for r in checked if r["fidelity"] == "exact")
            print("\nwith frozen steps: {}, without history (imported as is): {}".format(
                sum(1 for r in checked if r["fidelity"] == "exact-with-frozen-steps"),
                sum(1 for r in checked if r["fidelity"] == "imported-as-is")))
            part = sum(1 for r in checked if r["fidelity"] == "partial-but-matching")
            off = sum(1 for r in checked if r["fidelity"] == "volume-mismatch")
            print("\nchecked against Fusion's own volumes: {} faithful, {} matching but "
                  "partial, {} wrong".format(exact, part, off))
            geo = [r for r in reports if (r.get("geometry_sc") or {}).get("file")]
            if geo:
                print("exact geometry saved as <design>.geometry.sc for {} designs that are not right "
                      "with history ({} of them match Fusion's volume)".format(
                          len(geo), sum(1 for r in geo if r["geometry_sc"].get("matches"))))

        alone = [r for r in reports if r.get("retried_alone")]
        if alone:
            still = [r for r in alone if r["retried_alone"] is not True]
            print("\n{} design(s) ran out of time with {} jobs and were built again alone{}".format(
                len(alone), args.jobs,
                "; {} timed out again: raise --verify-timeout ({} s now)".format(
                    len(still), VERIFY_TIMEOUT_S) if still else ""))

        missing = {}
        for r in reports:
            for s in r["steps"]:
                if s["status"] != "translated":
                    missing[s["entity"]] = missing.get(s["entity"], 0) + 1
        if missing:
            print("\nmost common untranslated steps:")
            for k, v in sorted(missing.items(), key=lambda kv: -kv[1])[:12]:
                print("  {:<34} {}".format(k, v))
        print("\nreport: {}".format(os.path.join(args.outdir, "rebuild-report.json")))
        return 0

    out_path = args.out or os.path.splitext(os.path.splitext(args.input)[0])[0] + ".json"
    r = one(args.input, out_path, args.cli, args.verify, args.constraints)
    print("{}: {}/{} steps translated ({:.0%}), {} commands".format(
        r["design"], r["steps_translated"], r["steps_total"], r["coverage"], r["commands"]))
    for s in r["steps"]:
        if s["status"] != "translated":
            print("  {} #{} {} - {}".format(s["status"], s["index"], s["entity"],
                                            s.get("reason", "")[:90]))
    if args.verify:
        print("verify:", json.dumps(r.get("verify")))
    print("script: {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
