<#
    Сборка портативной версии НоуКо Cloud.

    .\packaging\build.ps1
    .\packaging\build.ps1 -OneFile
    .\packaging\build.ps1 -YandexClientId "123" -YandexClientSecret "abc" `
                            -GoogleClientId "id.apps.googleusercontent.com" -GoogleClientSecret "GOCSPX-..."

    Ключи OAuth кладутся в config.json рядом с exe: монтажёрам не нужно
    ничего вводить — достаточно нажать «Аккаунты» → «Войти».

    ВАЖНО: config.json с настоящими ключами попадает в публичный архив,
    и ключи сможет забрать кто угодно. Для публичных релизов собирайте
    без ключей (-NoPortableKeys) — пользователь введёт их в мастере
    настройки или в «Настройках».
#>
param(
    [switch]$OneFile,
    [switch]$SkipFfmpeg,
    [switch]$NoPortableKeys,
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
Get-Process -Name "NoukoCloud" -ErrorAction SilentlyContinue | Stop-Process -Force
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
    $env:NOUKOCLOUD_ONEFILE = "1"
} else {
    $env:NOUKOCLOUD_ONEFILE = "0"
}
$pyiArgs += "packaging\noukocloud.spec"

Write-Host "==> Собираем" -ForegroundColor Cyan
python @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }

# --- Раскладываем рядом с exe ---
if ($OneFile) {
    $target = "dist"                      # один файл: dist\NoukoCloud.exe
    # В одно-файловом режиме spec ffmpeg не кладёт — копируем сами,
    # содержимое папки (иначе получится bin\bin\).
    if (Test-Path "bin") {
        New-Item -ItemType Directory -Force -Path (Join-Path $target "bin") | Out-Null
        Copy-Item -Recurse -Force "bin\*" (Join-Path $target "bin") -ErrorAction SilentlyContinue
    }
} else {
    $target = "dist\NoukoCloud"       # папка: dist\NoukoCloud\NoukoCloud.exe
    # ffmpeg кладёт сам spec — и без ffprobe (−100 МБ). Копировать папку
    # bin ещё раз нельзя: появится bin\bin\ и архив потолстеет вдвое.
}

Copy-Item -Recurse -Force docs (Join-Path $target "docs") -ErrorAction SilentlyContinue

# --- Ключи OAuth в portable-конфиг ---
$portable = [ordered]@{
    yandex_client_id      = $YandexClientId
    yandex_client_secret = $YandexClientSecret
    google_client_id      = $GoogleClientId
    google_client_secret = $GoogleClientSecret
    google_api_key        = $GoogleApiKey
}
$filled = ($portable.Values | Where-Object { $_ -and $_.Trim() }).Count
if ($NoPortableKeys) {
    # Публичная сборка: настоящих ключей в архиве быть не должно.
    $portablePath = Join-Path $target "config.json"
    if (Test-Path $portablePath) { Remove-Item $portablePath -Force }
    Write-Host "    Ключи OAuth не кладутся в сборку (-NoPortableKeys)" -ForegroundColor Green
} elseif ($filled -eq 0) {
    Write-Host "    Ключи OAuth не заданы — пользователь введёт их в мастере настройки" -ForegroundColor Yellow
} else {
    $portable | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $target "config.json")
    if ($filled -eq $portable.Values.Count) {
        Write-Host "    Ключи OAuth записаны в config.json рядом с программой" -ForegroundColor Green
    } else {
        Write-Host "    Записаны не все ключи OAuth ($filled из $($portable.Values.Count))" -ForegroundColor Yellow
        Write-Host "    Пользователю придётся ввести недостающие в «Настройках»" -ForegroundColor Yellow
    }
}

# --- Уведомления о сторонних программах ---
$notices = Join-Path $Root "THIRD_PARTY_NOTICES.md"
if (Test-Path $notices) {
    Copy-Item -Force $notices (Join-Path $target "THIRD_PARTY_NOTICES.md")
}

Write-Host "`nГотово: $target\NoukoCloud.exe" -ForegroundColor Green
Write-Host "Для публикации: zip этого каталога и прикрепите к GitHub Release." -ForegroundColor DarkGray