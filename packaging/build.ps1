<#
    Сборка портативной версии svsCloud.

    .\packaging\build.ps1
    .\packaging\build.ps1 -OneFile
    .\packaging\build.ps1 -YandexClientId "123" -YandexClientSecret "abc" `
                            -GoogleClientId "id.apps.googleusercontent.com" -GoogleClientSecret "GOCSPX-..."

    Ключи OAuth кладутся в config.json рядом с exe: монтажёрам не нужно
    ничего вводить — достаточно нажать «Аккаунты» → «Войти».
#>
param(
    [switch]$OneFile,
    [switch]$SkipFfmpeg,
    [string]$YandexClientId = "",
    [string]$YandexClientSecret = "",
    [string]$GoogleClientId = "",
    [string]$GoogleClientSecret = "",
    [string]$GoogleApiKey = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "==> Проверяем зависимости" -ForegroundColor Cyan
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if (-not $SkipFfmpeg) {
    $ffmpeg = Join-Path $Root "bin\ffmpeg.exe"
    if (-not (Test-Path $ffmpeg)) {
        Write-Host "==> Скачиваем ffmpeg" -ForegroundColor Cyan
        python tools\fetch_ffmpeg.py
    } else {
        Write-Host "    ffmpeg уже на месте" -ForegroundColor DarkGray
    }
}

Write-Host "==> Убираем старые сборки" -ForegroundColor Cyan
Get-Process -Name "svsCloud" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", "dist",
    "--workpath", "build"
)
# Режим (папка/один файл) задаётся переменной — так совместимо с .spec
if ($OneFile) {
    $env:SVSCLOUD_ONEFILE = "1"
} else {
    $env:SVSCLOUD_ONEFILE = "0"
}
$pyiArgs += "packaging\svscloud.spec"

Write-Host "==> Собираем" -ForegroundColor Cyan
python @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }

# --- Раскладываем рядом с exe ---
if ($OneFile) {
    $target = "dist"                      # один файл: dist\svsCloud.exe
} else {
    $target = "dist\svsCloud"       # папка: dist\svsCloud\svsCloud.exe
}

Copy-Item -Recurse -Force docs (Join-Path $target "docs") -ErrorAction SilentlyContinue
if (Test-Path "bin") {
    Copy-Item -Recurse -Force bin (Join-Path $target "bin") -ErrorAction SilentlyContinue
}

# --- Ключи OAuth в portable-конфиг ---
$portable = [ordered]@{
    yandex_client_id      = $YandexClientId
    yandex_client_secret = $YandexClientSecret
    google_client_id      = $GoogleClientId
    google_client_secret = $GoogleClientSecret
    google_api_key        = $GoogleApiKey
}
$portable | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $target "config.json")
$filled = ($portable.Values | Where-Object { $_ -and $_.Trim() }).Count
if ($filled -eq $portable.Values.Count) {
    Write-Host "    Ключи OAuth записаны в config.json рядом с программой" -ForegroundColor Green
} else {
    Write-Host "    Записаны не все ключи OAuth ($filled из $($portable.Values.Count))" -ForegroundColor Yellow
    Write-Host "    Пользователю придётся ввести недостающие в «Настройках»" -ForegroundColor Yellow
}

Write-Host "`nГотово: $target\svsCloud.exe" -ForegroundColor Green
Write-Host "Для публикации: zip этого каталога и прикрепите к GitHub Release." -ForegroundColor DarkGray