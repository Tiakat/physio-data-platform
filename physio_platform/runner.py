"""Runner: execute one project's pipeline over all staged patients.

For every staged raw file the runner calls the project's adapter
(``process_patient(raw_path, cfg, out_dir, log) -> dict``). Failures are
isolated per patient: one bad file never stops the rest, and every run
writes ``run_manifest.json`` with per-patient status, timings, the config
fingerprint and the code version, so any result can be traced back to the
exact code + config that produced it.
"""
from __future__ import annotations

import importlib
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import ProjectConfig
from .ingest import StagedFile

log = logging.getLogger(__name__)

RUN_MANIFEST = "run_manifest.json"


def load_adapter(spec: str) -> Callable:
    """Import ``module:function`` and return the function."""
    mod_name, _, func_name = spec.partition(":")
    if not mod_name or not func_name:
        raise ValueError(f"adapter spec must be 'module:function', got {spec!r}")
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        raise ValueError(f"adapter {spec!r} is not a callable")
    return fn


def _code_version() -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    try:
        from . import __version__
        return f"v{__version__}"
    except Exception:
        return "unknown"


def _patient_id(staged: StagedFile) -> str:
    # default: file stem; projects can override via notes.patient_id_from
    return Path(staged.local_path).stem


def run_project(cfg: ProjectConfig, staged: list[StagedFile],
                out_root: str | Path | None = None) -> dict:
    """Process every staged file. Returns the run manifest dict."""
    out_root = Path(out_root or cfg.outputs.root)
    adapter = load_adapter(cfg.processing.adapter)
    version = _code_version()
    started = datetime.now(timezone.utc)

    manifest = {
        "project": cfg.name,
        "config_fingerprint": cfg.fingerprint,
        "code_version": version,
        "seed": cfg.processing.seed,
        "started_at": started.isoformat(),
        "patients": {},
    }

    n_ok = n_failed = 0
    for sf in staged:
        pid = _patient_id(sf)
        p_out = out_root / cfg.name / cfg.fingerprint / pid
        p_out.mkdir(parents=True, exist_ok=True)
        p_log = logging.getLogger(f"{__name__}.{cfg.name}.{pid}")
        t0 = datetime.now(timezone.utc)
        rec: dict = {"dropbox_path": sf.dropbox_path,
                     "local_path": sf.local_path,
                     "started_at": t0.isoformat()}
        try:
            result = adapter(sf.local_path, cfg, str(p_out), p_log) or {}
            rec.update(result)
            rec["status"] = "ok"
            n_ok += 1
            p_log.info("patient %s ok", pid)
        except Exception as e:  # noqa: BLE001 - isolate failures per patient
            rec["status"] = "failed"
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["traceback"] = traceback.format_exc(limit=8)
            n_failed += 1
            p_log.error("patient %s failed: %s", pid, e)
        rec["finished_at"] = datetime.now(timezone.utc).isoformat()
        (p_out / "patient_log.json").write_text(json.dumps(rec, indent=2))
        manifest["patients"][pid] = {
            k: v for k, v in rec.items() if k != "traceback"}

    manifest.update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "n_ok": n_ok,
        "n_failed": n_failed,
        "n_total": len(staged),
    })
    mpath = out_root / cfg.name / cfg.fingerprint / RUN_MANIFEST
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2))
    log.info("run done: %d ok, %d failed, %d total (manifest: %s)",
             n_ok, n_failed, len(staged), mpath)
    return manifest
