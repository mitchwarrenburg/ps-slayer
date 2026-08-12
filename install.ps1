<#
    ps-slayer installer.

      * creates a private virtualenv and installs dependencies
      * builds ps-slayer.ico
      * registers a Task Scheduler job that starts ps-slayer at logon, elevated,
        with no console window                                     (needs admin)
      * adds Start Menu + desktop shortcuts that launch it elevated with no UAC
        prompt, by asking the scheduler to run the task
      * registers an entry in Settings -> Installed apps

    Usage:
      powershell -ExecutionPolicy Bypass -File .\install.ps1
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoTask   # no autostart,
                                                                       # no admin needed
#>
[CmdletBinding()]
param(
    [switch]$NoTask,        # skip the scheduled task (and the elevation prompt)
    [switch]$NoDesktop      # skip the desktop shortcut
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ps-slayer'
$AppName  = 'ps-slayer'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Only the scheduled task needs admin. Everything else is per-user, so -NoTask
# installs happily without a UAC prompt.
if (-not $isAdmin -and -not $NoTask) {
    Write-Host "Elevating to register the startup task..." -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($NoDesktop) { $argList += '-NoDesktop' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    exit
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Venv = Join-Path $Root '.venv'
$Script = Join-Path $Root 'ps_slayer.py'
$Ico = Join-Path $Root 'ps-slayer.ico'
$Config = Join-Path $Root 'config.toml'
$LogFile = Join-Path $Root 'logs\ps-slayer.log'
Set-Location $Root

Write-Host ""
Write-Host "$AppName installer" -ForegroundColor Cyan
Write-Host "  folder: $Root"
Write-Host ""

# --- python ----------------------------------------------------------------
$python = $null
foreach ($candidate in @('py', 'python')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $probe = if ($candidate -eq 'py') { @('-3', '--version') } else { @('--version') }
        $ver = & $cmd.Source @probe 2>&1
        if ($ver -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 11) {
            $python = @{
                Exe  = $cmd.Source
                Args = if ($candidate -eq 'py') { @('-3') } else { @() }
            }
            Write-Host "  found $ver" -ForegroundColor Green
            break
        }
    }
}
if (-not $python) { throw "Python 3.11+ not found on PATH. Install it from python.org, then re-run." }

# --- venv + deps -----------------------------------------------------------
if (-not (Test-Path (Join-Path $Venv 'Scripts\pythonw.exe'))) {
    Write-Host "  creating virtualenv..." -ForegroundColor Gray
    & $python.Exe @($python.Args + @('-m', 'venv', $Venv))
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvPyW = Join-Path $Venv 'Scripts\pythonw.exe'

Write-Host "  installing dependencies (pygame, pystray, pillow)..." -ForegroundColor Gray
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install --upgrade pygame pystray pillow --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# --- sanity check ----------------------------------------------------------
Write-Host "  validating config..." -ForegroundColor Gray
& $VenvPy $Script --check | Out-Null
if ($LASTEXITCODE -ne 0) {
    & $VenvPy $Script --check
    throw "config.toml has errors -- fix them and re-run"
}

Write-Host "  building icon..." -ForegroundColor Gray
& $VenvPy $Script --ico | Out-Null

# --- scheduled task --------------------------------------------------------
if ($NoTask) {
    Write-Host "  skipping scheduled task (-NoTask)" -ForegroundColor Yellow
} else {
    Write-Host "  registering scheduled task '$TaskName'..." -ForegroundColor Gray

    $action = New-ScheduledTaskAction -Execute $VenvPyW `
                                      -Argument "`"$Script`"" `
                                      -WorkingDirectory $Root

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = 'PT15S'   # let the wheel enumerate before we start watching

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                            -LogonType InteractiveToken `
                                            -RunLevel Highest

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $settings.DisallowStartIfOnBatteries = $false
    $settings.StopIfGoingOnBatteries = $false

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                           -Principal $principal -Settings $settings `
                           -Description 'Instant process killer bound to sim wheel buttons' `
                           -Force | Out-Null
}

# --- shortcuts -------------------------------------------------------------
function New-Shortcut {
    param($Path, $Target, $Arguments, $WorkDir, $Icon, $Description, [int]$WindowStyle = 1)
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($Path)
    $sc.TargetPath = $Target
    if ($Arguments) { $sc.Arguments = $Arguments }
    if ($WorkDir) { $sc.WorkingDirectory = $WorkDir }
    if ($Icon) { $sc.IconLocation = $Icon }
    if ($Description) { $sc.Description = $Description }
    $sc.WindowStyle = $WindowStyle
    $sc.Save()
}

$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
New-Item -ItemType Directory -Path $StartMenu -Force | Out-Null
Write-Host "  creating shortcuts..." -ForegroundColor Gray

$schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'

if ($NoTask) {
    # no task to trigger, so launch the interpreter directly (runs unelevated)
    New-Shortcut -Path (Join-Path $StartMenu "$AppName.lnk") -Target $VenvPyW `
        -Arguments "`"$Script`"" -WorkDir $Root -Icon $Ico `
        -Description 'Start ps-slayer'
} else {
    # Ask the scheduler to run the task: it is registered RunLevel Highest, so this
    # starts ps-slayer elevated with no UAC prompt. Minimised to hide the console blip.
    New-Shortcut -Path (Join-Path $StartMenu "$AppName.lnk") -Target $schtasks `
        -Arguments "/run /tn `"$TaskName`"" -WorkDir $Root -Icon $Ico `
        -Description 'Start ps-slayer (elevated)' -WindowStyle 7
    New-Shortcut -Path (Join-Path $StartMenu "Stop $AppName.lnk") -Target $schtasks `
        -Arguments "/end /tn `"$TaskName`"" -WorkDir $Root -Icon $Ico `
        -Description 'Stop ps-slayer' -WindowStyle 7
}

New-Shortcut -Path (Join-Path $StartMenu 'Edit config.lnk') -Target $Config `
    -WorkDir $Root -Icon $Ico -Description 'Edit mappings (config.toml)'
New-Shortcut -Path (Join-Path $StartMenu 'Find wheel buttons.lnk') -Target $VenvPy `
    -Arguments "`"$Script`" --learn" -WorkDir $Root -Icon $Ico `
    -Description 'Press wheel buttons to see their numbers'
New-Shortcut -Path (Join-Path $StartMenu 'View log.lnk') -Target $LogFile `
    -WorkDir $Root -Icon $Ico -Description 'ps-slayer log'

if (-not $NoDesktop) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    Copy-Item (Join-Path $StartMenu "$AppName.lnk") (Join-Path $desktop "$AppName.lnk") -Force
}

# --- Settings -> Installed apps --------------------------------------------
$uninstallKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
New-Item -Path $uninstallKey -Force | Out-Null
$sizeKb = [int]((Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object -Property Length -Sum).Sum / 1KB)
$version = (& $VenvPy $Script --version) -replace '^\S+\s+', ''
$props = @{
    DisplayName     = $AppName
    DisplayVersion  = $version
    DisplayIcon     = $Ico
    Publisher       = $env:USERNAME
    InstallLocation = $Root
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\uninstall.ps1`""
    QuietUninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\uninstall.ps1`" -Quiet"
    EstimatedSize   = $sizeKb
    NoModify        = 1
    NoRepair        = 1
}
foreach ($k in $props.Keys) {
    $type = if ($props[$k] -is [int]) { 'DWord' } else { 'String' }
    New-ItemProperty -Path $uninstallKey -Name $k -Value $props[$k] -PropertyType $type -Force | Out-Null
}

# --- start now -------------------------------------------------------------
function Get-SlayerProcesses {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*ps_slayer.py*' }
}

# Ask the app itself, via its mutex. A process scan cannot see an ELEVATED instance
# from an unelevated shell -- Win32_Process blanks out its command line and image
# path -- so scanning alone would happily report "not running" and then fail to start.
function Test-SlayerRunning {
    & $VenvPy $Script --is-running | Out-Null
    return ($LASTEXITCODE -eq 0)
}

Get-SlayerProcesses | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
# the mutex is only released once the process is fully torn down; a new instance
# started too early just exits again
$deadline = (Get-Date).AddSeconds(6)
while ((Test-SlayerRunning) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }

$blocked = Test-SlayerRunning
if ($blocked) {
    Write-Host ""
    Write-Host "  An instance is already running and could not be stopped." -ForegroundColor Yellow
    Write-Host "  It is almost certainly running elevated, which this installer cannot" -ForegroundColor Yellow
    Write-Host "  see or terminate from here. Quit it from the tray sword -> Quit, then" -ForegroundColor Yellow
    Write-Host "  re-run this installer (or run it as administrator)." -ForegroundColor Yellow
    Write-Host ""
}

if ($blocked) {
    $state = 'left alone -- another instance already holds it'
} elseif ($NoTask) {
    Start-Process -FilePath $VenvPyW -ArgumentList "`"$Script`"" -WorkingDirectory $Root
    Start-Sleep -Seconds 2
    $state = if (Test-SlayerRunning) { 'running (unelevated)' }
             else { 'FAILED TO START -- check logs\ps-slayer.log' }
} else {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    if (-not (Test-SlayerRunning)) { $state = "$state -- but not running; check logs\ps-slayer.log" }
}

Write-Host ""
Write-Host "Installed $AppName $version." -ForegroundColor Green
Write-Host "  Start Menu : $StartMenu"
Write-Host "  Task state : $state"
if ($NoTask) {
    Write-Host "  Privileges : standard user -- admin-owned processes cannot be killed" -ForegroundColor Yellow
} else {
    Write-Host "  Privileges : elevated (no UAC prompt when launched from the shortcut)"
}
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Start Menu -> $AppName -> 'Find wheel buttons', press your kill buttons"
Write-Host "  2. Start Menu -> $AppName -> 'Edit config', fill in the numbers, enabled = true"
Write-Host "  3. Tray sword -> Reload config"
Write-Host ""
Read-Host "Press Enter to close"
