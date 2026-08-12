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
    $_ -and ($_.Major -eq 3) -and ($_.Minor -ge 10) -and ($_.Minor -le 14)
} | Sort-Object Major, Minor, Micro -Descending

if (-not $validCandidates) {
    Write-Host "Error: Neither 'python' nor 'python3' provides a lock-supported Python (3.10-3.14). Found python=$foundPython, python3=$foundPython3." -ForegroundColor Red
    exit 1
}

$selectedPython = $validCandidates[0]
$pyExe = $selectedPython.Path
Write-Host "Python: $($selectedPython.Name) ($($selectedPython.Version))"

# Stop and remove the live task before clearing its interpreter. Polling makes
# Stop-ScheduledTask's asynchronous completion explicit and bounded.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing task before dependency update..."
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        throw "Could not stop the existing Robyx task; the venv was not modified. Run Stop-ScheduledTask -TaskName '$TaskName' manually. $($_.Exception.Message)"
    }
    $stopDeadline = (Get-Date).AddSeconds(30)
    do {
        $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
        if ($state -ne "Running") {
            break
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $stopDeadline)
    if ($state -eq "Running") {
        throw "$TaskName did not stop within 30 seconds; the existing venv was not modified. Stop it manually with Stop-ScheduledTask -TaskName '$TaskName'."
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
}

# Create venv
Write-Host "Creating virtual environment..."
& $pyExe -m venv --clear "$ProjectRoot\.venv"
$venvPython = "$ProjectRoot\.venv\Scripts\python.exe"

# Install deps
Write-Host "Installing dependencies..."
$runtimeLock = & $venvPython "$ProjectRoot\bot\dependency_locks.py" `
    --project-root $ProjectRoot --kind runtime
if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve the runtime dependency lock"
}
& $venvPython -m pip install -q --require-hashes -r $runtimeLock.Trim()

# Run setup if no .env
if (-not (Test-Path "$ProjectRoot\.env")) {
    Write-Host ""
    Write-Host "No .env found - running setup wizard..."
    & $venvPython "$ProjectRoot\setup.py"
}

# Python's stdlib cannot express an equivalent Windows ACL. The shared
# hardener is therefore a documented no-op on Windows, while atomic writes and
# the current-user Task Scheduler boundary remain intact.
& $venvPython "$ProjectRoot\bot\local_security.py" --project-root $ProjectRoot

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
