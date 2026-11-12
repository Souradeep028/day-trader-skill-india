"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def folder_path(folder):
    """A skill folder: at the root (the NSE pack) or inside a second plugin (congress-trades)."""
    for base in (ROOT, os.path.join(ROOT, "congress-trades")):
        if os.path.isdir(os.path.join(base, folder)):
            return os.path.join(base, folder)
    return os.path.join(ROOT, folder)


def load(folder, name):
    path = os.path.join(folder_path(folder), name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
