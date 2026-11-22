#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install the toolkit's Fusion parts into Fusion's API folder (macOS and Windows).

    python3 install.py            the three Fusion scripts (InventoryHub, ExportAllDesigns, ExtractTimeline)
    python3 install.py --addin    also the CrosslinkNightRun add-in, for unattended extraction (fusion/nightloop.py)
    python3 install.py --remove   take all of them out again

On Windows use `py install.py`. Run it again after updating the toolkit. It only copies folders;
it changes nothing in Fusion's settings. Restart Fusion afterwards if it was running.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "fusion")
SCRIPTS = ("InventoryHub", "ExportAllDesigns", "ExtractTimeline")
SHARED = {"InventoryHub", "ExtractTimeline"}   # get the bucket rule; ExportAllDesigns stands alone
ADDIN = "CrosslinkNightRun"


def api_folder():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, "Autodesk", "Autodesk Fusion 360", "API")
    return os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/API")


def copy_folder(name, dest_parent):
    dest = os.path.join(dest_parent, name)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(os.path.join(SRC, name), dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addin", action="store_true", help="also install the CrosslinkNightRun add-in")
    ap.add_argument("--remove", action="store_true", help="remove the scripts and the add-in")
    ap.add_argument("--api", help="Fusion's API folder, if it is not in the usual place")
    a = ap.parse_args()

    api = a.api or api_folder()
    if not os.path.isdir(api):
        print("Fusion's API folder was not found: " + api)
        print("Start Fusion once (it creates the folder), then run this again, or pass --api.")
        return 1
    scripts, addins = os.path.join(api, "Scripts"), os.path.join(api, "AddIns")

    if a.remove:
        for name, parent in [(s, scripts) for s in SCRIPTS] + [(ADDIN, addins)]:
            p = os.path.join(parent, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
                print("removed           " + name)
        return 0

    os.makedirs(scripts, exist_ok=True)
    os.makedirs(addins, exist_ok=True)
    for s in SCRIPTS:
        dest = copy_folder(s, scripts)
        if s in SHARED:
            shutil.copy2(os.path.join(SRC, "common", "f2s_buckets.py"), dest)
        print("installed script  " + s)
    if a.addin:
        copy_folder(ADDIN, addins)
        print("installed add-in  {} (starts with Fusion; run with --remove, or delete its folder, to switch it off)"
              .format(ADDIN))
    print("Fusion API folder: " + api)
    print("In Fusion: Utilities > Add-Ins > Scripts and Add-Ins. Restart Fusion if it was running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
