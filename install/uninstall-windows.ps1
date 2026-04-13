# Robyx — Windows uninstaller
$TaskName = "Robyx"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "=== Robyx Windows Uninstaller ==="
Write-Host ""

# Stop and remove scheduled task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task removed."
} else {
    Write-Host "Scheduled task not found (already removed)."
}

# Kill any lingering bot process
$procs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "bot\.py"
}
if ($procs) {
    Write-Host "Stopping lingering bot process..."
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
}

# Clean runtime state
$dataDir = Join-Path $ProjectRoot "data"
if (Test-Path $dataDir) {
    Write-Host "Cleaning runtime state..."
    Get-ChildItem -Path $dataDir -Recurse -Filter "lock" | Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $dataDir -Recurse -Filter "output.log" | Remove-Item -Force -ErrorAction SilentlyContinue
}

# Clean log
Remove-Item -Path (Join-Path $ProjectRoot "bot.log") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $ProjectRoot "bot.log.*") -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "=== Robyx uninstalled ==="
Write-Host ""
Write-Host "Service stopped and removed from startup."
Write-Host "Project files are untouched. Remove $ProjectRoot manually if desired."
Write-Host ""
