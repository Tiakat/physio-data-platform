# Azure setup for the lab research data platform.
# Run in PowerShell, once. Roughly 15 minutes including provisioning waits.
#
# Prerequisites
#   winget install Microsoft.AzureCLI
#   az login          <- opens a browser, sign in with the lab account
#
# Nothing here needs hospital IT. The lab owns the subscription.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- settings
$RG        = "rg-labdata"
$LOC       = "canadacentral"          # Canadian data residency
$STORAGE   = "stlabdata$(Get-Random -Minimum 1000 -Maximum 9999)"  # must be globally unique
$PGNAME    = "pg-labdata"
$VMNAME    = "vm-labdata"
$VMSIZE    = "Standard_B2ms"          # 2 vCPU, 8 GB. Resize later in 5 minutes.
$PGADMIN   = "labadmin"
$BUDGET    = 300                      # CAD per month, alert threshold

Write-Host "Storage account name will be: $STORAGE" -ForegroundColor Cyan
Write-Host "Write this down. It is needed in .env" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. group
az group create --name $RG --location $LOC

# ---------------------------------------------------------------- 2. storage
az storage account create `
  --name $STORAGE --resource-group $RG --location $LOC `
  --sku Standard_LRS --kind StorageV2 `
  --min-tls-version TLS1_2 `
  --allow-blob-public-access false `
  --https-only true

# One container per lifecycle stage. rawdata is immutable by policy.
foreach ($c in @("incoming","rawdata","standardised","processed","reports")) {
  az storage container create --name $c --account-name $STORAGE --auth-mode login
}

# Move raw files older than 180 days to cool tier automatically.
$rule = @'
{"rules":[{"enabled":true,"name":"archive-raw","type":"Lifecycle",
"definition":{"actions":{"baseBlob":{"tierToCool":{"daysAfterModificationGreaterThan":180}}},
"filters":{"blobTypes":["blockBlob"],"prefixMatch":["rawdata/"]}}}]}
'@
$rule | Out-File -Encoding ascii lifecycle.json
az storage account management-policy create --account-name $STORAGE --resource-group $RG --policy `@lifecycle.json
Remove-Item lifecycle.json

# ---------------------------------------------------------------- 3. database
# Burstable tier, automatic backups, NOT reachable from the internet.
az postgres flexible-server create `
  --name $PGNAME --resource-group $RG --location $LOC `
  --tier Burstable --sku-name Standard_B1ms `
  --storage-size 32 --version 16 `
  --backup-retention 7 `
  --admin-user $PGADMIN `
  --public-access None `
  --yes

# ---------------------------------------------------------------- 4. compute
az vm create `
  --name $VMNAME --resource-group $RG --location $LOC `
  --image Ubuntu2204 --size $VMSIZE `
  --admin-username labuser --generate-ssh-keys `
  --public-ip-sku Standard `
  --os-disk-size-gb 128

# Only HTTPS from anywhere. SSH restricted to the current public address.
$myip = (Invoke-RestMethod https://api.ipify.org)
az network nsg rule create --resource-group $RG --nsg-name "${VMNAME}NSG" `
  --name allow-https --priority 100 --access Allow --protocol Tcp `
  --destination-port-ranges 443 --source-address-prefixes "*"
az network nsg rule create --resource-group $RG --nsg-name "${VMNAME}NSG" `
  --name allow-ssh-me --priority 110 --access Allow --protocol Tcp `
  --destination-port-ranges 22 --source-address-prefixes "$myip/32"

# ---------------------------------------------------------------- 5. budget alert
$sub = az account show --query id -o tsv
az consumption budget create `
  --budget-name "labdata-monthly" --amount $BUDGET `
  --category Cost --time-grain Monthly `
  --start-date (Get-Date -Format "yyyy-MM-01") `
  --end-date (Get-Date).AddYears(3).ToString("yyyy-MM-01") `
  --resource-group $RG 2>$null

# ---------------------------------------------------------------- 6. output
$ip = az vm show -d -g $RG -n $VMNAME --query publicIps -o tsv
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Storage account : $STORAGE"
Write-Host "  Database        : $PGNAME.postgres.database.azure.com"
Write-Host "  VM address      : $ip"
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Put the storage account name into .env"
Write-Host "  2. ssh labuser@$ip"
Write-Host "  3. On the VM: sudo apt update; sudo apt install -y docker.io docker-compose-plugin git"
Write-Host "  4. git clone <repo>; cd labdata; docker compose up -d"
Write-Host ""
Write-Host "To generate a write-only upload key for the extraction company, run:"
Write-Host "  .\azure\company_key.ps1 -Storage $STORAGE -ResourceGroup $RG"
