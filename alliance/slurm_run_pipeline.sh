#!/bin/bash
#
# slurm_run_pipeline.sh — template Slurm job for the physio pipeline on Alliance.
# Copy, fill in the TODOs, submit with:  sbatch slurm_run_pipeline.sh
#
#SBATCH --account=def-molo          # boss's PI group (matches RAPI def-molo)
#SBATCH --job-name=physio-pipeline
#SBATCH --time=06:00:00             # adjust to the phase you run
#SBATCH --mem=32G                   # adjust to the phase you run
#SBATCH --cpus-per-task=8
#SBATCH --output=logs/pipeline_%j.out
#SBATCH --error=logs/pipeline_%j.err

set -euo pipefail

# TODO: pick the cluster you have access to and its Python module, e.g.:
#   Narval:   module load python/3.11
#   Beluga:   module load python/3.11
module load python/3.11

# TODO: point at the repo clone on /project (git clone once, then git pull)
REPO="${REPO:-$HOME/repos/physio-data-platform}"
cd "$REPO"

# Secrets are passed as env vars, NEVER written into this file.
# Needed: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN,
#         PIPELINE_DATA_KEY (Fernet key — decrypts nothing new, only reads
#         what the pipeline already encrypted).
: "${PIPELINE_DATA_KEY:?Export PIPELINE_DATA_KEY before submitting}"

mkdir -p logs

python -m venv --upgrade venv 2>/dev/null || true
source venv/bin/activate
pip install -q -r requirements.txt

# TODO: pick the phase to run, e.g.:
#   python tools/ingest_ett.py --all
#   python tools/signal_processing.py --project DEXREM
#   python tools/supervisor.py
python tools/supervisor.py
