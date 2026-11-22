#!/usr/bin/env python3
r"""Crosslink 2026-11-17. Runs the ExtractTimeline pass to the end without anyone at the screen.

Start it in a terminal:   python3 -I nightloop.py        (stop it with Ctrl-C)
On Windows:               py -I nightloop.py
Options:                  --mem-cap GB   Fusion memory at which a round stops and Fusion is restarted
                                         (ExtractTimeline's MEM_CAP_GB, 40 by default; lower it on a
                                         machine with less RAM)
                          --select list  read only the designs in _reextract.txt (a pass of step 5);
                                         default: every design of the inventory (step 3)

Work left is counted from files, the same way ExtractTimeline decides what to read: a design of the
inventory (or of the list) without a .timeline.json yet, a file with states pending, and wanted
states (_state_wanted.json, step 5) that the file does not hold.

Needs the CrosslinkNightRun add-in in Fusion's AddIns folder (it starts ExtractTimeline when a
request file appears). The loop: start Fusion if it is not running, ask for a run, wait for it to
end; if designs are still open (the script stops itself when Fusion's memory passes its cap),
quit Fusion, start it again, ask again. It ends when nothing is left, when a round made no
progress, or after MAX_ROUNDS. ExtractTimeline only reads designs; nothing is saved in Fusion.

macOS and Windows. On Windows Fusion is started with its launcher from the newest folder under
%LOCALAPPDATA%\Autodesk\webdeploy\production (set FUSION_LAUNCHER to a path to use another one), and
the process is found by its name, Fusion360.exe (FUSION_PROCESS to change it).
"""
import glob, json, os, subprocess, sys, time

WINDOWS = os.name == "nt"

WORK = os.path.expanduser("~/cad/work")
OUT = os.path.expanduser("~/cad/fusion-timeline")
REQUEST, DONE, ALIVE = (os.path.join(WORK, n) for n in ("_run_request.json", "_run_done.json", "_nightrun_alive.txt"))
WANTED = os.path.join(WORK, "_state_wanted.json")
SELECT_LIST = os.path.join(WORK, "_reextract.txt")
INVENTORY = os.path.expanduser("~/cad/fusion-inventory/inventory.json")
APP = "Autodesk Fusion"                                             # macOS application name
WIN_PROCESS = os.environ.get("FUSION_PROCESS", "Fusion360.exe")     # Windows process name
MAX_ROUNDS = 30
HARD_CAP_GB = 70.0        # if the script's own guard (40 GB) should fail: end Fusion from outside
LOG = os.path.join(WORK, "_nightloop.log")
SETTINGS = {}             # passed to ExtractTimeline through the add-in (see --mem-cap)


def say(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def pid():
    if WINDOWS:
        r = run(["tasklist", "/FI", "IMAGENAME eq " + WIN_PROCESS, "/FO", "CSV", "/NH"])
        for line in r.stdout.splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) > 1 and cells[0].lower() == WIN_PROCESS.lower() and cells[1].isdigit():
                return int(cells[1])
        return None
    r = run(["pgrep", "-f", "MacOS/Autodesk Fusion"])
    return int(r.stdout.split()[0]) if r.stdout.split() else None


def _win_mem_gb(p):
    """Private bytes of process p on Windows (what ExtractTimeline's memory guard reads too)."""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                   [(n, ctypes.c_size_t) for n in (
                       "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                       "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                       "PeakPagefileUsage", "PrivateUsage")]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    h = k32.OpenProcess(0x1000 | 0x0010, False, p)   # query limited information, read memory
    if not h:
        return None
    try:
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        return c.PrivateUsage / 1e9 if k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb) else None
    finally:
        k32.CloseHandle(h)


def mem_gb(p):
    try:
        if WINDOWS:
            return _win_mem_gb(p)
        t = run(["top", "-l", "1", "-pid", str(p), "-stats", "mem"], timeout=30).stdout.strip().splitlines()[-1].strip()
        return float(t.rstrip("+-")[:-1]) * {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3}[t.rstrip("+-")[-1]]
    except Exception:
        return None


def win_launcher():
    exe = os.environ.get("FUSION_LAUNCHER")
    if exe and os.path.exists(exe):
        return exe
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Autodesk", "webdeploy", "production")
    for name in ("FusionLauncher.exe", WIN_PROCESS):
        hits = sorted(glob.glob(os.path.join(base, "*", name)), key=os.path.getmtime, reverse=True)
        if hits:
            return hits[0]
    return None


def launch():
    if not WINDOWS:
        run(["open", "-a", APP])
        return
    exe = win_launcher()
    if not exe:
        say("Fusion's launcher was not found under %LOCALAPPDATA%\\Autodesk\\webdeploy\\production; "
            "set FUSION_LAUNCHER to its path")
        return
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                     creationflags=0x00000008 | 0x00000200)   # detached, own process group


def end(p, force=False):
    if WINDOWS:
        run(["taskkill"] + (["/F"] if force else []) + ["/PID", str(p)])
    else:
        run(["kill", "-KILL" if force else "-TERM", str(p)])


def _json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def scope():
    """The designs this run is about, as paths under OUT without suffix (None: unknown)."""
    if SETTINGS.get("SELECT") == "list":
        try:
            with open(SELECT_LIST, encoding="utf-8") as fh:
                return [ln.strip().replace("\\", "/") for ln in fh if ln.strip()]
        except OSError:
            return []
    inv = _json(INVENTORY, None)
    if not inv:
        return None
    return [r["path"] for r in inv.get("designs", [])
            if r.get("ext") == "f3d" and r.get("path") and r.get("bucket") != "unreadable"]


def open_designs():
    """(designs that still need a visit, states still to write) -- see the top of this file."""
    wanted = _json(WANTED, {})
    keys = scope()
    if keys is None:
        keys = list(wanted)
    else:
        keys = list(dict.fromkeys(keys + [k for k in wanted if SETTINGS.get("SELECT") != "list" or k in keys]))
    left, pending = 0, 0
    for key in keys:
        d = _json(os.path.join(OUT, key + ".timeline.json"), None)
        if d is None:
            left += 1
            pending += len(wanted.get(key) or [])
            continue
        # the same test as ExtractTimeline's states_missing: wanted steps without a frozen state
        have = {r.get("index") for r in d.get("timeline") or []
                if isinstance(r.get("state_step"), dict) and r["state_step"].get("frozen")}
        missing = set(int(i) for i in wanted.get(key) or []) - have
        if d.get("states_pending") or missing:
            left += 1
            pending += d.get("states_pending") or len(missing)
    return left, pending


def alive():
    try:
        return time.time() - os.path.getmtime(ALIVE) < 30
    except OSError:
        return False


def start_fusion():
    opened = 0
    for _ in range(90):                       # up to 15 minutes for Fusion and the add-in
        p = pid()
        if p is not None and alive():
            return True
        # 18 Nov: a killed Fusion was still listed for a moment, so nothing got started. Look
        # again on every pass and start it whenever no Fusion is there (again after 5 minutes).
        if p is None and time.time() - opened > 300:
            say("starting Fusion")
            launch()
            opened = time.time()
        time.sleep(10)
    return False


def quit_fusion():
    p = pid()
    if p is None:
        return
    say("quitting Fusion")
    if WINDOWS:
        end(p)                                # asks the window to close, like the close button
    else:
        run(["osascript", "-e", 'tell application "%s" to quit' % APP], timeout=60)
    for how, wait in ((None, 90), ("term", 30), ("kill", 90)):
        if how:
            say("Fusion still there, ending it:", how)
            end(p, force=(how == "kill" or WINDOWS))
        for _ in range(wait // 5):
            if pid() is None:
                time.sleep(20)                # let the system take the memory back
                return
            time.sleep(5)


def one_round(n):
    for f in (DONE, REQUEST, REQUEST + ".taken"):
        if os.path.exists(f):
            os.remove(f)
    with open(REQUEST + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"id": "nightloop-%d-%s" % (n, time.strftime("%H%M%S")), "settings": SETTINGS}, fh)
    os.replace(REQUEST + ".tmp", REQUEST)
    last = 0
    began = time.time()
    while not os.path.exists(DONE):
        p = pid()
        if p is None:
            say("Fusion is gone in the middle of a round")
            return {"ok": False, "error": "Fusion exited"}
        if time.time() - last > 600:
            last = time.time()
            g = mem_gb(p)
            try:
                sp = os.path.join(OUT, "status.txt")
                if os.path.getmtime(sp) < began:
                    st = "script is reading the hub, no design opened yet"   # old line is from the last run
                else:
                    with open(sp, encoding="utf-8") as fh:
                        st = fh.read().strip().splitlines()[-1]
            except Exception:
                st = "?"
            say("Fusion %s GB |" % (round(g, 1) if g else "?"), st[:110])
            if g and g > HARD_CAP_GB:
                say("over the hard cap of", HARD_CAP_GB, "GB: ending Fusion from outside")
                end(p, force=True)
                time.sleep(30)
                return {"ok": False, "error": "hard memory cap"}
        time.sleep(15)
    with open(DONE, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    global HARD_CAP_GB
    os.makedirs(WORK, exist_ok=True)
    import argparse
    ap = argparse.ArgumentParser(description="Runs ExtractTimeline rounds unattended (see the top of this file).")
    ap.add_argument("--mem-cap", type=float, metavar="GB",
                    help="Fusion memory at which a round stops and Fusion is restarted (default 40)")
    ap.add_argument("--select", choices=("all", "list"), default="all",
                    help="list: only the designs in _reextract.txt (a step 5 pass); all: the whole inventory")
    a = ap.parse_args()
    SETTINGS["SELECT"] = a.select
    if a.mem_cap:
        SETTINGS.update(MEM_CAP_GB=a.mem_cap, MEM_START_GB=round(a.mem_cap * 0.8, 1))
        HARD_CAP_GB = max(a.mem_cap * 1.75, a.mem_cap + 4)
        say("memory cap %.1f GB (no new design above %.1f GB, hard cap %.1f GB)"
            % (a.mem_cap, SETTINGS["MEM_START_GB"], HARD_CAP_GB))
    left, pending = open_designs()
    if SETTINGS["SELECT"] == "list" and not scope():
        say("--select list, but %s is missing or empty: run next_pass.py first" % SELECT_LIST)
        return 1
    if scope() is None and not os.path.exists(WANTED):
        say("no inventory (%s) and no wanted list: run InventoryHub first" % INVENTORY)
        return 1
    say("start (SELECT %s): %d designs open, about %d states to write" % (SETTINGS["SELECT"], left, pending))
    stuck = 0
    for n in range(1, MAX_ROUNDS + 1):
        if left == 0:
            break
        if not start_fusion():
            say("Fusion or the CrosslinkNightRun add-in did not come up (no fresh %s). Stopping." % ALIVE)
            return 1
        res = one_round(n)
        was, was_left = pending, left
        left, pending = open_designs()
        say("round %d ended: ok=%s mem_stop=%s | %d designs open, about %d states left"
            % (n, res.get("ok"), res.get("mem_stop"), left, pending))
        if res.get("error"):
            say(str(res["error"])[-400:])
        # 21 Nov: a design read for the first time may need no state files at all (mesh bases),
        # so fewer open designs counts as progress too
        stuck = stuck + 1 if pending >= was and left >= was_left else 0
        if stuck >= 2:
            say("two rounds without progress. Stopping; Fusion is left as it is.")
            return 1
        if left:
            quit_fusion()
    say("finished: nothing left to extract." if left == 0 else "stopped after %d rounds." % MAX_ROUNDS)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("stopped by hand; Fusion is left as it is.")
