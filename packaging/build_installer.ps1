<#
    Сборка установщика НоуКо Cloud (Inno Setup, без подписи).

    .\packaging\build_installer.ps1 -Version 1.0.0

    Нужна собранная программа в dist\NoukoCloud (собирается сама, если её
    нет). ISCC.exe ищется в PATH и в обычных местах установки Inno Setup.

    Результат: release\installer\NoukoCloud-Setup-<версия>.exe
#>
param(
    [string]$Version = "",
    [switch]$SkipBuild,
    [switch]$SkipFfmpeg,
    [switch]$NoPortableKeys
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Version) {
    $Version = (Select-String -Path "app\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
}
if (-not $Version) { throw "Не удалось определить версию" }
Write-Host "==> Версия: $Version" -ForegroundColor Cyan

# --- 1. Программа ---
if (-not $SkipBuild) {
    $buildArgs = @{}
    if ($SkipFfmpeg)    { $buildArgs["SkipFfmpeg"] = $true }
    if ($NoPortableKeys) { $buildArgs["NoPortableKeys"] = $true }
    & "packaging\build.ps1" @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Сборка программы не удалась" }
}
$source = Join-Path $Root "dist\NoukoCloud"
if (-not (Test-Path (Join-Path $source "NoukoCloud.exe"))) {
    throw "Нет собранной программы: $source"
}

# --- 2. Компилятор Inno Setup ---
$iscc = $null
$cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 5\ISCC.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $iscc = $c; break }
    }
}
if (-not $iscc) {
    throw "Не найден ISCC.exe — установите Inno Setup 6 (https://jrsoftware.org/isdl.php) или добавьте его в PATH"
}
Write-Host "==> Inno Setup: $iscc" -ForegroundColor DarkGray

# --- 3. Сборка установщика ---
$outDir = Join-Path $Root "release\installer"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
& $iscc "/DMyAppVersion=$Version" "/DSourceDir=$source" "/O$outDir" "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Сборка установщика не удалась" }

$setup = Join-Path $outDir "NoukoCloud-Setup-$Version.exe"
if (-not (Test-Path $setup)) { throw "Установщик не найден: $setup" }
$mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "`nГотово: $setup ($mb МБ)" -ForegroundColor Green
Write-Host "Подписи нет: SmartScreen предупредит о неизвестном издателе — так и задумано для 1.0." -ForegroundColor DarkGray
