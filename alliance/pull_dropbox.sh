#!/usr/bin/env bash
#
# pull_dropbox.sh — pull per-project DATA folders from Dropbox to Alliance.
#
# Reads the data roots from the pipeline's own config/projects.yaml knowledge:
#   DEXREM -> Included patients ; 8 projects -> Database/RawData ;
#   COLECTOMIE -> Database ; PVB-ABDO -> Database.
# Skips Documents and non-data files (spreadsheets, docs, images, Photos/).
#
# Destination: /project/def-molo/katia5/dropbox/<PROJECT>/
# This is the PLAINTEXT staging area. After the pull finishes, run
#   alliance/encrypt_tree.py  to produce the encrypted copy, then delete this.
#
# ~1 TB total — run it in the background:
#   nohup ./alliance/pull_dropbox.sh > ~/pull_dropbox.log 2>&1 &
#   tail -f ~/pull_dropbox.log
set -u

DEST="/project/def-molo/katia5/dropbox"
SRC="dropbox:Liam/Projets actifs"
mkdir -p "$DEST"

sync_one() {  # $1 = dropbox subpath, $2 = dest subdir, rest = extra rclone excludes
  local src="$1" dest="$2"; shift 2
  echo "### $src -> $dest"
  rclone sync "$SRC/$src" "$DEST/$dest" \
    --exclude "*.xlsx" --exclude "*.xls" \
    --exclude "*.docx" --exclude "*.doc" --exclude "*.pdf" --exclude "*.txt" \
    --exclude "*.jpg" --exclude "*.jpeg" --exclude "*.png" \
    --exclude "Photos/**" \
    "$@" \
    --transfers 8 --checkers 16 --stats 60s --stats-log-level NOTICE
}

sync_one "DEXREM/Included patients" "DEXREM"
for p in ESMONOL IPAMS MONREPI POSBRAIN PROMISES SILVR V-RAPS; do
  sync_one "$p/Database/RawData" "$p"
done
sync_one "Colectomie en ambulatoire/Database" "COLECTOMIE" --exclude "Patient non inclus*/**"
sync_one "PVB abdo/Database" "PVB-ABDO"

echo "### ALL DONE"
