# Windows test

The toolkit was developed on macOS. This is the plan for checking it end to end on Windows: Fusion
for Windows, SolidCraft for Windows, and the unattended night run. It is written so that a person
or a coding agent (for example Claude Code on the Windows machine) can follow it without other
context.

## Ground rules

- **Read-only in Fusion.** The scripts never save a design. Do not save, move or rename anything in
  the hub during the test. Close your own documents before each script run.
- **Nothing outward without the owner's OK:** no uploads, no posts, no pushes to `main`. Results go
  on a branch (`windows-test`) and into a pull request the owner reviews.
- **Design data stays private.** Do not commit outputs (`*.json`, `*.step`, `*.f3d`, `*.sc`, …;
  `.gitignore` already excludes them) and do not paste design names into the results file, issues or
  the pull request: write "design A", "design B" and keep the mapping to yourself (or in a file
  outside the repository).
- **Fix what is clearly a Windows problem** (paths, encodings, process handling) in small commits on
  the branch, each with what failed and why the fix is right. Anything that would change results on
  macOS too: describe it, don't fix it.

## 0. Set up

| What | Check |
|---|---|
| Fusion for Windows, signed in, the hub with the designs active | starts, the Data Panel shows the projects |
| Python 3.9+ from python.org ("Add python.exe to PATH" ticked) | `py --version` |
| SolidCraft for Windows | `solidcraft-cli --version`; note **where the installer put `solidcraft-cli.exe`** and whether `timeline_to_solidcraft.py` finds it without `SOLIDCRAFT_CLI` (`py -c "import timeline_to_solidcraft as t; print(t.find_cli())"`) |
| this repository | `git clone`, then `git switch -c windows-test` |

Then `py install.py --addin`. Check that it names `%APPDATA%\Autodesk\Autodesk Fusion 360\API` and
that the three scripts appear in Fusion under **Utilities → Add-Ins → Scripts and Add-Ins**, and the
add-in under *Add-Ins*.

## 1. Without Fusion

    py fusion\common\test_buckets.py
    py golden\run_golden.py

Both must pass. `run_golden.py` builds invented shapes in SolidCraft and compares their volumes with
closed-form values: a failure here is a translator or SolidCraft difference between the platforms,
the most important thing to report.

## 2. Inventory (step 1 of the README)

Run **InventoryHub** in Fusion. Report: run time, the summary (`%USERPROFILE%\cad\fusion-inventory\inventory-summary.txt`:
counts per bucket, designs with real work, assemblies, big), and any design that could not be opened.
If the same hub was inventoried on macOS, the counts must be identical.

**Path length for all designs.** The sample in section 4 extracts only six designs, so check the paths
the extractor would write for every design now (`.base123.extra.step` is about the longest name it
adds to a design's path):

    py -c "import json,os; h=os.path.expanduser('~/cad'); d=json.load(open(h+'/fusion-inventory/inventory.json',encoding='utf-8'))['designs']; L=sorted((len(os.path.join(h,'fusion-timeline',*r['path'].split('/')))+len('.base123.extra.step'),r['path']) for r in d if r.get('ext')=='f3d'); print('longest:',L[-1][0],'characters; over 259:',sum(1 for n,_ in L if n>259))"

Report the two numbers (not the path). Anything over 259 characters fails on Windows unless long paths
are switched on (`LongPathsEnabled`); if there are any, say how many and whether long paths are enabled
on this machine (`reg query HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled`).

## 3. Backup (step 2)

1. Run **ExportAllDesigns**. Report: run time, counts from the final message, `backup-log.txt`.
2. **Run it again at once.** Expected: `new 0 changed 0 moved 0`, every design `unchanged`, done in
   minutes, no design opened.
3. Report the **longest path** written under `%USERPROFILE%\cad\fusion-export` (characters) and whether
   any export failed because of the path length (Windows' 260-character limit is the main suspect).

## 4. Extract a sample (step 3, by hand)

Pick six designs from the inventory's lists (`%USERPROFILE%\cad\fusion-inventory\lists\`):
two from `parametric.txt`, one from `mesh-base.txt`, one from `geometry-only.txt`, one flagged
`assembly`, and the one with the most timeline steps you can find (`inventory.csv`, column
`timeline_steps`). Write their paths, one per line, to `%USERPROFILE%\cad\work\_reextract.txt`, set
`SELECT = "list"` at the top of `ExtractTimeline.py` in Fusion's Scripts folder, and run
**ExtractTimeline**.

Report: run time, the final message, `summary.json` (`source` must say it was read from the inventory),
errors per design, the longest path under `%USERPROFILE%\cad\fusion-timeline`. Run it a second time:
all six must be skipped.

## 5. Convert (step 4)

    py timeline_to_solidcraft.py %USERPROFILE%\cad\fusion-timeline --all --verify --jobs 2 --outdir %USERPROFILE%\cad\fusion-rebuild

Report the grade of each design (`rebuild-report.json`, `fidelity.grade`), the run time, and any
`timeout`, encoding error (`UnicodeDecodeError`, `charmap`) or path error. If the same designs were
converted on macOS, the grades should match; list every difference.

## 6. Improve, unattended (steps 5 and 3 with the night loop)

1. `py next_pass.py` and note what it says (designs and states wanted).
2. Quit Fusion. Look up Fusion's memory when idle with one small design open (Task Manager, column
   *Commit size*, or Details → `Fusion360.exe`). Choose a cap about 3 GB above that, so a round is
   forced to stop and Fusion has to be restarted at least once.
3. Start the loop in a terminal and leave the machine alone:

       py -I fusion\nightloop.py --select list --mem-cap <that cap> --jobs 2

   (`--select list` reads only the designs `next_pass.py` listed in `_reextract.txt`. If `next_pass.py`
   said there is nothing to fetch, run the loop without `--select list` instead after moving one
   design's `.timeline.json` out of `%USERPROFILE%\cad\fusion-timeline` (keep it; put it back
   afterwards): the loop then extracts that design. Either way at least one restart must happen.)

   Expected in `%USERPROFILE%\cad\work\_nightloop.log`: "starting Fusion", rounds that end with
   `mem_stop` set, "quitting Fusion", Fusion started again by the loop, `finished: nothing left to
   extract`, then the loop translates and runs `next_pass.py` by itself ("translating", "next_pass",
   their output in `_nightloop-translate-1.log` and `_nightloop-next-pass-1.log`) and either fetches
   another pass or ends with `done: the translator needs nothing more from Fusion`. Also check that the loop refuses to start without an inventory
   (rename `inventory.json` for a moment and run it: it must stop with "run InventoryHub first" and not
   start Fusion).
4. Compare the grades in `rebuild-report.json` (the loop's last translation) with the first
   conversion.

Report: whether the loop found Fusion's launcher by itself (if not, the path that works, set as
`FUSION_LAUNCHER`), whether it found the process (`Fusion360.exe` or another name: `FUSION_PROCESS`),
whether quitting asked any question on screen, how many rounds and restarts, the memory values in the
log, how many passes, and the end line.

## 7. Report

Put the results in `docs/windows-test-results.md` on the branch (no design names), commit the fixes
separately, push the branch and open a pull request titled "Windows test". Include:

- Windows version, Fusion version, SolidCraft version, Python version, RAM;
- each section above: passed / failed, numbers, what was fixed;
- open problems with the exact error text.
