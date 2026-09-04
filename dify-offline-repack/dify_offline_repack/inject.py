"""Offline injection & verification helpers."""
from __future__ import annotations

import os
from typing import List

from . import util
from .model import RepackOptions


def rewrite_requirements(path: str) -> List[str]:
    """Prepend the offline header to requirements.txt (idempotent)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    header = "--no-index --find-links=./wheels/\n"
    lines = content.splitlines(keepends=True)
    # drop an existing identical header to keep idempotency
    cleaned = [ln for ln in lines if ln.strip() not in ("--no-index --find-links=./wheels/",
                                                        "--no-index --find-links=./wheels",
                                                        "--find-links=./wheels/")]
    cleaned.insert(0, header)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(cleaned)
    return ["prepended offline header to requirements.txt"]


def pyproject_to_requirements(plugin_dir: str, pkg_name: str) -> List[str]:
    """Normalize dependency declaration to the requirements.txt install path.

    - When both requirements.txt and pyproject.toml exist (official template keeps
      them in sync): keep requirements.txt and remove pyproject.toml/uv.lock from the
      payload so daemon's `uv pip install -r requirements.txt` offline path applies.
    - When only pyproject.toml exists: extract [project].dependencies into
      requirements.txt, then remove pyproject.toml/uv.lock.
    (daemon launches main.py from cwd; it never needs the project itself installed)
    """
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore

    pyproject_path = os.path.join(plugin_dir, "pyproject.toml")
    req_path = os.path.join(plugin_dir, "requirements.txt")
    changes: List[str] = []

    if not os.path.exists(req_path):
        with open(pyproject_path, "rb") as f:
            doc = tomllib.load(f)
        deps = ((doc.get("project") or {}).get("dependencies")) or []
        if not deps:
            raise RuntimeError("pyproject.toml has no [project].dependencies to convert")
        with open(req_path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"# from {pkg_name} pyproject [project].dependencies\n{d}" for d in deps))
            f.write("\n")
        changes.append(f"converted {pkg_name} pyproject [project].dependencies to requirements.txt")
    else:
        changes.append("kept existing requirements.txt (pyproject present but both kept in sync)")

    for name in ("pyproject.toml", "uv.lock"):
        p = os.path.join(plugin_dir, name)
        if os.path.exists(p):
            os.remove(p)
            changes.append(f"removed {name} from payload (daemon uses requirements.txt path)")
    return changes


def ensure_no_runtime_leftovers(plugin_dir: str) -> List[str]:
    """Drop things that must never travel in the plugin payload."""
    dropped = []
    for name in (".venv", ".uv-cache", "__pycache__"):
        # __pycache__ handled via walk below
        p = os.path.join(plugin_dir, name)
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            dropped.append(name)
    return dropped


def summarize_uncompressed(artifact_path: str) -> int:
    import zipfile
    with zipfile.ZipFile(artifact_path) as zf:
        return sum(i.file_size for i in zf.infolist())
