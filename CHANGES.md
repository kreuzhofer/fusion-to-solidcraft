# Changes against upstream

Based on [acbrazzl/fusion-to-solidcraft](https://github.com/acbrazzl/fusion-to-solidcraft), commit `a8e94dedf3`
(2026-11-11), Apache-2.0, Copyright 2026 Total Overwatch Productions LLC (see `LICENSE` and `NOTICE`). Upstream's
`.github/` (CI) is left out. Everything below was changed or added in this repository, November 2026, while moving a
real hub of 215 designs. Code comments marked `Crosslink <date>` belong to these changes.

## New tools

- **`fusion/InventoryHub`**: lists every design of the hub and sorts it into a bucket (`parametric`, `mesh-base`,
  `geometry-only`, `not-a-design`, `unreadable`) with the reason, flags (assembly, big, no real Fusion work) and
  what happens to it next. Writes `inventory.json`, `inventory.csv`, `inventory-summary.txt` and
  `lists/<bucket>.txt`. A design read at the same version is not opened again.
- **`fusion/common/f2s_buckets.py`**: the bucket rule in one place, plain Python, shared by InventoryHub and
  ExtractTimeline so the inventory's promise and the extractor's decision cannot drift apart.
  `fusion/common/test_buckets.py` checks it, also against timelines already extracted.
- **`fusion/CrosslinkNightRun`** (add-in) and **`fusion/nightloop.py`**: unattended extraction. The add-in runs
  ExtractTimeline when a request file appears; the loop outside Fusion asks for rounds, watches Fusion's memory and
  restarts Fusion between rounds until nothing is left to extract, then translates, plans the next pass and
  fetches the states the translator asked for, until nothing new is needed (`--extract-only` to stop after the
  extraction). It counts the work left from the inventory and the wanted list (`--select list` for a pass of
  step 5, `--mem-cap` for smaller machines).
- **`next_pass.py`**: after a translator run, writes the list of frozen states the next extraction should fetch.
- **`compare_runs.py`**: compares two translator runs design by design.
- **`install.py`**: copies the Fusion scripts (and optionally the add-in) into Fusion's API folder on macOS and
  Windows.

## ExportAllDesigns

- Added the missing `get()` helper: upstream calls it in `measure()` without defining it, so the manifest recorded
  0 bodies and 0 volume for every design.
- Documents that were open before the run are never closed; everything the script opened is closed without saving
  (Fusion flags some old designs as modified on open, and leaving those open filled the session).
- A repeatable backup: later runs export only new designs and new versions (decided from the hub listing, without
  opening), keep older exports in `_previous/` (`KEEP_PREVIOUS`), move the files of moved or renamed designs before
  exporting anything, keep the files of deleted designs (`gone_from_hub`), write under a temporary name, keep the
  last good export when one fails (`last_error`), keep stable `(2)` names for duplicates, and append to
  `backup-log.txt`.

## ExtractTimeline (the JSON `SCHEMA`, 16 upstream, 26 now)

Reading more of each design:
- Splines carry `nurbs` (control points, degree, knots, weights) and `samples`; sketch texts carry words, font,
  height and outline curves.
- Every feature gets `x`: its readable properties as data (parameters with expressions, points, vectors, matrices,
  the bodies, faces, edges, planes and axes it refers to), the bodies and faces it made, and for features that
  consume their inputs (Combine, Split, Remove, Shell, Draft, Hole) the inputs read with the model rolled back to
  just before the step. A feature that is the input of another (what a pattern repeats) is recorded by name.
- Every step gets `bodies_after`: each body with name, extent, volume, centre of mass and a pick point;
  `parent_component`; times per step.
- Timeline groups are expanded before reading (a whole history inside one collapsed group used to come out as a
  single step).
- Extrude profiles that cannot be read in the finished model (Press Pull) are read with the model rolled back.

Geometry from Fusion where a step cannot be rebuilt:
- After a base or derive feature, and for every step the rebuild asks for (`STATE_WANTED`), the bodies the step
  made or changed are exported as `<design>.base<step>.step` (a frozen state). Bodies Fusion leaves out of the export
  are copied and written through a scratch document. Light bulbs are switched for the export and put back; nothing
  is saved.
- Mesh bodies are written as STL when they appear; `<design>.basefinal.step` holds the finished design with every
  body, hidden ones included (a plain STEP export only writes visible bodies).
- Mesh bases (schema 26): a design that starts from a converted mesh and is modelled after it is read from the step
  after its last mesh step; that state is its starting body.

Big hubs and big assemblies:
- Collections over `ITEM_CAP` are not read item by item (Fusion rebuilds a face list on every `.item(i)`, which made
  one design take 20 minutes at 100 % CPU).
- Designs with mesh features are not rolled back; per-step measuring has a time budget per design.
- Body lists up to 6,000 bodies; bodies of linked designs are read once per placement and stored once per design
  (`linked_sets`), and a linked design is hidden as a whole for state exports.
- States are fetched in portions (`STATE_CHUNK`) and the run stops cleanly when Fusion's memory passes `MEM_CAP_GB`,
  so the night loop can restart Fusion.
- `status.txt` names the design being read.

Which designs are read:
- Designs come from InventoryHub's `inventory.json` and are opened by ID, without walking the hub (falls back to the
  walk without an inventory).
- A design already read is skipped. It is read again only when Fusion saved a new version of it, when a newer schema
  changes something for it (`still_current`), or when the rebuild asks for a state its file does not hold yet
  (`states_missing`). A design read again keeps the frozen states it had.

## timeline_to_solidcraft.py (the translator)

- Builds each step in an open SolidCraft session (`solidcraft-cli mcp --headless`) and compares the rebuilt bodies
  with Fusion's after every step (lockstep); a step the engine refuses is taken out so the rest still builds.
- Profile picks: the right sketch point for sketches off the XY plane, and profiles matched to SolidCraft's own
  regions by area and centroid.
- Many more features: Box, Cylinder, Sphere, Move, Combine, Hole (simple, counterbore, countersink, clearance,
  tapped), Remove, Scale, Mirror, Split (plane, face, curved tool), rectangular and circular patterns, copied
  bodies, sketch texts, splines, ellipses, Offset Faces, Shell, revolve about any axis, components.
- Frozen states: a step that cannot be rebuilt takes Fusion's result from its state file, and the steps around it
  stay editable. Mesh bases start from their converted mesh.
- Grades per design: `exact`, `exact-with-frozen-steps`, `partial-but-matching`, `imported-as-is`,
  `volume-mismatch`, checked against Fusion's own measurements body by body.
- Every design that does not come back right with history also gets `<design>.geometry.sc`: its exact final shape.
- Bodies end up in the components Fusion had them in, with Fusion's body names.
- Writes `state-wanted.json` (what the next extraction should fetch) after every run.
- `--jobs`, `--verify-timeout` and an automatic second try of designs that ran out of time.

## Known SolidCraft import limits seen along the way

All from Fusion STEP exports, SolidCraft CLI 0.41.0; worth reporting upstream with minimal cases.
- Some surface bodies are not imported ("SMLib export: not supported yet: a sheet touching itself along an edge";
  "the solid's only face … outer loop runs clockwise in (u, v)").
- Some solids come in as surfaces with volume 0 ("pcurves failed … off the spline's knot range on a rational net").
- One solid came in 12 % too large with no warning.
- Very large files are refused ("STEP file has more than 20000000 parameters").
- `solid.scale` with unequal factors about a point on an imported body can leave a body of volume 0.
