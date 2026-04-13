# Robyx — Windows installer (Task Scheduler)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$TaskName = "Robyx"

Write-Host "=== Robyx Windows Installer ===" -ForegroundColor Cyan
Write-Host ""

# Check Administrator privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Error: This script requires Administrator privileges." -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'." -ForegroundColor Yellow
    exit 1
}

# Pick the newest available Python >= 3.10 from python/python3
function Get-PythonCandidate($CommandName) {
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return $null
    }

    try {
        $version = & $cmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
        if (-not $version) {
            return $null
        }
        $parts = $version.Trim().Split('.')
        if ($parts.Length -lt 3) {
            return $null
        }

        [PSCustomObject]@{
            Name = $CommandName
            Path = $cmd.Source
            Version = $version.Trim()
            Major = [int]$parts[0]
            Minor = [int]$parts[1]
            Micro = [int]$parts[2]
        }
    }
    catch {
        return $null
    }
}

$python = Get-PythonCandidate "python"
$python3 = Get-PythonCandidate "python3"

$foundPython = if ($python) { $python.Version } else { "not found" }
$foundPython3 = if ($python3) { $python3.Version } else { "not found" }

$validCandidates = @($python, $python3) | Where-Object {
    $_ -and (($_.Major -gt 3) -or (($_.Major -eq 3) -and ($_.Minor -ge 10)))
} | Sort-Object Major, Minor, Micro -Descending

if (-not $validCandidates) {
    Write-Host "Error: Neither 'python' nor 'python3' provides Python 3.10+. Found python=$foundPython, python3=$foundPython3." -ForegroundColor Red
    exit 1
}

$selectedPython = $validCandidates[0]
$pyExe = $selectedPython.Path
Write-Host "Python: $($selectedPython.Name) ($($selectedPython.Version))"

# Create venv
Write-Host "Creating virtual environment..."
& $pyExe -m venv --clear "$ProjectRoot\.venv"
$venvPython = "$ProjectRoot\.venv\Scripts\python.exe"

# Install deps
Write-Host "Installing dependencies..."
& $venvPython -m pip install -q -r "$ProjectRoot\bot\requirements.txt"

# Run setup if no .env
if (-not (Test-Path "$ProjectRoot\.env")) {
    Write-Host ""
    Write-Host "No .env found - running setup wizard..."
    & $venvPython "$ProjectRoot\setup.py"
}

# Create data dirs
New-Item -ItemType Directory -Force -Path "$ProjectRoot\data\system-monitor" | Out-Null

# Remove existing task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing scheduled task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create scheduled task
Write-Host "Creating scheduled task..."
$action = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "$ProjectRoot\bot\bot.py" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Robyx AI Agent Orchestrator"

# Start the task
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "=== Robyx installed ===" -ForegroundColor Green
Write-Host ""
Write-Host "Task:   $TaskName"
Write-Host "Status: Get-ScheduledTask -TaskName $TaskName"
Write-Host "Stop:   Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Start:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:   Get-Content $ProjectRoot\bot.log -Wait"
Write-Host ""
