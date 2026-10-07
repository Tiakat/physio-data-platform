#!/usr/bin/env bash
#
# transfer_encrypted_blobs.sh
# Copies ENCRYPTED Azure blobs (rawdata/, processed/, reports/) to Alliance
# /project storage. Never decrypts anything: ciphertext in, ciphertext out.
# Blob names carry no patient codes, so the layout is safe to move as-is.
#
# Requirements on the machine you run this from: rclone >= 1.60.
# (On Alliance clusters rclone is shipped under /cvmfs — `which rclone`.)
#
# Usage — run from an Alliance login node (fastest path into /project):
#   export AZURE_ACCOUNT="labdataplatform"
#   export AZURE_KEY="<storage account key>"   # never commit this anywhere
#   export ALLIANCE_PROJECT_DIR="/project/def-molo/katia5/physio"
#   DRY_RUN=1 ./transfer_encrypted_blobs.sh            # preview (default)
#   DRY_RUN=0 ./transfer_encrypted_blobs.sh            # real transfer
#
# Getting AZURE_KEY (pick one):
#   - Windows PowerShell: az storage account keys list \
#       --account-name labdataplatform --query "[0].value" -o tsv
#   - Azure portal: labdataplatform -> Access keys -> key1 -> Show
# Paste the key into the cluster terminal only. Never into chat, never into a file.
set -euo pipefail

: "${AZURE_ACCOUNT:?Set AZURE_ACCOUNT to the Azure storage account name}"
: "${AZURE_KEY:?Set AZURE_KEY to the storage account key}"
: "${ALLIANCE_PROJECT_DIR:?Set ALLIANCE_PROJECT_DIR, e.g. /project/def-molo/katia5/physio}"

DRY_RUN="${DRY_RUN:-1}"
CONTAINERS="${CONTAINERS:-rawdata processed reports}"   # space-separated

RCLONE_CONF="$(mktemp)"
trap 'rm -f "$RCLONE_CONF"' EXIT
chmod 600 "$RCLONE_CONF"

cat > "$RCLONE_CONF" <<EOF
[azure]
type = azureblob
account = ${AZURE_ACCOUNT}
key = ${AZURE_KEY}
EOF

FLAGS=(--config "$RCLONE_CONF" --checksum --transfers 8 --retries 5 --stats 30s)
if [ "$DRY_RUN" = "1" ]; then
  FLAGS+=(--dry-run)
  echo "### DRY RUN — nothing will be copied. Set DRY_RUN=0 for the real thing."
fi

mkdir -p "$ALLIANCE_PROJECT_DIR"

for c in $CONTAINERS; do
  echo "### container: $c"
  rclone sync "azure:$c" "$ALLIANCE_PROJECT_DIR/$c" "${FLAGS[@]}"
done

echo "### done. Target layout:"
find "$ALLIANCE_PROJECT_DIR" -maxdepth 2 -type d | sort
