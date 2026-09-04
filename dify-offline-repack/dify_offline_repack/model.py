"""Shared value objects and options for the repack engine."""
from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional


@dataclasses.dataclass
class PluginInfo:
    """Parsed identity/metadata of a Dify plugin package."""

    author: str
    name: str
    version: str
    type: str = "plugin"
    runner_language: str = "python"
    runner_python: str = "3.12"
    dependency_file: str = "requirements.txt"  # normalized to requirements in offline pkg
    original_dependency_files: List[str] = dataclasses.field(default_factory=list)
    source_size_bytes: int = 0
    source_uncompressed_bytes: int = 0

    @property
    def identity(self) -> str:
        return f"{self.author}/{self.name}:{self.version}"

    @property
    def safe_name(self) -> str:
        return f"{self.author}-{self.name}"


@dataclasses.dataclass
class RepackOptions:
    """Options controlling one repack run."""

    target_python: str = "3.12"          # python version of the target daemon venv
    archs: List[str] = dataclasses.field(default_factory=lambda: ["x86_64", "aarch64"])
    glibc_minor: int = 36                # accept manylinux_2_N wheels with N<=glibc 2.N of the
                                         # target image (Debian bookworm = 2.36; lower for older images)
    index_url: str = "https://pypi.org/simple"
    uv_path: Optional[str] = None        # default: found on PATH
    keep_pyproject: bool = False         # not yet supported -> normalized to requirements path
    dry_run: bool = False


@dataclasses.dataclass
class WheelRecord:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    arch: str          # "x86_64" | "aarch64" | "any"


@dataclasses.dataclass
class RepackReport:
    ok: bool
    plugin: PluginInfo
    lock_lines: List[str]
    wheels: List[WheelRecord]
    artifact_path: Optional[str] = None
    artifact_sha256: str = ""
    artifact_size: int = 0
    uncompressed_size: int = 0
    offline_recheck_ok: Optional[bool] = None
    warnings: List[str] = dataclasses.field(default_factory=list)
    errors: List[str] = dataclasses.field(default_factory=list)
    transformations: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)
