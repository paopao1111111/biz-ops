[CmdletBinding()]
param(
    [string]$SourceDir = $PSScriptRoot,
    [string]$InstallDir = 'C:\x-browse-console-worker',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$TaskName = 'X Browse Console Worker'
$Python = 'C:\acc-rpa\.venv\Scripts\python.exe'
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Required Python not found: $Python" }
foreach ($name in @('worker.py','worker.example.json','start-worker.ps1','install-worker.ps1','uninstall-worker.ps1','README.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $name) -PathType Leaf)) { throw "Build file missing: $name" }
}

if (Test-Path -LiteralPath $InstallDir) {
    $Backup = "$InstallDir.backup-$Timestamp"
    Write-Host "Existing install found. Moving it to $Backup"
    Move-Item -LiteralPath $InstallDir -Destination $Backup
}
New-Item -ItemType Directory -Force -Path $InstallDir, (Join-Path $InstallDir 'logs') | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceDir 'worker.py') -Destination $InstallDir
Copy-Item -LiteralPath (Join-Path $SourceDir 'start-worker.ps1') -Destination $InstallDir
Copy-Item -LiteralPath (Join-Path $SourceDir 'install-worker.ps1') -Destination $InstallDir
Copy-Item -LiteralPath (Join-Path $SourceDir 'uninstall-worker.ps1') -Destination $InstallDir
Copy-Item -LiteralPath (Join-Path $SourceDir 'README.md') -Destination $InstallDir
Copy-Item -LiteralPath (Join-Path $SourceDir 'worker.example.json') -Destination $InstallDir

$ConfigPath = Join-Path $InstallDir 'worker.json'
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Copy-Item -LiteralPath (Join-Path $SourceDir 'worker.example.json') -Destination $ConfigPath
}
& icacls.exe $ConfigPath /inheritance:r /grant:r "${CurrentUser}:(R,W)" 'SYSTEM:(F)' 'Administrators:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to apply restrictive ACL to worker.json' }

$StartScript = Join-Path $InstallDir 'start-worker.ps1'
$Argument = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`""
$Action = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument $Argument -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Trigger.Delay = 'PT17S'
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description 'Independent read-only X Browse Console Windows worker' -Force | Out-Null

Write-Host "Installed to $InstallDir for $CurrentUser. Edit $ConfigPath before starting."
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started scheduled task: $TaskName"
}
