<#
    Выпуск релиза НоуКо Cloud: сборка -> архив -> публикация на GitHub.

    .\packaging\release.ps1 -Version 0.1.0
    .\packaging\release.ps1 -Version 0.2.0 -YandexClientId "..." -YandexClientSecret "..."

    Имя архива всегда NoukoCloud-win64.zip — чтобы ссылка
    releases/latest/download/NoukoCloud-win64.zip не ломалась между версиями.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$SkipFfmpeg,
    [switch]$SkipBuild,
    [string]$YandexClientId = "",
    [string]$YandexClientSecret = "",
    [string]$GoogleClientId = "",
    [string]$GoogleClientSecret = "",
    [string]$GoogleApiKey = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- 1. Сборка ---
if (-not $SkipBuild) {
    $buildArgs = @{}
    if ($SkipFfmpeg)     { $buildArgs["SkipFfmpeg"] = $true }
    if ($YandexClientId)     { $buildArgs["YandexClientId"] = $YandexClientId }
    if ($YandexClientSecret) { $buildArgs["YandexClientSecret"] = $YandexClientSecret }
    if ($GoogleClientId)     { $buildArgs["GoogleClientId"] = $GoogleClientId }
    if ($GoogleClientSecret) { $buildArgs["GoogleClientSecret"] = $GoogleClientSecret }
    if ($GoogleApiKey)       { $buildArgs["GoogleApiKey"] = $GoogleApiKey }
    & "packaging\build.ps1" @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }
}

# --- 2. Архив со стабильным именем ---
$source = "dist\NoukoCloud"
if (-not (Test-Path $source)) { throw "нет папки $source" }
New-Item -ItemType Directory -Force -Path "release" | Out-Null
$archive = "release\NoukoCloud-win64.zip"
if (Test-Path $archive) { Remove-Item $archive -Force }
Compress-Archive -Path "$source\*" -DestinationPath $archive -CompressionLevel Optimal
$mb = [math]::Round((Get-Item $archive).Length / 1MB, 1)
Write-Host "==> Архив: $archive ($mb МБ)" -ForegroundColor Cyan

# --- 3. Публикация ---
python tools\publish_release.py --version $Version
if ($LASTEXITCODE -ne 0) { throw "Публикация не удалась" }

Write-Host "`nГотово. Постоянная ссылка на скачивание:" -ForegroundColor Green
Write-Host "https://github.com/peeppohui/NouKoCloud/releases/latest/download/NoukoCloud-win64.zip" -ForegroundColor Green
