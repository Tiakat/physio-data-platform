#!/bin/bash
#SBATCH --job-name=physio-ml-pipeline
#SBATCH --account=def-molo
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=/home/katia5/logs/ml-pipeline-%j.log
#SBATCH --error=/home/katia5/logs/ml-pipeline-%j.err
#SBATCH --mail-type=FAIL
#SBATCH --mail-user=katiadjerroud2@gmail.com

# =====================================================================
# Physio ML Pipeline — Slurm batch script for Narval
#
# Runs the full pipeline on encrypted Alliance data:
#   1. Decrypt files in memory (never writes plaintext to disk)
#   2. Deterministic QC (qc_engine.py)
#   3. Cross-variable consistency (consistency.py)
#   4. XGBoost artifact detection
#   5. Saves QC report (encrypted)
#
# Usage:
#   sbatch run_pipeline.slurm <PROJECT> [PATIENT]
#   sbatch run_pipeline.slurm DEXREM           # all DEXREM patients
#   sbatch run_pipeline.slurm DEXREM "Patient 5"  # single patient
# =====================================================================

set -euo pipefail

PROJECT="${1:?Usage: sbatch run_pipeline.slurm <PROJECT> [PATIENT]}"
PATIENT="${2:-}"

echo "============================================================"
echo "Physio ML Pipeline"
echo "  Project: $PROJECT"
echo "  Patient: ${PATIENT:-(all)}"
echo "  Job: $SLURM_JOB_ID on $SLURM_NODELIST"
echo "  Started: $(date)"
echo "============================================================"

# --- Paths ---
ENCRYPTED_ROOT="/project/def-molo/katia5/dropbox_enc"
KEY_FILE="$HOME/.alliance_key"
REPO_DIR="$HOME/physio-data-platform"
OUTPUT_DIR="/project/def-molo/katia5/ml_output/${PROJECT}"
mkdir -p "$OUTPUT_DIR" "$HOME/logs"

# --- Python environment ---
# Use system Python with required packages
# If packages are missing, install to user site:
#   pip install --user xgboost scikit-learn torch pandas numpy pyyaml cryptography
module load python/3.11 2>/dev/null || true

# --- Verify key ---
if [[ ! -f "$KEY_FILE" ]]; then
    echo "ERROR: Alliance key not found at $KEY_FILE" >&2
    exit 1
fi
chmod 600 "$KEY_FILE"

# --- Find encrypted files ---
if [[ -n "$PATIENT" ]]; then
    SEARCH_DIR="${ENCRYPTED_ROOT}/${PROJECT}/Database/ExtractedData"
    echo "Searching for patient: $PATIENT"
else
    SEARCH_DIR="${ENCRYPTED_ROOT}/${PROJECT}/Database/ExtractedData"
fi

if [[ ! -d "$SEARCH_DIR" ]]; then
    echo "ERROR: Directory not found: $SEARCH_DIR" >&2
    echo "Available projects:"
    ls "$ENCRYPTED_ROOT/"
    exit 1
fi

# --- Python environment ---
# Activate the ML pipeline virtual environment
VENV_DIR="$HOME/ml-pipeline-venv"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
    echo "Activated venv: $VENV_DIR"
else
    echo "WARNING: venv not found at $VENV_DIR, using system python3"
fi

echo "Input: $SEARCH_DIR"
echo "Output: $OUTPUT_DIR"

# --- Run pipeline ---
# The Python script handles decryption in memory using the key file.
# Plaintext is NEVER written to disk.

export ALLIANCE_KEY_FILE="$KEY_FILE"
export PYTHONPATH="$REPO_DIR/alliance:$PYTHONPATH"

python3 "$REPO_DIR/alliance/run_on_alliance.py" \
    --input-dir "$SEARCH_DIR" \
    --output-dir "$OUTPUT_DIR" \
    --project "$PROJECT" \
    ${PATIENT:+--patient "$PATIENT"} \
    --model "$REPO_DIR/alliance/models/qc_xgb_12var.json"

echo ""
echo "============================================================"
echo "Pipeline complete: $(date)"
echo "Output: $OUTPUT_DIR"
echo "============================================================"
