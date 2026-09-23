# Phase 4: move legacy Azure layout -> PROJECT/Database/<Layer>/<subject>/<source>/...
#   .\scripts\run_restructure.ps1                 # plan only (read-only)
#   .\scripts\run_restructure.ps1 -Copy           # server-side copy + verify (old blobs kept)
#   .\scripts\run_restructure.ps1 -DeleteLegacy   # delete old blobs whose copy is VERIFIED
param([switch]$Copy, [switch]$DeleteLegacy, [string]$Project = "")
$ErrorActionPreference = "Stop"
$p = @(); if ($Project) { $p = @("--project", $Project) }

python -m physio.inventory_azure
python -m physio.restructure plan
Get-Content -Encoding UTF8 reports\phase4\restructure_summary.md

if ($Copy) {
    python -m physio.restructure copy @p
    python -m physio.restructure verify @p
}
if ($DeleteLegacy) {
    python -m physio.restructure verify @p
    python -m physio.restructure delete-legacy --confirm @p
    python -m physio.inventory_azure
}
