#!/usr/bin/env bash
#
# daily_sync.sh — daily Alliance sync: Dropbox -> template -> encrypted.
#
# Each run:
#   1. pulls new/changed files from Dropbox Database/ folders (rclone sync,
#      idempotent — only deltas transfer),
#   2. reorganizes staging into the lab template (fix_structure.py),
#   3. encrypts into /project/def-molo/katia5/dropbox_enc/ (encrypt_tree.py,
#      skips files that are already encrypted),
#   4. deletes the plaintext staging (only on success),
#   5. resubmits itself for ~24h later (only on success).
#
# One-time setup (on a login node):
#   nano ~/.alliance_key   # paste the ALLIANCE_DATA_KEY value
#   chmod 600 ~/.alliance_key
#
# Submit the first run:
#   sbatch ~/physio-data-platform/alliance/daily_sync.sh
#
# Logs: ~/sync-<jobid>.log
#
#SBATCH --job-name=alliance-sync
#SBATCH --time=04:00:00
#SBATCH --mem=8G
#SBATCH --output=/home/katia5/sync-%j.log

set -euo pipefail

module load python/3.11
export PIPELINE_DATA_KEY="$(cat ~/.alliance_key)"

STAGE="/project/def-molo/katia5/dropbox"
FIXED_STAGE="/project/def-molo/katia5/dropbox_fixed"
ENC="/project/def-molo/katia5/dropbox_enc"
REPO="$HOME/physio-data-platform"
PY="$HOME/cryptenv/bin/python"

mkdir -p "$STAGE" "$FIXED_STAGE"

# quick connectivity check before the long pull
echo "### checking Dropbox connectivity"
rclone lsf "dropbox:Liam/Projets actifs/DEXREM/Database" --max-depth 1 \
  > /dev/null || { echo "FATAL: cannot reach Dropbox"; exit 1; }

sync_one() {  # $1 = dropbox subpath, $2 = dest subdir, rest = extra excludes
  local src="$1" dest="$2"; shift 2
  echo "### $src -> $dest"
  rclone sync "dropbox:Liam/Projets actifs/$src" "$STAGE/$dest" \
    --exclude "*.docx" --exclude "*.doc" --exclude "*.pdf" --exclude "*.txt" \
    --exclude "*.jpg" --exclude "*.jpeg" --exclude "*.png" \
    --exclude "Photos/**" \
    "$@" \
    --transfers 8 --checkers 16 --stats 60s --stats-log-level NOTICE
}

# 1. pull (xlsx/xls INCLUDED — demographic files are in scope)
sync_one "DEXREM/Database" "DEXREM"
for p in ESMONOL IPAMS MONREPI POSBRAIN PROMISES SILVR V-RAPS; do
  sync_one "$p/Database" "$p"
done
sync_one "Colectomie en ambulatoire/Database" "COLECTOMIE" \
  --exclude "Patient non inclus*/**"
sync_one "PVB abdo/Database" "PVB-ABDO"

# 2. fix structure into the lab template
echo "### fixing structure"
rm -rf "$FIXED_STAGE"
"$PY" "$REPO/alliance/fix_structure.py" "$STAGE" "$FIXED_STAGE"

# 3. encrypt new files
echo "### encrypting"
"$PY" "$REPO/alliance/encrypt_tree.py" "$FIXED_STAGE" "$ENC"

# 4. cleanup (only reached on success thanks to set -e)
echo "### cleaning staging"
rm -rf "$STAGE" "$FIXED_STAGE"

# 5. resubmit for tomorrow
echo "### resubmitting for tomorrow"
sbatch --begin=now+24hours "$REPO/alliance/daily_sync.sh"

echo "### DONE $(date)"
