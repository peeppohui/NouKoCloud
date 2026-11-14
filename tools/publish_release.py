"""Публикация релиза НоуКо Cloud на GitHub.

Создаёт (или обновляет) релиз и заливает архив со стабильным именем
`NoukoCloud-win64.zip` — чтобы ссылка
`https://github.com/<repo>/releases/latest/download/NoukoCloud-win64.zip`
никогда не ломалась.

Рядом с архивом публикуются `NoukoCloud-win64.zip.sha256` и `latest.json`
(манифест релиза для автообновления). Эти файлы не удаляются при перезаливке.

Токен берётся из системного хранилища git (Git Credential Manager).

    python tools/publish_release.py --version 1.1.0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Скрипт запускают как `python tools\publish_release.py` — папка tools
# и так в sys.path, но пусть будет явно: так работает и запуск из IDE.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_manifest import (  # noqa: E402
    LATEST_NAME,
    SHA_NAME,
    archive_secret_fields,
    write_release_artifacts,
)

REPO = "peeppohui/NouKoCloud"
ASSET_NAME = "NoukoCloud-win64.zip"      # имя постоянное — ссылка не должна ломаться
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "release" / ASSET_NAME
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
#: Установщик (Inno Setup, без подписи) — отдельный файл в релизе.
INSTALLER_DIR = ROOT / "release" / "installer"
SETUP_PREFIX = "NoukoCloud-Setup-"
#: Эти файлы живут в релизе постоянно — их не удаляем при перезаливке.
KEEP_ASSETS = (ASSET_NAME, SHA_NAME, LATEST_NAME, NOTICES.name)


def installer_asset(version: str) -> Path | None:
    """Файл установщика для этой версии (если он собран)."""
    candidate = INSTALLER_DIR / f"{SETUP_PREFIX}{version}.exe"
    return candidate if candidate.exists() else None


def is_kept_asset(name: str) -> bool:
    """Ассеты, которые нельзя удалять при перезаливке релиза."""
    return name in KEEP_ASSETS or name.startswith(SETUP_PREFIX)

BODY = """Настольная утилита для монтажёров: открыть папку на **Яндекс.Диске** или
**Google Диске** (свою или по публичной ссылке), посмотреть превью,
отметить нужные файлы и скачать с докачкой.

### Что нового в {version}

{changes}

### Как пользоваться

1. Скачайте **установщик** `NoukoCloud-Setup-{version}.exe` и запустите его —
   программа встанет в `%LOCALAPPDATA%\\Programs\\NoukoCloud`, ярлык
   появится в меню «Пуск». Либо скачайте архив `NoukoCloud-win64.zip` и
   распакуйте его в любую папку (поверх старой версии тоже можно —
   настройки и вход сохранятся).
2. Запустите `NoukoCloud.exe`.
3. Нажмите **«Открыть ссылку»** и вставьте ссылку на папку или файл — это
   работает сразу, без входа в аккаунт.
4. Отметьте нужные файлы чекбоксами и нажмите **«Скачать выбранное»**.

При первом запуске программа покажет **мастер настройки**: ключи, папку
загрузок и автозапуск. Его можно пропустить — публичные ссылки работают
и без ключей.

Полная инструкция для монтажёра — в `docs/ИНСТРУКЦИЯ.md` (папка `docs`
рядом с программой) и на странице репозитория.

### Если нужен свой «Мой диск»

Публичные ссылки работают без входа. Чтобы заглядывать в свой аккаунт,
впишите ключи OAuth-приложения в «Настройках» (или в мастере настройки)
и нажмите «Аккаунты» (инструкция — в `docs/OAUTH_SETUP.md` в репозитории).

### Проверка целостности

Рядом с архивом лежит `NoukoCloud-win64.zip.sha256` — сверьте его, если
хотите убедиться, что файл докачался целиком.

### Требования

Windows 10/11 x64. Python не нужен.

### Установщик не подписан

Сборка **не подписана** сертификатом: Windows и SmartScreen покажут
предупреждение «Неизвестный издатель». Это ожидаемо — нажмите
«Подробнее» → «Выполнить в любом случае». Настройки, входы в аккаунты
и скачанные файлы при удалении программы остаются на месте.
"""

#: Что изменилось в текущем релизе — попадает в описание на GitHub.
CHANGES = """Версия 1.1.0 — в ней закрыты все пункты P0 из ТЗ на 1.0: то, что в 1.0.0
ещё было в планах, теперь работает.

### Нового

* **Установщик для Windows.** Появился `NoukoCloud-Setup-1.1.0.exe`: ставится
  без прав администратора в `%LOCALAPPDATA%\\Programs\\NoukoCloud`, ярлык
  появляется в «Пуск». Portable-архив тоже остался — кому удобнее без
  установки. Сборка не подписана: Windows покажет «Неизвестный издатель»,
  нажмите «Подробнее» → «Выполнить в любом случае».
* **Мастер первого запуска.** При первом старте программа сама предлагает
  настроить ключи, папку загрузок и автозапуск. Пропустить можно — программа
  работает и с публичными ссылками. Повторно мастер открывается через
  «Помощь» → «Мастер настройки».
* **Поиск и фильтры в дереве.** Поиск по уже загруженным файлам (Ctrl+F),
  фильтр по типам (видео, аудио, фото, документы), счётчик совпадений.
  Регистр не важен, «ё» находится по «е».
* **Покадровое превью.** Таймкод `ЧЧ:ММ:СС:КК`, шаг кадра клавишами `,` и `.`,
  скорость J/K/L, отметки фрагмента I/O и кнопка «Кадр PNG» — кадр
  сохраняется в папку загрузок без интерфейса.
* **Отчёты и место.** «Отчёт CSV» выгружает очередь в файл, а при нехватке
  места на диске загрузки встают на паузу, не стирая недокачанное.
* **Повтор неудачных.** Кнопка «Повторить неудачные» в очереди.
* **Диагностика по кнопке.** «Собрать диагностику» делает архив с журналом
  и настройками; токены и ключи в нём закрыты. Ничего никуда не отправляется
  само — файл остаётся у вас.
* **«О программе».** Версия, дата сборки, ссылка на релиз, проверка
  обновлений и список лицензий.
* **Выход из аккаунта.** «Аккаунты» → «Выйти» или «Сменить аккаунт»:
  вход удаляется только у выбранного облака, чужие публичные ссылки остаются.

### Надёжность

* **Списки папок кэшируются** на 5 минут: повторное открытие папки не
  ходит в сеть. Если сеть отвалилась, показываются сохранённые данные с
  пометкой, что они могли устареть.
* **Настройки и состояние переживают обновление.** Перед миграцией
  сохраняется копия `config.v2.bak`, повреждённый файл откладывается как
  `config.broken.json`, а недокачанные части продолжают докачиваться.
* **Кнопка «Обновить»** сбрасывает кэш и перечитывает папку заново.

### Мелочи

* В очереди виден счётчик неудачных загрузок, а кнопка «Отчёт CSV» пишет
  `NoukoCloud-report-<дата>.csv` в папку загрузок.
* Поиск не тормозит интерфейс: он идёт по уже загруженному дереву и не
  делает запросов в облако.
* Исправлены мелкие ошибки интерфейса (иконка «О программе», длинные
  сообщения об ошибках, обрезка текста в списках).
"""


def get_token() -> str:
    """Достаёт токен из хранилища git."""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8",
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(
        "Не найден токен GitHub. Выполните один раз: git push (откроется окно входа)"
    )


def upload_asset(client, release_id: int, path: Path, name: str | None = None) -> dict:
    """Заливает один файл в релиз и возвращает его описание."""
    asset_name = name or path.name
    size = path.stat().st_size
    print(f"загружаю {asset_name} ({size / 1024 / 1024:.1f} МБ)...")
    started = time.time()
    with open(path, "rb") as handle:
        resp = client.post(
            f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets",
            params={"name": asset_name},
            content=handle,
            headers={"Content-Type": "application/octet-stream"},
            timeout=3600.0,
        )
    if resp.status_code not in (200, 201):
        print("ОШИБКА загрузки:", resp.status_code, resp.text[:400])
        raise SystemExit(1)
    print(f"загружено за {time.time() - started:.0f} с")
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация релиза НоуКо Cloud")
    parser.add_argument("--version", required=True, help="версия, например 1.1.0")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--allow-secrets", action="store_true",
                        help="разрешить архив с настоящими ключами OAuth (по умолнию — нет)")
    args = parser.parse_args()

    if not ARCHIVE.exists():
        raise SystemExit(f"Нет архива {ARCHIVE}. Соберите его: packaging\\release.ps1")

    # Настоящие ключи OAuth в публичном архиве — нельзя: их заберёт кто угодно.
    secrets = archive_secret_fields(ARCHIVE)
    if secrets and not args.allow_secrets:
        print("ОШИБКА: в архиве есть настоящие ключи OAuth:", ", ".join(sorted(secrets)))
        print("Соберите архив без ключей (packaging\\release.ps1) или разрешите флагом --allow-secrets.")
        return 2

    # Контрольная сумма и манифест релиза — рядом с архивом.
    sha_path, json_path = write_release_artifacts(args.version, ARCHIVE)
    print(f"==> {sha_path}")
    print(f"==> {json_path}")

    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "NoukoCloud-release",
    }
    api = f"https://api.github.com/repos/{REPO}"
    tag = f"v{args.version}"
    name = f"НоуКо Cloud {args.version}"
    body = BODY.format(version=args.version, changes=CHANGES)

    # Редиректы обязательны: после переименования репозитория GitHub
    # отвечает 301 на «человеческое» имя и ждёт запрос по числовому id.
    with httpx.Client(headers=headers, timeout=120.0, follow_redirects=True) as client:
        # 1. Релиз с таким тегом уже есть?
        existing = client.get(f"{api}/releases/tags/{tag}")
        if existing.status_code == 200:
            release = existing.json()
            release_id = release["id"]
            print(f"релиз {tag} уже есть (id={release_id}) — обновляем")
            upd = client.patch(
                f"{api}/releases/{release_id}",
                json={"name": name, "body": body, "draft": args.draft, "prerelease": False},
            )
            if upd.status_code >= 300:
                print("ОШИБКА обновления:", upd.status_code, upd.text[:300])
                return 1
        else:
            resp = client.post(
                f"{api}/releases",
                json={
                    "tag_name": tag,
                    "name": name,
                    "body": body,
                    "draft": args.draft,
                    "prerelease": False,
                },
            )
            if resp.status_code not in (200, 201):
                print("ОШИБКА создания:", resp.status_code, resp.text[:400])
                return 1
            release_id = resp.json()["id"]
            print(f"релиз создан: {tag} (id={release_id})")

        # 2. Старые файлы с другими именами убираем — остаются только наши.
        #    Постоянные (архив, .sha256, latest.json) перезаливаем на месте.
        for asset in client.get(f"{api}/releases/{release_id}/assets").json():
            if not is_kept_asset(asset["name"]):
                print(f"удаляю устаревший файл: {asset['name']}")
                client.delete(f"{api}/releases/assets/{asset['id']}")
            else:
                print(f"заменяю файл: {asset['name']}")
                client.delete(f"{api}/releases/assets/{asset['id']}")

        # 3. Заливаем архив и сопутствующие файлы.
        upload_asset(client, release_id, ARCHIVE, ASSET_NAME)
        upload_asset(client, release_id, sha_path, SHA_NAME)
        upload_asset(client, release_id, json_path, LATEST_NAME)
        if NOTICES.exists():
            upload_asset(client, release_id, NOTICES, NOTICES.name)
        setup = installer_asset(args.version)
        if setup is not None:
            upload_asset(client, release_id, setup, setup.name)
        else:
            print("установщик не собран — в релизе его не будет "
                  "(соберите: packaging\\build_installer.ps1)")

        rel = client.get(f"{api}/releases/{release_id}").json()
        print(f"итог: {rel['name']} | draft={rel['draft']} | файлы: "
              f"{[(a['name'], round(a['size'] / 1024 / 1024, 1)) for a in rel['assets']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
