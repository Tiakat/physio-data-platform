"""
ETT ingestion as a Prefect flow (Azure-side orchestration).

Mirrors flows/ingestion_flow.py's task structure:

    discover deliveries -> verify+ingest -> build feed

Runs tools/ingest_ett.py and tools/build_feed.py as subprocesses so the
parsing logic lives in one place (the CLIs), while Prefect owns retries,
logging and scheduling.

Required env: AZURE_STORAGE_ACCOUNT + AZURE_STORAGE_KEY (or a DefaultAzure
credential), PHYSIO_STORE pointing at the mounted store path, and --source
resolving to the synced ETT prefix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from prefect import flow, get_run_logger, task

REPO = Path(__file__).resolve().parent.parent
STORE = os.getenv("PHYSIO_STORE", "/mnt/physio-store")
SOURCE = os.getenv("ETT_SOURCE", "")  # e.g. Azure-mounted parquet_zip/
PROJECT = os.getenv("ETT_PROJECT", "")  # optional forced project code


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", *cmd],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=6 * 3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stderr[-2000:]}")
    return proc.stdout


@task(retries=3, retry_delay_seconds=60)
def ingest_deliveries() -> str:
    """Run the ETT ingester over all new deliveries."""
    logger = get_run_logger()
    if not SOURCE:
        raise RuntimeError("ETT_SOURCE is not configured.")
    cmd = ["tools.ingest_ett", "--source", SOURCE, "--out", STORE]
    if PROJECT:
        cmd += ["--project", PROJECT]
    out = _run(cmd)
    logger.info(out[-1500:])
    return out


@task(retries=2, retry_delay_seconds=60)
def build_website_feed() -> str:
    """Rebuild the de-identified website feed."""
    logger = get_run_logger()
    out = _run(["tools.build_feed", "--store", STORE,
                "--out", str(Path(STORE) / "reports" / "feed")])
    logger.info(out[-1500:])
    return out


@flow(name="ett-ingestion")
def ett_ingestion() -> dict:
    """Ingest new ETT deliveries, then rebuild the website feed."""
    ingest_log = ingest_deliveries()
    feed_log = build_website_feed()
    return {"ingest": ingest_log, "feed": feed_log}


if __name__ == "__main__":
    ett_ingestion()
