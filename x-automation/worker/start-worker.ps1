[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\x-browse-console-worker',
    [int]$RestartDelaySeconds = 10
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\acc-rpa\.venv\Scripts\python.exe'
$Worker = Join-Path $InstallDir 'worker.py'
$Config = Join-Path $InstallDir 'worker.json'
$Logs = Join-Path $InstallDir 'logs'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Worker -PathType Leaf)) { throw "Worker not found: $Worker" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location -LiteralPath $InstallDir

while ($true) {
    & $Python $Worker --config $Config
    $exitCode = $LASTEXITCODE
    Add-Content -LiteralPath (Join-Path $Logs 'launcher.log') -Encoding UTF8 -Value "$(Get-Date -Format o) worker exited code=$exitCode; restarting in $RestartDelaySeconds seconds"
    Start-Sleep -Seconds $RestartDelaySeconds
}
