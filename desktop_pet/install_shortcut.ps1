param(
    [switch]$EnableStartup = $true,
    [switch]$CreateDesktopShortcut = $true
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
# "启动哈基米桌宠.cmd" constructed via Unicode code points so PS 5.1 without BOM never mangles it
$cmdName = -join ([char[]](0x542F, 0x52A8, 0x54C8, 0x57FA, 0x7C73, 0x684C, 0x5BA0)) + '.cmd'
$lnkTitle = -join ([char[]](0x54C8, 0x57FA, 0x7C73, 0x684C, 0x5BA0)) + '.lnk'
$launcher = Join-Path $repoRoot $cmdName
$icon = Join-Path $PSScriptRoot 'assets\hakimi_pet.ico'
$ws = New-Object -ComObject WScript.Shell

if ($CreateDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $desktopLnk = Join-Path $desktop $lnkTitle
    $s = $ws.CreateShortcut($desktopLnk)
    $s.TargetPath = $launcher
    $s.WorkingDirectory = $repoRoot
    $s.WindowStyle = 7
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Description = 'Hakimi Q-Version Desktop Pet (Gemini 3.8 Flash)'
    $s.Save()
    Write-Host "Desktop shortcut created: $desktopLnk" -ForegroundColor Green
}

if ($EnableStartup) {
    $startup = [Environment]::GetFolderPath('Startup')
    $startupLnk = Join-Path $startup 'HakimiDesktopPet.lnk'
    $s2 = $ws.CreateShortcut($startupLnk)
    $s2.TargetPath = $launcher
    $s2.WorkingDirectory = $repoRoot
    $s2.WindowStyle = 7
    if (Test-Path $icon) { $s2.IconLocation = $icon }
    $s2.Description = 'Hakimi Q-Version Desktop Pet AutoStart'
    $s2.Save()
    Write-Host "Startup shortcut enabled: $startupLnk" -ForegroundColor Cyan
}
