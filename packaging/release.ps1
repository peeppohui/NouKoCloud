<#
    Выпуск релиза НоуКо Cloud: сборка -> архив -> проверка -> публикация.

    .\packaging\release.ps1 -Version 1.1.0

    По умолчанию сборка идёт БЕЗ настоящих OAuth-ключей: ключи в публичном
    архиве заберёт кто угодно. Пользователь вводит их в мастере настройки
    или в «Настройках». Если ключи всё-таки нужны внутри сборки (своя
    приватная раздача) — добавьте -WithKeys.

    Имя архива всегда NoukoCloud-win64.zip — чтобы ссылка
    releases/latest/download/NoukoCloud-win64.zip не ломалась между версиями.

    Шаги:
      1. сборка (packaging\build.ps1);
      2. архив release\NoukoCloud-win64.zip;
      3. проверка «в архиве нет настоящих ключей» + .sha256 + latest.json;
      4. установщик release\installer\NoukoCloud-Setup-<версия>.exe (Inno Setup,
         без подписи — SmartScreen предупредит; -SkipInstaller пропускает шаг);
      5. публикация на GitHub (tools\publish_release.py).
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$SkipFfmpeg,
    [switch]$SkipBuild,
    [switch]$SkipInstaller,
    [switch]$WithKeys,
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
    if (-not $WithKeys)  { $buildArgs["NoPortableKeys"] = $true }
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

# --- 3. Проверка и манифест релиза ---
# Скрипт сам делает .sha256 и latest.json и ругается, если внутри архива
# остались настоящие ключи OAuth.
$manifestArgs = @("tools\release_manifest.py", "--version", $Version, "--archive", $archive)
if ($WithKeys) { $manifestArgs += "--allow-secrets" }
python @manifestArgs
if ($LASTEXITCODE -eq 2) {
    throw "В архиве есть настоящие ключи OAuth. Соберите без ключей (без -WithKeys) или раздавайте архив приватно."
}
if ($LASTEXITCODE -ne 0) { throw "Не удалось подготовить манифест релиза" }

# --- 4. Установщик (Inno Setup, без подписи) ---
if (-not $SkipInstaller) {
    Write-Host "==> Собираем установщик" -ForegroundColor Cyan
    & "packaging\build_installer.ps1" -Version $Version -SkipBuild
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    установщик не собрался — релиз можно опубликовать и без него" -ForegroundColor Yellow
    }
}

# --- 5. Публикация ---
python tools\publish_release.py --version $Version
if ($LASTEXITCODE -ne 0) { throw "Публикация не удалась" }

Write-Host "`nГотово. Постоянная ссылка на скачивание:" -ForegroundColor Green
Write-Host "https://github.com/peeppohui/NouKoCloud/releases/latest/download/NoukoCloud-win64.zip" -ForegroundColor Green
Write-Host "Контрольная сумма: release\NoukoCloud-win64.zip.sha256" -ForegroundColor DarkGray
Write-Host "Манифест релиза:  release\latest.json" -ForegroundColor DarkGray
