"""Dependency locking with uv (deterministic pins for target python/platform)."""
from __future__ import annotations

import os
import re
import tempfile
from typing import List, Optional

from . import util


def uv_compile_lock(requirements_path: str, lock_path: str, *, target_python: str,
                    index_url: str, uv_path: str,
                    constraints: Optional[List[str]] = None) -> List[str]:
    """uv pip compile into an exact pin list (linux python-platform)."""
    cmd = [
        uv_path, "pip", "compile",
        requirements_path,
        "--python-version", target_python,
        "--python-platform", "linux",
        "--index-url", index_url,
        "-o", lock_path,
        "--quiet",
    ]
    for c in constraints or []:
        cmd += ["-c", c]
    code, out, err = util.run(cmd, timeout=900)
    if code != 0:
        raise RuntimeError(f"uv pip compile failed (exit {code}):\n{err[-2000:]}")
    pins = parse_pins(lock_path)
    if not pins:
        raise RuntimeError("uv pip compile produced no pinned packages")
    return pins


def lock_pins_cp312(requirements_path: str, lock_dir: str, *, target_python: str,
                    index_url: str, uv_path: str, archs: List[str],
                    glibc_minor: int) -> List[str]:
    """Compile a lock whose every pinned package still ships a cp312 wheel.

    The 2026+ ecosystem increasingly drops cp312 wheels on latest releases while the
    daemon runtime is fixed at python 3.12; this guard down-pins such packages to the
    newest version that still provides cp312 wheels (uv constraint file), iterating
    until stable. Distributions that never ship cp312 wheels raise a clear error.
    Intermediate files (lock.txt / cp312-pins.txt) are written into lock_dir, never
    into the plugin source directory.
    """
    from . import wheels

    os.makedirs(lock_dir, exist_ok=True)
    overrides: dict = {}
    sdist_ok: set = set()      # deps resolved via sdist fallback (never ship cp312 wheels)
    pins: List[str] = []
    for _round in range(6):
        constraints = None
        if overrides:
            con_path = os.path.join(lock_dir, "cp312-pins.txt")
            with open(con_path, "w", encoding="utf-8") as f:
                f.writelines(f"{n}=={v}\n" for n, v in overrides.items())
            constraints = [con_path]
        pins = uv_compile_lock(requirements_path, os.path.join(lock_dir, "lock.txt"),
                               target_python=target_python, index_url=index_url,
                               uv_path=uv_path, constraints=constraints)
        fully_missing = []
        for pin in pins:
            name, _, ver = pin.partition("==")
            if name in sdist_ok:
                continue
            avail = wheels.arch_availability(name, ver, archs, glibc_minor, index_url)
            if not any(avail.values()):
                fully_missing.append((name, ver))
        if not fully_missing:
            break
        new_over: dict = {}
        impossible = []
        for name, ver in fully_missing:
            best, _av = wheels.best_cp312_version(name, archs, glibc_minor, index_url)
            if best is None:
                if wheels.sdist_any_version(name, index_url):
                    sdist_ok.add(name)
                    util.log.warning("dep %s %s has no cp312 wheels; using sdist fallback", name, ver)
                else:
                    impossible.append(name)
            elif best != ver:
                new_over[name] = best
                util.log.warning("down-pin %s %s -> %s (latest dropped cp312 wheels)", name, ver, best)
            else:  # best==ver but still fully missing -> contradiction, treat impossible
                impossible.append(name)
        if impossible:
            raise RuntimeError(
                "the following dependencies never ship python3.12(cp312) wheels AND provide no "
                "sdist source for offline building: " + ", ".join(impossible) +
                ". Plugin is not offline-supportable on daemon python 3.12.")
        if not new_over:
            break
        overrides.update(new_over)
    return pins


PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^;\s#]+)")


def parse_pins(lock_path: str) -> List[str]:
    """Return 'name==version' lines from a uv-compiled requirements file."""
    pins: List[str] = []
    with open(lock_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PIN_RE.match(line)
            if m:
                pins.append(f"{m.group(1)}=={m.group(2)}")
    return pins


def offline_recheck(lock_or_requirements: str, wheels_dir: str, *, target_python: str,
                    uv_path: str, python_platform: str = "linux") -> bool:
    """Verify that the pinned closure is fully satisfiable from wheels_dir alone (no index).

    python_platform must reflect the target arch (e.g. 'aarch64-unknown-linux-gnu'),
    otherwise a single-arch (aarch64) wheel set is wrongly judged unsatisfiable on the
    default x86_64 'linux' target.
    """
    out = os.path.join(os.path.dirname(lock_or_requirements), f"offline-recheck-{_safe(python_platform)}.txt")
    cmd = [
        uv_path, "pip", "compile",
        lock_or_requirements,
        "--no-index",
        "--find-links", wheels_dir,
        "--python-version", target_python,
        "--python-platform", python_platform,
        "-o", out,
        "--quiet",
    ]
    code, _out, err = util.run(cmd, timeout=900)
    if code != 0:
        util.log.warning("offline recheck failed (%s):\n%s", python_platform, err[-1500:])
        return False
    return True


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)
