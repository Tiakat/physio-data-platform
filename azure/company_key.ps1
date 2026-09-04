# Generate a WRITE ONLY upload key for the extraction company.
#
# The key allows create and write into the incoming container only.
# It does NOT allow read, list or delete. If the key leaks, nobody can read or
# destroy any data; they can only add files. This is the single most important
# security decision in the transfer design.
#
# This replaces native SFTP on blob storage, which costs about 220 CAD per month
# whether or not anything is transferred. This costs nothing.
#
#   .\company_key.ps1 -Storage stlabdata1234 -ResourceGroup rg-labdata

param(
    [Parameter(Mandatory=$true)][string]$Storage,
    [Parameter(Mandatory=$true)][string]$ResourceGroup,
    [int]$ValidMonths = 12
)

$expiry = (Get-Date).AddMonths($ValidMonths).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

$sas = az storage container generate-sas `
  --account-name $Storage `
  --name incoming `
  --permissions cw `        # c = create, w = write. No r, no l, no d.
  --expiry $expiry `
  --https-only `
  --auth-mode login --as-user `
  -o tsv

$url = "https://$Storage.blob.core.windows.net/incoming?$sas"

Write-Host ""
Write-Host "Upload URL, valid until $expiry" -ForegroundColor Green
Write-Host $url
Write-Host ""
Write-Host "Send this to the extraction company with these instructions:" -ForegroundColor Cyan
Write-Host @"

  Upload each file with AzCopy:

      azcopy copy "<local file>" "<the URL above, with the filename inserted>"

  Or with any HTTPS client, PUT to:

      https://$Storage.blob.core.windows.net/incoming/<filename>?<sas token>
      x-ms-blob-type: BlockBlob

  Please also send, once per day, a manifest file named
      manifest_YYYYMMDD.json
  listing every file sent that day with its SHA-256 checksum, so that a missing
  transfer can be detected rather than assumed absent.

"@
Write-Host "Set a calendar reminder to reissue this key before $expiry" -ForegroundColor Yellow
