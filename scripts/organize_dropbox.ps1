# Organise Dropbox studies into the lab template (config/template.yaml).
#   .\scripts\organize_dropbox.ps1                         # PLAN only, all studies (nothing moves)
#   .\scripts\organize_dropbox.ps1 -Study "PVB abdo"       # plan one study
#   .\scripts\organize_dropbox.ps1 -Study "PVB abdo" -Apply  # move that study's files
#   .\scripts\organize_dropbox.ps1 -Apply                  # move everything
#   .\scripts\organize_dropbox.ps1 -Undo 20260924-101500-ab12
param([string]$Study = "", [switch]$Apply, [switch]$Skeleton, [string]$Undo = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$s = @(); if ($Study) { $s = @("--study", $Study) }

if ($Undo)     { python -m physio.organize undo --run $Undo; exit }
if ($Skeleton) { python -m physio.organize skeleton @s }
if ($Apply)    { python -m physio.organize apply @s }
else           { python -m physio.organize plan @s
                 Write-Host "`nPlan only. Check reports\organize\plan.csv and unsure.csv, then add -Apply." }
Get-Content -Encoding UTF8 reports\organize\summary.md
