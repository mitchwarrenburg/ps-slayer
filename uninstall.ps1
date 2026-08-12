<#
    Removes the ps-slayer scheduled task, shortcuts and Installed-apps entry,
    and stops the running process.

    Leaves config.toml, logs and .venv alone -- delete the folder yourself if
    you want it all gone.
#>
[CmdletBinding()]
param([switch]$Quiet)

$ErrorActionPreference = 'Stop'
$TaskName = 'ps-slayer'
$AppName = 'ps-slayer'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# only removing the scheduled task needs admin
$taskExists = $null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
if ($taskExists -and -not $isAdmin) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Quiet) { $argList += '-Quiet' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    exit
}

# --- stop it ---------------------------------------------------------------
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*ps_slayer.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- scheduled task --------------------------------------------------------
if ($taskExists) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
} else {
    Write-Host "No scheduled task named '$TaskName'." -ForegroundColor Yellow
}

# --- shortcuts -------------------------------------------------------------
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
if (Test-Path $StartMenu) {
    Remove-Item $StartMenu -Recurse -Force
    Write-Host "Removed Start Menu folder." -ForegroundColor Green
}
$desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
if (Test-Path $desktopLnk) {
    Remove-Item $desktopLnk -Force
    Write-Host "Removed desktop shortcut." -ForegroundColor Green
}

# --- Settings -> Installed apps --------------------------------------------
$uninstallKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
if (Test-Path $uninstallKey) {
    Remove-Item $uninstallKey -Recurse -Force
    Write-Host "Removed Installed-apps entry." -ForegroundColor Green
}

Write-Host "Done. Config, logs and .venv left in $Root" -ForegroundColor Green
if (-not $Quiet) { Read-Host "Press Enter to close" }
