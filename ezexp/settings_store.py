import os
import json
from PySide6.QtCore import QSettings

from ezexp import APP_DIR

def get_vaults():
    v = QSettings("ezexp", "EzExp").value("vaults", [])
    if isinstance(v, str):
        v = [v]
    return [p for p in (v or []) if isinstance(p, str) and p and os.path.isdir(p)]

def set_vaults(paths):
    s = QSettings("ezexp", "EzExp")
    s.setValue("vaults", [os.path.normpath(p) for p in paths])
    s.sync()

APP_EXTS = {"exe", "lnk", "bat", "cmd", "msi"}

def get_quicklaunch():
    v = QSettings("ezexp", "EzExp").value("quicklaunch", [])
    if isinstance(v, str):
        v = [v]
    return [os.path.normpath(p) for p in (v or [])
            if isinstance(p, str) and p]

def set_quicklaunch(paths):
    s = QSettings("ezexp", "EzExp")
    s.setValue("quicklaunch", [os.path.normpath(p) for p in paths])
    s.sync()

def get_launch_counts():
    v = QSettings("ezexp", "EzExp").value("launch_counts", "")
    try:
        d = json.loads(v) if isinstance(v, str) and v else (v or {})
    except (ValueError, TypeError):
        d = {}
    out = {}
    for k, c in (d.items() if isinstance(d, dict) else []):
        try:
            out[k] = int(c)
        except (TypeError, ValueError):
            pass
    return out

def bump_launch_count(path):
    s = QSettings("ezexp", "EzExp")
    d = get_launch_counts()
    d[path] = d.get(path, 0) + 1
    s.setValue("launch_counts", json.dumps(d))
    s.sync()

def remove_launch_count(path):
    s = QSettings("ezexp", "EzExp")
    d = {p: c for p, c in get_launch_counts().items()
         if p.lower() != path.lower()}
    s.setValue("launch_counts", json.dumps(d))
    s.sync()
