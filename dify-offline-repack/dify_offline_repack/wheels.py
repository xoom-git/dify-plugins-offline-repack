"""Acquire pinned dependency wheels straight from the PEP 503 index.

Rationale (see docs/03-验证记录.md C1/C2): pip download evaluates sys_platform
markers against the running host, so foreign-OS acquisition must be driven by the
index metadata instead. uv provides deterministic pins; this module downloads the
exact wheel files (per PEP 425 tags) for the target cp312 / linux / arch.
"""
from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

from . import util
from .model import WheelRecord

_ATTR_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_ALIAS_RE = re.compile(r"[-_.]+")


def project_alias(name: str) -> str:
    return _ALIAS_RE.sub("", name).lower()


def _version_match(pin_ver: str, cand_ver: str) -> bool:
    if pin_ver == cand_ver:
        return True
    # tolerate normalization differences like "1.0" vs "1.0.0"
    a = _ALIAS_RE.sub(".", pin_ver).strip(".").split(".")
    b = _ALIAS_RE.sub(".", cand_ver).strip(".").split(".")
    # allow trailing zero padding difference up to segment count 3
    for _ in range(max(len(a), len(b)) - min(len(a), len(b))):
        if len(a) < len(b):
            a.append("0")
        else:
            b.append("0")
    return a == b


_MANYLINUX_LEGACY = {"manylinux1": 5, "manylinux2010": 12, "manylinux2014": 17}
_MANYLINUX_RE = re.compile(r"^manylinux_2_(\d+)_([a-z0-9_]+)$")


def _platform_ok(plat: str, arch: str, glibc_minor: int) -> Tuple[bool, bool]:
    """Return (plat_acceptable, arch_ok) for a (possibly dot-composite) platform tag."""
    if plat == "any":
        return True, True
    arch_ok = False
    for sub in plat.split("."):
        m = _MANYLINUX_RE.match(sub)
        if m:
            minor, machine = int(m.group(1)), m.group(2)
        else:
            lm = re.match(r"^(manylinux1|manylinux2010|manylinux2014)_([a-z0-9_]+)$", sub)
            if not lm:
                continue
            minor, machine = _MANYLINUX_LEGACY[lm.group(1)], lm.group(2)
        if machine not in ("x86_64", "aarch64"):
            continue
        if minor > glibc_minor:
            continue
        arch_ok = arch_ok or (machine == arch)
        return True, arch_ok
    return (True, arch_ok) if arch_ok else (False, False)


def wheel_score(py: str, abi: str, plat: str, arch: str, glibc_minor: int) -> Tuple[int, int]:
    """Return (acceptable_priority, arch_rank) or (-1,-1) if incompatible.

    Accepts cp312 / py3 wheels for linux: -any wheels, or (composite) manylinux
    tags whose glibc 2.N is <= the target image glibc (Debian bookworm=2.36).
    arch_rank 0 means a wheel for the requested arch exists in the tag list.
    """
    plat_ok, arch_ok = _platform_ok(plat, arch, glibc_minor)
    if not plat_ok:
        return (-1, -1)
    # python tag rules (target runtime cp312)
    py_tokens = py.split(".")
    py3_universal = ("py3" in py_tokens) and all(t in ("py2", "py3") for t in py_tokens)
    if (py3_universal or py == "py3") and abi in ("none", "abi3"):
        return (3 if plat == "any" else 2, 0 if arch_ok else 1)
    # abi3 wheels (e.g. cp310-abi3-*) are importable on any CPython >= their minimum, incl 3.12
    m312 = re.match(r"^cp3(\d{1,2})$", py)
    if abi == "abi3" and m312 and int(m312.group(1)) <= 12:
        return (4, 0 if arch_ok else 1)
    if py == "cp312":
        if abi in ("none", "abi3", "cp312"):
            return (5 if abi == "cp312" else 4, 0 if arch_ok else 1)
    return (-1, -1)


def _pick_wheel(pin_name: str, pin_ver: str, arch: str, floor: int,
                links: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """links: list of (href, filename). Return (href, filename) best candidate."""
    best: Optional[Tuple[int, int, str, str]] = None
    for href, fn in links:
        if not fn.endswith(".whl"):
            continue
        # dist-version-python-abi-platform.whl
        base = fn[:-4]
        parts = base.split("-")
        if len(parts) < 5:
            continue
        dist = parts[0]
        if project_alias(dist) != project_alias(pin_name):
            continue
        # find version: it is the second field only when tags contain '-' none… wheels
        # always: dist-version-py-abi-plat with version possibly containing dots only.
        # python tag starts with cp/py: locate first field matching ^(cp|py)\d
        tag_idx = None
        for i in range(1, len(parts) - 2):
            if re.match(r"^(cp|py)\d", parts[i]):
                tag_idx = i
                break
        if tag_idx is None:
            continue
        cand_ver = "-".join(parts[1:tag_idx])
        if not _version_match(pin_ver, cand_ver):
            continue
        if tag_idx + 2 >= len(parts):
            continue
        py, abi, plat = parts[tag_idx], parts[tag_idx + 1], "-".join(parts[tag_idx + 2:])
        score, arch_rank = wheel_score(py, abi, plat, arch, floor)
        if score < 0:
            continue
        cand = (score, arch_rank, href, fn)
        if best is None or (score, -arch_rank) > (best[0], -best[1]):
            best = cand
    if best is None:
        return None
    return best[2], best[3]


def fetch_index_page(index_url: str, pin_name: str, timeout: int = 60) -> List[Tuple[str, str]]:
    safe = urllib.parse.quote(project_alias(pin_name).replace("-", ""))
    # PEP 503: project page path uses hyphen-normalized name
    path_name = re.sub(r"[-_.]+", "-", pin_name).lower()
    url = index_url.rstrip("/") + "/" + path_name + "/"
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            body = util.http_get(url, timeout=timeout).decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 2:
                import time as _t
                _t.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"failed to fetch index page for {pin_name}: {last_err}")
    links: List[Tuple[str, str]] = []
    for m in _ATTR_RE.finditer(body):
        href = m.group(1).strip()
        fn = (m.group(2) or "").strip()
        if not fn:
            fn = urllib.parse.unquote(href.split("/")[-1].split("#")[0])
        links.append((href, fn))
    return links


def arch_availability(pin_name: str, pin_ver: str, archs: List[str], glibc_minor: int,
                      index_url: str) -> Dict[str, bool]:
    """Whether an acceptable cp312 wheel exists for pin on each requested arch."""
    links = fetch_index_page(index_url, pin_name)
    return {arch: _pick_wheel(pin_name, pin_ver, arch, glibc_minor, links) is not None
            for arch in archs}


def best_cp312_version(pin_name: str, archs: List[str], glibc_minor: int,
                       index_url: str) -> Tuple[Optional[str], Dict[str, bool]]:
    """Newest version of pin_name that still ships cp312 wheels.

    Scans the index page once. Returns (best_version or None if the dist never ships
    cp312 wheels, availability per requested arch). Used to down-pin resolutions when
    the latest release dropped cp312 wheels (2026+ ecosystem trend).
    """
    links = fetch_index_page(index_url, pin_name)
    by_ver: Dict[str, List[Tuple[str, str]]] = {}
    for href, fn in links:
        if not fn.endswith(".whl"):
            continue
        parts = fn[:-4].split("-")
        tag_idx = None
        for i in range(1, len(parts) - 2):
            if re.match(r"^(cp|py)\d", parts[i]):
                tag_idx = i
                break
        if tag_idx is None:
            continue
        by_ver.setdefault("-".join(parts[1:tag_idx]), []).append((href, fn))

    def _ver_key(v: str):
        toks = re.findall(r"\d+", v)
        return tuple(int(x) for x in toks[:4]) + (0,) * (4 - len(toks)) + (v,)

    best: Optional[str] = None
    best_avail: Dict[str, bool] = {}
    for ver in sorted(by_ver, key=_ver_key, reverse=True):
        avail: Dict[str, bool] = {}
        for arch in archs:
            avail[arch] = False
            for href, fn in by_ver[ver]:
                base = fn[:-4]
                parts = base.split("-")
                if project_alias(parts[0]) != project_alias(pin_name):
                    continue
                for i in range(1, len(parts) - 2):
                    if re.match(r"^(cp|py)\d", parts[i]):
                        if i + 2 < len(parts) and _version_match(ver, "-".join(parts[1:i])):
                            py, abi, plat = parts[i], parts[i + 1], "-".join(parts[i + 2:])
                            score, _rank = wheel_score(py, abi, plat, arch, glibc_minor)
                            if score >= 0:
                                avail[arch] = True
                        break
        if all(avail.values()):
            return ver, avail
        if best is None:
            best, best_avail = ver, avail
    return best, best_avail


def sdist_link(name: str, ver: str, index_url: str) -> Optional[Tuple[str, str]]:
    """Find the sdist (tar.gz/zip) of name==ver on the index; returns (href, filename)."""
    links = fetch_index_page(index_url, name)
    for href, fn in links:
        if not (fn.endswith(".tar.gz") or fn.endswith(".zip")):
            continue
        stem = fn[: -len(".tar.gz")] if fn.endswith(".tar.gz") else fn[: -len(".zip")]
        parts = stem.split("-")
        if len(parts) < 2 or project_alias(parts[0]) != project_alias(name):
            continue
        if _version_match(ver, "-".join(parts[1:])):
            return href, fn
    return None


def sdist_any_version(name: str, index_url: str) -> bool:
    links = fetch_index_page(index_url, name)
    return any(fn.endswith((".tar.gz", ".zip")) for _h, fn in links)


def download_sdist(pin: str, out_dir: str, index_url: str) -> Optional[str]:
    """Download the sdist for a pin into out_dir; returns filename or None."""
    name, _, ver = pin.partition("==")
    hit = sdist_link(name, ver, index_url)
    if hit is None:
        return None
    href, fn = hit
    sha = ""
    frag = urllib.parse.urlparse(href).fragment
    if frag.startswith("sha256="):
        sha = frag.split("=", 1)[1]
    target = os.path.join(out_dir, fn)
    if not os.path.exists(target):
        util.log.info("downloading sdist %s", fn)
        data = util.http_get(href.split("#")[0], timeout=300)
        if sha and hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError(f"sha256 mismatch for {fn}")
        with open(target, "wb") as f:
            f.write(data)
    return fn


def ensure_build_backends(out_dir: str, index_url: str) -> List[str]:
    """Ensure common PEP517 build-backend wheels exist (for offline sdist builds)."""
    os.makedirs(out_dir, exist_ok=True)
    installed = []
    for backend in ("setuptools", "wheel", "packaging"):
        prefix = project_alias(backend) + "-"
        if any(fn.startswith(prefix) for fn in os.listdir(out_dir)):
            continue
        links = fetch_index_page(index_url, backend)
        best = None
        for href, fn in links:
            if not fn.endswith("-py3-none-any.whl"):
                continue
            base = fn[: -len(".whl")]
            parts = base.split("-")
            if len(parts) < 4 or project_alias(parts[0]) != project_alias(backend):
                continue
            v = "-".join(parts[1:-3])
            key = tuple(int(x) for x in re.findall(r"\d+", v))
            if best is None or key > best[0]:
                best = (key, href, fn)
        if best is None:
            util.log.warning("no pure wheel for build backend %s", backend)
            continue
        _key, href, fn = best
        data = util.http_get(href.split("#")[0], timeout=300)
        with open(os.path.join(out_dir, fn), "wb") as f:
            f.write(data)
        installed.append(fn)
    return installed


def download_wheels_for_pins(pins: List[str], out_dir: str, *, arch: str,
                             index_url: str, glibc_minor: int,
                             allow_any_shared: Dict[str, str]) -> Tuple[List[WheelRecord], List[str]]:
    """Download wheels for every pin into out_dir.

    allow_any_shared: name -> downloaded filename for py3-none-any wheels already saved
    (pure wheels are identical across arches and are downloaded once).
    Returns (records, missing).
    """
    os.makedirs(out_dir, exist_ok=True)
    records: List[WheelRecord] = []
    missing: List[str] = []
    for pin in pins:
        name, _, ver = pin.partition("==")
        links = fetch_index_page(index_url, name)
        hit = _pick_wheel(name, ver, arch, glibc_minor, links)
        if hit is None:
            missing.append(pin)
            cands = [fn for _h, fn in links if fn.endswith(".whl")][:6]
            util.log.info("no acceptable wheel for %s (%s): candidates=%s", pin, arch, cands)
            continue
        href, fn = hit
        # sha256 fragment
        sha = ""
        frag = urllib.parse.urlparse(href).fragment
        if frag.startswith("sha256="):
            sha = frag.split("=", 1)[1]
        pure_any = fn.endswith("-py3-none-any.whl")
        if pure_any and name in allow_any_shared:
            target = os.path.join(out_dir, fn)
            if not os.path.exists(target):
                os.link(allow_any_shared[name], target) if os.name != "nt" else __import__("shutil").copyfile(
                    allow_any_shared[name], target)
            records.append(WheelRecord(name, ver, fn, href, sha, "any"))
            continue
        if not sha:
            util.log.warning("no sha256 fragment for %s %s", name, fn)
        target = os.path.join(out_dir, fn)
        if not os.path.exists(target):
            util.log.info("downloading %s (%s)", fn, arch)
            data = util.http_get(href.split("#")[0], timeout=300)
            if sha and hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"sha256 mismatch for {fn}")
            with open(target, "wb") as f:
                f.write(data)
        records.append(WheelRecord(name, ver, fn, href, sha, "any" if pure_any else arch))
        if pure_any:
            allow_any_shared[name] = target
    return records, missing
