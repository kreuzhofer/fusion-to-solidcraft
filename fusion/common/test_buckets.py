#!/usr/bin/env python3
"""Check the bucket rule against timelines already extracted (no Fusion needed).

    python3 fusion/common/test_buckets.py [~/cad/fusion-timeline]

1. Small built-in cases (always).
2. With a timeline folder: rebuilds each design's facts from its .timeline.json and checks that
   the rule agrees with what ExtractTimeline decided for it (a mesh base where it wrote one, none
   where it did not), then prints the counts per bucket as InventoryHub would.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f2s_buckets as B  # noqa: E402


def cases():
    c = B.classify
    assert c({"ext": "stl"})["bucket"] == "not-a-design"
    assert c({"ext": "f3d", "error": "boom"})["bucket"] == "unreadable"
    assert c({"ext": "f3d", "parametric": False})["bucket"] == "geometry-only"
    assert c({"ext": "f3d", "kinds": ["Sketch", "ExtrudeFeature"], "meshes_left": 0})["bucket"] == "parametric"
    r = c({"ext": "f3d", "kinds": ["", "MeshConvertFeature", "OffsetFacesFeature"], "meshes_left": 0})
    assert r["bucket"] == "mesh-base" and r["mesh_base"]["index"] == 1, r
    r = c({"ext": "f3d", "kinds": ["", "MeshConvertFeature", "ConstructionPlane"], "meshes_left": 0})
    assert r["bucket"] == "geometry-only" and r["real_work"] is False, r
    r = c({"ext": "f3d", "kinds": ["ExtrudeFeature", "MeshConvertFeature", "OffsetFacesFeature"], "meshes_left": 1})
    assert r["bucket"] == "geometry-only" and r["real_work"] is True, r
    r = c({"ext": "f3d", "kinds": ["Occurrence", "Occurrence"], "occurrences": 2})
    assert r["bucket"] == "geometry-only" and "no real Fusion work: only imported or converted" in r["flags"], r
    assert c({"ext": "f3d", "kinds": ["BaseFeature"]})["bucket"] == "geometry-only"
    r = c({"ext": "f3d", "kinds": ["", "MeshReduceFeature", "Feature"], "names": ["", "R", "CopyPasteMeshes1"],
           "meshes_left": 2})
    assert r["real_work"] is False, r
    print("built-in cases pass")


def facts_from_timeline(doc):
    kinds = [s.get("entity") or "" for s in doc.get("timeline") or []]
    return {"ext": "f3d", "parametric": bool(kinds) or None, "kinds": kinds,
            "names": [s.get("name") or "" for s in doc.get("timeline") or []],
            "meshes_left": len(doc.get("final_meshes") or []),
            "bodies": len(doc.get("final_bodies") or []),
            "occurrences": sum(1 for k in kinds if k == "Occurrence")}


def against(folder):
    files = sorted(glob.glob(os.path.join(folder, "**", "*.timeline.json"), recursive=True))
    records, wrong = [], []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            doc = json.load(fh)
        r = B.classify(facts_from_timeline(doc))
        r["design"] = os.path.relpath(f, folder)[: -len(".timeline.json")]
        records.append(r)
        if doc.get("schema", 0) >= 26:   # the extractor applied the mesh-base rule itself
            said = (doc.get("mesh_base") or {}).get("index")
            got = (r.get("mesh_base") or {}).get("index")
            if said != got:
                wrong.append((r["design"], said, got, r["reason"]))
    print("\n".join(B.summary_lines(records)))
    print("\n{} designs; extractor and rule disagree on the mesh base in {}".format(len(records), len(wrong)))
    for w in wrong:
        print("  ", w)
    return not wrong


if __name__ == "__main__":
    cases()
    folder = os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else None
    if folder:
        sys.exit(0 if against(folder) else 1)
