# Dropbox -> Azure migration, phases 1-3.  Run from the repo root in the venv:
#   .\.venv\Scripts\Activate.ps1
#   .\scripts\run_migration.ps1            # inventory + reconcile + DRY RUN
#   .\scripts\run_migration.ps1 -Upload    # ... then really upload the MISSING files
param([switch]$Upload, [string]$Project = "")
$ErrorActionPreference = "Stop"
$p = @(); if ($Project) { $p = @("--project", $Project) }

python -m physio.inventory_dropbox @p
python -m physio.inventory_azure
python -m physio.reconcile
Get-Content -Encoding UTF8 reports\phase2\reconciliation_summary.md

if ($Upload) {
    python -m physio.sync @p
    python -m physio.sync @p --retry-failed
    # refresh the picture after uploading
    python -m physio.inventory_azure
    python -m physio.reconcile
    Get-Content -Encoding UTF8 reports\phase2\reconciliation_summary.md
} else {
    python -m physio.sync @p --dry-run
    Write-Host "`nDry run only. Re-run with -Upload to upload the MISSING files."
}
