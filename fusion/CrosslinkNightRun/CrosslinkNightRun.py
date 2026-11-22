# Crosslink 2026-11-15 (night). Fusion add-in, written for one purpose: let the extraction rounds
# of the Fusion -> SolidCraft migration run unattended.
#
# It watches for ~/cad/work/_run_request.json. When that file appears it loads the installed
# ExtractTimeline script afresh (so an updated script is picked up without restarting Fusion),
# applies the settings named in the request, runs it with its message boxes written to a file
# instead of shown, and writes ~/cad/work/_run_done.json. Nothing else: it adds no commands, no
# buttons, and saves nothing. ExtractTimeline itself only reads designs.
#
# Request:  {"id": "<any text>", "settings": {"SELECT": "list" | "all", ...}}
# Switch it off: remove this folder from Fusion's AddIns folder (or untick "Run on Startup").
import adsk.core
import importlib.util
import json
import os
import threading
import time
import traceback

WORK = os.path.expanduser("~/cad/work")
REQUEST = os.path.join(WORK, "_run_request.json")
DONE = os.path.join(WORK, "_run_done.json")
ALIVE = os.path.join(WORK, "_nightrun_alive.txt")
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "Scripts", "ExtractTimeline", "ExtractTimeline.py")
EVENT_ID = "CrosslinkNightRunEvent"
ALLOWED = ("SELECT", "SELECT_LIST", "LIMIT", "STATE_WANTED", "MEASURE_BUDGET_S", "INPUT_BUDGET_S",
           "MEM_CAP_GB", "MEM_START_GB")

_state = {"stop": None, "event": None, "handler": None, "busy": False}


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


class _Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        if _state["busy"]:
            return
        _state["busy"] = True
        req, out = {}, {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(REQUEST, "r", encoding="utf-8") as fh:
                req = json.load(fh)
            os.replace(REQUEST, REQUEST + ".taken")
            out["id"] = req.get("id")
            spec = importlib.util.spec_from_file_location("crosslink_extract_timeline", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for k, v in (req.get("settings") or {}).items():
                if k in ALLOWED:
                    setattr(mod, k, os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v)
            mod.QUIET = True
            out["schema"] = getattr(mod, "SCHEMA", None)
            mod.run({"IsApplicationStartup": False})
            out["ok"] = True
            # 17 Nov: did the script stop itself because Fusion's memory passed its cap? (GB, or None)
            out["mem_stop"] = (getattr(mod, "_CHUNK", None) or {}).get("stop")
        except Exception:
            out["ok"] = False
            out["error"] = traceback.format_exc()[-1500:]
        finally:
            out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                _write(DONE, out)
            except Exception:
                pass
            _state["busy"] = False


def _watch(stop):
    app = adsk.core.Application.get()
    while not stop.wait(5.0):
        try:
            with open(ALIVE, "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + (" busy" if _state["busy"] else " idle") + "\n")
            if not _state["busy"] and os.path.exists(REQUEST):
                app.fireCustomEvent(EVENT_ID, "")
        except Exception:
            pass


def run(context):
    try:
        os.makedirs(WORK, exist_ok=True)
        app = adsk.core.Application.get()
        _state["event"] = app.registerCustomEvent(EVENT_ID)
        _state["handler"] = _Handler()
        _state["event"].add(_state["handler"])
        _state["stop"] = threading.Event()
        threading.Thread(target=_watch, args=(_state["stop"],), daemon=True).start()
    except Exception:
        try:
            with open(os.path.join(WORK, "_nightrun_error.txt"), "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass


def stop(context):
    try:
        if _state["stop"] is not None:
            _state["stop"].set()
        app = adsk.core.Application.get()
        if _state["event"] is not None and _state["handler"] is not None:
            _state["event"].remove(_state["handler"])
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
