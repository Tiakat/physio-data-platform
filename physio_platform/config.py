"""Project configuration: load and validate the YAML files in ``projects/``.

One YAML file drives the whole pipeline for one project, so the same code
processes all ~22 projects. Anything project-specific (file layout, signal
types, processing rules) lives here, not in the code.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a project config is missing or invalid."""


@dataclass
class DropboxConfig:
    source_dir: str          # path inside Dropbox, e.g. "/Projets actifs/eeg/raw"
    file_pattern: str = "*.csv"
    recursive: bool = True


@dataclass
class ProcessingConfig:
    # "module:function" -> process_patient(raw_path, cfg, out_dir, log) -> dict
    adapter: str = ""
    chunk_seconds: float = 600.0   # stream raw files in chunks this long
    seed: int = 0                  # fixed seed: runs are reproducible


@dataclass
class OutputsConfig:
    root: str = "./processed"      # Parquet store root
    write_clean_signals: bool = True
    write_features: bool = True


@dataclass
class WebsiteConfig:
    feed_dir: str = "./website"    # JSON feed + figures the site reads
    figures: list[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    name: str
    description: str = ""
    version: int = 1
    dropbox: DropboxConfig = field(default_factory=DropboxConfig)
    staging_dir: str = "./staging"
    sampling_rate_hz: float = 0.0
    channels: list[str] = field(default_factory=list)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    outputs: OutputsConfig = field(default_factory=OutputsConfig)
    website: WebsiteConfig = field(default_factory=WebsiteConfig)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Short hash of the config: outputs are keyed by it, so changing the
        config never silently overwrites results from an older config."""
        blob = yaml.safe_dump(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.name,
            "description": self.description,
            "version": self.version,
            "dropbox": {"source_dir": self.dropbox.source_dir,
                        "file_pattern": self.dropbox.file_pattern,
                        "recursive": self.dropbox.recursive},
            "staging_dir": self.staging_dir,
            "sampling_rate_hz": self.sampling_rate_hz,
            "channels": self.channels,
            "processing": {"adapter": self.processing.adapter,
                           "chunk_seconds": self.processing.chunk_seconds,
                           "seed": self.processing.seed},
            "outputs": {"root": self.outputs.root,
                        "write_clean_signals": self.outputs.write_clean_signals,
                        "write_features": self.outputs.write_features},
            "website": {"feed_dir": self.website.feed_dir,
                        "figures": self.website.figures},
            "notes": self.notes,
        }


def _req(mapping: dict, key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"[{ctx}] missing required key: {key!r}")
    return mapping[key]


def load_project(path: str | Path) -> ProjectConfig:
    """Load and validate one project YAML file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    name = _req(raw, "project", str(path))
    db = raw.get("dropbox", {}) or {}
    proc = raw.get("processing", {}) or {}
    outs = raw.get("outputs", {}) or {}
    web = raw.get("website", {}) or {}

    cfg = ProjectConfig(
        name=str(name),
        description=str(raw.get("description", "")),
        version=int(raw.get("version", 1)),
        dropbox=DropboxConfig(
            source_dir=str(_req(db, "source_dir", "dropbox")),
            file_pattern=str(db.get("file_pattern", "*.csv")),
            recursive=bool(db.get("recursive", True)),
        ),
        staging_dir=str(raw.get("staging_dir", "./staging")),
        sampling_rate_hz=float(raw.get("sampling_rate_hz", 0.0)),
        channels=[str(c) for c in (raw.get("channels") or [])],
        processing=ProcessingConfig(
            adapter=str(_req(proc, "adapter", "processing")),
            chunk_seconds=float(proc.get("chunk_seconds", 600.0)),
            seed=int(proc.get("seed", 0)),
        ),
        outputs=OutputsConfig(
            root=str(outs.get("root", "./processed")),
            write_clean_signals=bool(outs.get("write_clean_signals", True)),
            write_features=bool(outs.get("write_features", True)),
        ),
        website=WebsiteConfig(
            feed_dir=str(web.get("feed_dir", "./website")),
            figures=[str(x) for x in (web.get("figures") or [])],
        ),
        notes=dict(raw.get("notes") or {}),
    )

    if cfg.sampling_rate_hz <= 0:
        raise ConfigError(f"[{cfg.name}] sampling_rate_hz must be > 0")
    if cfg.processing.chunk_seconds <= 0:
        raise ConfigError(f"[{cfg.name}] processing.chunk_seconds must be > 0")
    if ":" not in cfg.processing.adapter:
        raise ConfigError(f"[{cfg.name}] processing.adapter must be 'module:function'")

    return cfg


def load_all_projects(projects_dir: str | Path = "projects") -> list[ProjectConfig]:
    """Load every ``*.yaml`` in ``projects/`` except ``_template.yaml``."""
    out = []
    for p in sorted(Path(projects_dir).glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        out.append(load_project(p))
    return out
