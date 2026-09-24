# Registers a Windows scheduled task that keeps Dropbox organised automatically:
# it starts at logon and runs `python -m physio.organize watch`, which waits for
# Dropbox changes and moves/renames new files into the template within ~1 minute.
#   .\scripts\install_organizer_task.ps1            # install
#   .\scripts\install_organizer_task.ps1 -Remove    # uninstall
param([switch]$Remove)
$name = "Lab Dropbox organizer"
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; exit }

$repo   = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$log    = Join-Path $repo "data\_organize\watch.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$action   = New-ScheduledTaskAction -Execute "cmd.exe" `
            -Argument "/c `"`"$python`" -m physio.organize watch >> `"$log`" 2>&1`"" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Installed and started '$name'. Log: $log"
