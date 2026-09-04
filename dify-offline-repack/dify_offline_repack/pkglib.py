"""Read / write Dify plugin packages (.difypkg == zip) and their manifests."""
from __future__ import annotations

import io
import os
import zipfile
from typing import Dict, List

import yaml

from .model import PluginInfo


class PkgError(Exception):
    pass


def inspect_difypkg(path: str) -> PluginInfo:
    """Open a .difypkg and return its plugin identity + dependency file info."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PkgError(f"invalid difypkg (zip) file: {e}") from e
    with zf:
        names = zf.namelist()
        if "manifest.yaml" not in names:
            raise PkgError("manifest.yaml not found at package root")
        manifest_raw = zf.read("manifest.yaml")
        info = inspect_manifest(manifest_raw)
        info.source_size_bytes = os.path.getsize(path)
        info.source_uncompressed_bytes = sum(i.file_size for i in zf.infolist())
        found = []
        if "requirements.txt" in names:
            found.append("requirements.txt")
        if "pyproject.toml" in names:
            found.append("pyproject.toml")
        info.original_dependency_files = found
        if "pyproject.toml" in found and "requirements.txt" not in found:
            info.dependency_file = "pyproject.toml"
        return info


def inspect_manifest(raw: bytes) -> PluginInfo:
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001
        raise PkgError(f"failed to parse manifest.yaml: {e}") from e
    if not isinstance(doc, dict):
        raise PkgError("manifest.yaml is not a mapping")
    author = str(doc.get("author") or "")
    name = str(doc.get("name") or "")
    version = str(doc.get("version") or "")
    if not (author and name and version):
        raise PkgError("manifest.yaml missing author/name/version")
    meta = doc.get("meta") or {}
    runner = meta.get("runner") or {}
    pv = str(runner.get("version") or "3.12")
    if not pv.startswith("3.12"):
        # daemon 0.6.10 always creates venv with python 3.12; tolerate declaration,
        # but note it in the info for reporting.
        pass
    return PluginInfo(
        author=author,
        name=name,
        version=version,
        type=str(doc.get("type") or "plugin"),
        runner_language=str(runner.get("language") or "python"),
        runner_python=pv,
    )


def extract_difypkg(path: str, dst: str) -> List[str]:
    """Extract a difypkg into dst (root files land directly in dst)."""
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        out_names = []
        for zi in zf.infolist():
            name = zi.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise PkgError(f"unsafe path in difypkg: {name}")
            target = os.path.join(dst, *name.split("/"))
            if not os.path.abspath(target).startswith(os.path.abspath(dst) + os.sep) and target != dst:
                raise PkgError(f"path escape in difypkg: {name}")
            if zi.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(zi) as src, open(target, "wb") as out:
                out.write(src.read())
            out_names.append(name)
        return out_names


def read_text(path: str, max_kb: int = 512) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_kb * 1024)


def write_zip_from_dir(src_dir: str, out_path: str, skip_names: List[str] | None = None,
                       comment: bytes = b"") -> int:
    """Zip a directory tree (files relative to src_dir) into out_path."""
    skip_names = skip_names or []
    total = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                if rel in skip_names:
                    continue
                zf.write(full, rel)
                total += os.path.getsize(full)
        if comment:
            zf.comment = comment
    return total
