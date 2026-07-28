[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallDir = 'C:\x-browse-console-worker',
    [switch]$PreserveData
)

$ErrorActionPreference = 'Stop'
$TaskName = 'X Browse Console Worker'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$escaped = [Regex]::Escape((Join-Path $InstallDir 'worker.py'))
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @('python.exe','pythonw.exe','powershell.exe')) -and ($_.CommandLine -match $escaped -or $_.CommandLine -match [Regex]::Escape((Join-Path $InstallDir 'start-worker.ps1')))
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $InstallDir) {
    if ($PreserveData) {
        $Archive = "$InstallDir.preserved-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item -LiteralPath $InstallDir -Destination $Archive
        Write-Host "Worker data preserved at $Archive"
    } else {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
    }
}
Write-Host "Removed only scheduled task '$TaskName' and the independent worker installation."
