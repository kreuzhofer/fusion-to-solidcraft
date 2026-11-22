# fusion-to-solidcraft

Move your designs from Autodesk Fusion 360 to [SolidCraft](https://github.com/tangency-labs/solidcraft),
an open-source parametric CAD application, **with their modelling history** wherever that can be
done, and as exact geometry where it cannot. Every result is checked against Fusion's own
measurements, step by step.

Based on [acbrazzl/fusion-to-solidcraft](https://github.com/acbrazzl/fusion-to-solidcraft)
(Apache-2.0, Total Overwatch Productions LLC; method described in
[Rebuilding a Fusion 360 parametric timeline in SolidCraft with an LLM agent](https://totaloverwatchproductions.com/page/fusion-to-solidcraft-timeline/)).
This version adds an inventory, frozen states, mesh bases, unattended extraction and a step-by-step
check; every change against upstream is listed in [`CHANGES.md`](CHANGES.md).

**Status: works on a real hub of 215 designs, still changes often.** The JSON shape (`SCHEMA` in
`ExtractTimeline.py`) can change between versions; a newer schema reads again only the designs its
change concerns (for example only the mesh-base designs for schema 26).

## The steps at a glance

| Step | Where | Tool | What you get | Time (215 designs, for scale) |
|---|---|---|---|---|
| 0. Install | once | Python, SolidCraft, `install.py` | the scripts in Fusion | 15 min |
| 1. Inventory | Fusion | `InventoryHub` | every design listed, sorted into buckets, with the next step | 25 min |
| 2. Backup | Fusion | `ExportAllDesigns` | one STEP and one F3D per design; run it again any time | 30 min, later runs minutes |
| 3. Extract | Fusion, unattended | `ExtractTimeline` (+ `nightloop.py`) | each design's history, step by step, as JSON | 3 to 5 h |
| 4. Convert | terminal | `timeline_to_solidcraft.py` | a SolidCraft design (`.sc`) per design and a grade for each | 30 min to 2 h |
| 5. Improve | both | `next_pass.py`, then steps 3 and 4 again | more designs matching Fusion | 30 min to 7 h per pass |

Steps 1 to 3 run inside Fusion and only read your designs: nothing is ever saved in Fusion, and
the timeline marker is put back after every read. Step 2 first, because it is quick and is your
safety net whatever happens later. Step 5 is repeated until `next_pass.py` says nothing is left to
fetch.

## 0. Before you start: install

You need:

| What | Why | How |
|---|---|---|
| **Fusion 360**, licensed and signed in to the account that owns the designs | steps 1 to 3 run inside it | as you use it today; make the hub with your designs the active one |
| **Python 3.9 or newer** | steps 4 and 5, the loop | macOS: [python.org](https://www.python.org/downloads/) installer, or `brew install python`. Windows: python.org installer, tick "Add python.exe to PATH". Check: `python3 --version` (Windows: `py --version`) |
| **SolidCraft** with its command-line tool `solidcraft-cli` | step 4 builds and measures every design with it | install the app from its [releases](https://github.com/tangency-labs/solidcraft/releases). macOS: the tool is inside the app; the translator finds it there and in the usual Windows install folders; if not, put it on the PATH or set `SOLIDCRAFT_CLI` to its path. Check: `solidcraft-cli --version` (tested with 0.41.0) |
| **Disk space** | exports and states | about 60 MB per design (215 designs: 12 GB) |
| **Memory** | Fusion grows while it extracts | the extractor stops cleanly at 40 GB of Fusion memory and the loop restarts Fusion; on a machine with less RAM start the loop with `--mem-cap` (or lower `MEM_CAP_GB`) |

No Python packages are needed beyond the standard library (`f3d_inventory.py` alone wants
`pip install zstandard`).

**Install the Fusion scripts.** In a terminal, from this folder (start Fusion once before, so its
API folder exists):

    python3 install.py            # InventoryHub, ExportAllDesigns, ExtractTimeline
    python3 install.py --addin    # also the CrosslinkNightRun add-in for unattended extraction (step 3)
    python3 install.py --remove   # take everything out again

On Windows type `py` instead of `python3` (in all commands of this README). The scripts go to
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API` on macOS and to
`%APPDATA%\Autodesk\Autodesk Fusion 360\API` on Windows.

Then in Fusion: **Utilities → Add-Ins → Scripts and Add-Ins**. The three scripts appear under
*My Scripts*. Run `install.py` again after every update of this toolkit.

Settings are constants at the top of each Fusion script (open it in a text editor). Output folders
default to `~/cad/...`; keep them unless you have a reason, the later steps look there.

## 1. Inventory: what is in your hub, and what can be done with it

Run **InventoryHub** in Fusion. It lists every file of the active hub, opens each design once
(invisible, never saved) and sorts it into the bucket the later steps use, with the reason:

| Bucket | What it is | What happens to it |
|---|---|---|
| `parametric` | a timeline with modelling steps and no mesh steps | extracted and rebuilt with history |
| `mesh-base` | an STL converted to a body and then really modelled | history from the step after the last mesh step on; that body is the start |
| `geometry-only` | no history worth keeping: direct modelling, only imported bodies, a mesh with nothing modelled after it, or meshes left in the finished model | kept as its exact final shape (STEP and meshes) |
| `not-a-design` | other files in the hub (STL, PDF, images) | not migrated; download them from the Data Panel if you need them |
| `unreadable` | Fusion could not open it | open it by hand once, then run the inventory again |

Flags on top: `assembly` (uses other designs or has components), `big` (over 200 bodies counted once per
placement, as the extractor meets them: slower, more memory), `no real Fusion work` (only imported or
converted). The rule lives in one file, `fusion/common/f2s_buckets.py`, which ExtractTimeline uses too,
so what the inventory promises is what the extraction does. `python3 fusion/common/test_buckets.py
~/cad/fusion-timeline` checks the rule against timelines already extracted.

| Setting | Default | Meaning |
|---|---|---|
| `OUT_DIR` | `~/cad/fusion-inventory` | where the inventory goes |
| `DEEP` | `True` | `False`: list only, without opening anything (minutes instead of tens of minutes, but no buckets) |
| `ALL_HUBS` | `False` | walk every hub you belong to, not only the active one |
| `LIMIT` | `0` | stop after this many newly opened designs (`0` = all); try 5 first |

It writes to `OUT_DIR`:

| File | What is in it |
|---|---|
| `inventory-summary.txt` | how many designs in each bucket, what happens to them, how many have real work, the next step (also shown in Fusion at the end) |
| `inventory.json` | every file with ID, version, path, bucket, reason, flags and the facts behind them; step 3 reads it |
| `inventory.csv` | the same as a table, for a spreadsheet |
| `lists/<bucket>.txt`, `lists/real-work.txt` | the designs of each bucket, one per line, ready for step 3's `SELECT = "list"` |

Resumable: a design already read at the same version is not opened again. Run it again whenever
designs were added or changed in Fusion; step 3 tells you when that is the case.

**Next:** step 2.

## 2. Backup: every design as STEP and F3D

Run **ExportAllDesigns** in Fusion. It stands on its own: it walks the hub itself and needs nothing
from the other steps, so you can keep using it as a recurring backup of your designs, also long
after the migration.

| Setting | Default | Meaning |
|---|---|---|
| `OUT_DIR` | `~/cad/fusion-export` | where the files go, as `<Hub>/<Project>/<folders>/<design>.step/.f3d` |
| `EXPORT_STEP`, `EXPORT_F3D` | `True` | which files to write |
| `ALL_HUBS` | `False` | every hub, not only the active one |
| `KEEP_PREVIOUS` | `True` | keep the export of an older version as `_previous/<path>/<design>.v<N>.step/.f3d` |

The first run exports everything. Every later run exports only what is new or has a new version
in Fusion; it can tell without opening a design, so an unchanged hub is checked in minutes.

| In the hub | What the backup does |
|---|---|
| new design | exported |
| new version saved | exported again; the older export moves to `_previous/` |
| moved or renamed | its files are moved along, nothing is exported again |
| deleted | files kept, marked `gone_from_hub` (with the date) in `manifest.json` |
| export fails | the last good export stays, the error is recorded as `last_error` |

Nothing is ever deleted, and files are written under a temporary name first, so a stopped run never
leaves a half-written file behind; start it again and it continues. Two designs with the same name
in one folder get `<design> (2)`, and keep that name in later runs.

Outputs besides the files:

| File | What it holds |
|---|---|
| `manifest.json` | one record per design: version, when it was exported and checked, Fusion's own volume, area and body count (step 4 checks against these), kept older exports |
| `backup-log.txt` | one line per run (new, changed, moved, unchanged, failed, gone) and the names behind the numbers |

Leave documents of your own closed while it runs; it closes what it opened and leaves alone what
was open before.

Two limits: a STEP export holds the **visible** bodies only (step 3 writes a complete one per
design, hidden bodies included), and the F3D only opens in Fusion. Keep a second copy of the folder
somewhere else.

**Next:** step 3.

## 3. Extract: each design's history

**ExtractTimeline** reads every design's timeline: parameters, sketches with constraints, each
feature with its inputs, and with the timeline marker moved behind each step, what the model
measures there. It also writes the finished design as one complete STEP (`<design>.basefinal.step`)
and its mesh bodies as STL.

| Setting | Default | Meaning |
|---|---|---|
| `OUT_DIR` | `~/cad/fusion-timeline` | one `<design>.timeline.json` per design, plus state files |
| `INVENTORY`, `USE_INVENTORY` | `~/cad/fusion-inventory/inventory.json`, `True` | take the designs from step 1's inventory and open them by ID, instead of walking the hub (which takes minutes on every run); without an inventory the hub is walked |
| `SELECT` | `"all"` | `"all"`, `"list"` (the paths in `SELECT_LIST`: step 5's list, or one of step 1's `lists/<bucket>.txt`), a group name from `GROUPS`, or a piece of a design or folder name |
| `SELECT_LIST` | `~/cad/work/_reextract.txt` | the list for `SELECT = "list"` |
| `STATE_WANTED` | `~/cad/work/_state_wanted.json` | the steps whose state to write (step 5 fills it) |
| `LIMIT` | `0` | stop after this many designs; use 3 for a first try |
| `MEASURE_EACH_STEP`, `ROLLBACK_FOR_EDGES` | `True` | the per-step measurements and inputs the check in step 4 needs |
| `MEASURE_BUDGET_S`, `INPUT_BUDGET_S` | 2400, 1800 | per design, stop measuring / reading inputs after this long |
| `STATE_CHUNK` | 40 | at most this many new state files per visit of a design |
| `MEM_CAP_GB`, `MEM_START_GB` | 40, 32 | stop cleanly when Fusion uses this much memory; start no new design above the second |

Resumable: a design already read is skipped, so a run that was stopped simply continues when started
again. A design is read again when it was saved in Fusion since it was read, when a newer `SCHEMA`
changes something for it (`still_current` lists what each schema changed), or when the rebuild asks
for a frozen state its file does not hold yet (step 5). A design read again keeps the frozen states it
had. A design whose version or bucket changed since the inventory is listed in the final message
(and `summary.json`): run InventoryHub again then.

**Unattended (recommended):** install the add-in (`python3 install.py --addin`), run InventoryHub
(step 1: the loop counts the work left from the inventory), quit Fusion, and in a terminal:

    python3 -I fusion/nightloop.py

On Windows the loop starts Fusion with its launcher from `%LOCALAPPDATA%\Autodesk\webdeploy\production`
(set `FUSION_LAUNCHER` to the launcher's path if yours is elsewhere) and watches the process
`Fusion360.exe`. Keep the machine from sleeping while it runs (macOS: `caffeinate -i` in front of
the command).

The loop starts Fusion, has the add-in start ExtractTimeline, waits, restarts Fusion when the run
stopped for memory, and ends when nothing is left (`finished: nothing left to extract`), when two
rounds made no progress, or after 30 rounds. Stop it with Ctrl-C; Fusion is left as it is. Its
log is `~/cad/work/_nightloop.log`; `status.txt` in `OUT_DIR` names the design being read.

**By hand:** run ExtractTimeline from Scripts and Add-Ins. If Fusion gets slow or stops at the memory
cap, quit Fusion, start it again and run the script again.

**Next:** step 4.

## 4. Convert: build every design in SolidCraft and check it

In a terminal, on an otherwise idle machine:

    python3 timeline_to_solidcraft.py ~/cad/fusion-timeline --all --outdir ~/cad/fusion-rebuild \
        --verify --against ~/cad/fusion-export --jobs 4

| Option | Meaning |
|---|---|
| input | a `.timeline.json`, or a folder with `--all` |
| `--all` | every timeline below the folder |
| `--outdir` | where the results go (same folder structure as the input) |
| `--verify` | build each design, check it step by step against Fusion, save the `.sc` |
| `--against` | the export folder of step 2 (its `manifest.json` and STEP files); the path of each design below the input folder must match its path below this one |
| `--jobs` | designs built at the same time (each uses a SolidCraft process; 4 to 6 on a recent machine) |
| `--cli` | path of `solidcraft-cli`, if it is not found by itself |
| `--verify-timeout` | longest one build check may take, in seconds (default 300); raise it for big assemblies or a slow machine |
| `--no-retry` | do not build designs that timed out again one at a time (see below) |

Per design it writes the SolidCraft design (`<design>.sc`), the command script (`<design>.json`) and
`<design>.coverage.json`, which names every step as translated, taken from Fusion ("frozen") or
neither, and why. Over all designs: `rebuild-report.json`, and `state-wanted.json` for step 5. The
last lines of the output count the grades:

| Grade | Meaning |
|---|---|
| `exact` | every step translated, the result equals Fusion's |
| `exact-with-frozen-steps` | every step translated or taken from Fusion, the result equals Fusion's; the steps in between stay editable |
| `partial-but-matching` | the result equals Fusion's, some steps are neither translated nor frozen |
| `imported-as-is` | no history in Fusion: the exact geometry, nothing to edit |
| `volume-mismatch` | the rebuild differs; `first_divergence` in the coverage file names the step |

**Every design gets a SolidCraft file that is right.** When a rebuild does not match Fusion (or gets
no grade), its `<design>.sc` is kept (it may be close, and its history is worth a look), and next
to it the translator saves `<design>.geometry.sc`: Fusion's complete final state imported as it is,
without history but with the exact shape. `geometry_sc` in the coverage file says how close
SolidCraft measures it (`matches`). When a later run gets the design right, the geometry file goes.

Each build check has 300 s (`--verify-timeout`). Under heavy load large designs can run out of it.
At the end of the run the translator builds those again, one at a time and with twice the time;
they are marked `(built alone)` in the output and `retried_alone` in the report. A design that still
runs out says `TIMED OUT`: run it again with a higher `--verify-timeout`.

**Next:** step 5.

## 5. Improve: fetch Fusion's own result where the rebuild cannot follow

Where a step cannot be translated, does not build, or builds something other than Fusion had, the
state of the model right after that step in Fusion can stand in for it ("frozen state"). The
translator lists those steps in `state-wanted.json`. Then:

    python3 next_pass.py

It keeps the designs with at least one state that is not on disk yet, writes `~/cad/work/_state_wanted.json`
and `~/cad/work/_reextract.txt` (the previous ones are kept with a time stamp), and says how many
new states the pass will write. If it says **"nothing new to fetch from Fusion"**, you are done.
Otherwise run step 3 again for the listed designs only: unattended with

    python3 -I fusion/nightloop.py --select list

or by hand with `SELECT = "list"` in ExtractTimeline (states already on disk are kept), then step 4
again, then `next_pass.py` again.

Options: `--dry-run` (only report), `--wanted`, `--timelines`, `--work` (other folders).

**Mesh bases** (schema 26): a design that starts from an STL, converts it and is then modelled
further is read from the step after its last mesh step on; the model at that point becomes the
design's starting body, and the modelling after it is translated. A design that is only a mesh
import has no history worth keeping and stays geometry only.

## When something goes wrong

| What you see | What to do |
|---|---|
| `solidcraft-cli` not found | put it on `PATH` (see step 0) or pass `--cli` |
| Fusion stops responding while a script runs | normal while it reads a big design; `status.txt` in the output folder shows where it is. The loop's log shows Fusion's memory every 10 minutes |
| A design is skipped although you changed it | it is read again only when Fusion saved a new version of it; delete its `.timeline.json` to read it again anyway |
| Many designs without a grade after step 4 | the machine was busy; run those designs again alone |
| `two rounds without progress` in the loop log | a design cannot be read further; check `status.txt` and the design's `.timeline.json` (`states_pending`, errors) |
| A STEP file is refused ("more than 20000000 parameters") | a mesh converted to a solid with hundreds of thousands of faces; SolidCraft's import limit. Keep the original STL |

## Other scripts

| Script | What it does |
|---|---|
| `compare_runs.py` | compares two translator runs (`rebuild-report.json`) design by design: grades, designs matching Fusion, what got better or worse; numbers instead of names unless `--names` |
| `migrate.py` | imports each STEP of step 2 into SolidCraft and compares volume and area with Fusion's (`migration-report.json`); a quick check of the geometry route alone |
| `build_library.py` | one `.solidcraft` per design by the best route available, and `library.json` saying which |
| `f3d_inventory.py` | an HTML contact sheet of the F3D previews |
| `golden/run_golden.py` | the translator's regression suite (needs `solidcraft-cli`, not Fusion) |

Each prints `--help`.

## Verification

A rebuild is checked against Fusion's numbers, not against "it built":

- **Whole design:** the rebuilt volume is compared with the volume Fusion reported at export.
  The tolerance (1e-3 relative) was set from a control part: curved-face volumes are measured from
  a tessellation, so a plain cylinder reads about 1e-4 under its analytic value.
- **Per step:** with `MEASURE_EACH_STEP` on, `ExtractTimeline` rolls the timeline marker to each step
  and records volume, area, body count and bounding box there. A rebuild is replayed one step at a
  time and compared at each one, so a difference is attributed to the step that introduced it.
  Compare what each step adds, not only the running total, and compare the bounding box as well
  as the volume: a feature built at the wrong height keeps the volume and moves the box.
- **Consumed inputs:** a fillet, chamfer or draft destroys the edges or faces it was given, so
  those are read with the timeline rolled back to just before the feature. They are recorded as
  points on the geometry (and, for faces, area and centroid), which is how SolidCraft selects.
- **Nothing is dropped silently:** an untranslated step is listed in the coverage report with a
  reason; an unreadable selection is recorded as such in the timeline.

`golden/run_golden.py` is the regression suite for the translator. It writes timelines for
invented shapes whose volumes are known in closed form (a rounded plate, a chamfered block, a
shelled box, a parametric plate rebuilt at a second thickness, a constrained sketch) plus negative
controls (an unmapped feature type and a suppressed feature, which must be reported, not
translated), translates and builds them, and compares. It needs `solidcraft-cli`; it does not
need Fusion.

## What translates

`.coverage.json` names, for every step of a design, whether it was translated, taken from Fusion
(frozen state), or neither, and why. As of November 2026:

- **Translated:** sketches (lines, arcs, circles, ellipses, splines, texts; constraints and
  dimensions with `--constraints`), extrude (profiles and faces), revolve, fillet, chamfer, shell,
  draft, offset faces, box, cylinder, sphere, hole (simple, counterbore, countersink, clearance,
  tapped), combine, split body, remove, move, scale, mirror, rectangular and circular patterns,
  copied bodies, construction geometry, new components.
- **Taken from Fusion as a frozen state** when it cannot be rebuilt: loft, sweep, thread, sheet
  metal, mesh operations and anything the engine refuses; the steps before and after stay
  editable.
- **Imported as is:** designs without history (direct modelling, imported bodies); their exact
  final shape comes in as STEP and meshes.

Many build failures are in the SolidCraft kernel rather than in translation, and they change as
SolidCraft changes.

## Platforms

Developed and run on macOS (Apple silicon) with Fusion and SolidCraft 0.41.0. Windows support
(installer, memory guard, night loop, CLI lookup) is written and being tested; see
[`docs/windows-test.md`](docs/windows-test.md).

## Design data stays out of this repository

`.gitignore` excludes geometry (`*.f3d`, `*.step`, `*.stl`, …), every JSON/HTML/CSV output of the
pipeline, renders, and the default output folders. Keep it that way in forks: an extracted timeline
contains the full design.

## License

Apache License 2.0 — see `LICENSE` and `NOTICE`. Copyright 2026 Total Overwatch Productions LLC;
changes in this repository (listed in `CHANGES.md`) copyright 2026 the fusion-to-solidcraft
contributors, under the same license.

Not affiliated with or endorsed by Autodesk, Inc. Fusion 360 is a trademark of Autodesk, Inc.
