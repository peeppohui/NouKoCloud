"""Публикация релиза НоуКо Cloud на GitHub.

Создаёт (или обновляет) релиз и заливает архив со стабильным именем
`NoukoCloud-win64.zip` — чтобы ссылка
`https://github.com/<repo>/releases/latest/download/NoukoCloud-win64.zip`
никогда не ломалась.

Токен берётся из системного хранилища git (Git Credential Manager).

    python tools/publish_release.py --version 0.1.0
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = "peeppohui/NouKoCloud"
ASSET_NAME = "NoukoCloud-win64.zip"      # имя постоянное — ссылка не должна ломаться
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "release" / ASSET_NAME

BODY = """Настольная утилита для монтажёров: открыть папку на **Яндекс.Диске** или
**Google Диске** (свою или по публичной ссылке), посмотреть превью,
отметить нужные файлы и скачать с докачкой.

### Что нового в {version}

{changes}

### Как пользоваться

1. Скачайте архив и распакуйте его в любую папку (поверх старой версии
   тоже можно — настройки и вход сохранятся).
2. Запустите `NoukoCloud.exe`.
3. Нажмите **«Открыть ссылку»** и вставьте ссылку на папку или файл — это
   работает сразу, без входа в аккаунт.
4. Отметьте нужные файлы чекбоксами и нажмите **«Скачать выбранное»**.

Полная инструкция для монтажёра — в `docs/ИНСТРУКЦИЯ.md` (папка `docs`
рядом с программой) и на странице репозитория.

### Если нужен свой «Мой диск»

Публичные ссылки работают без входа. Чтобы заглядывать в свой аккаунт,
впишите ключи OAuth-приложения в «Настройках» и нажмите «Аккаунты»
(инструкция — в `docs/OAUTH_SETUP.md` в репозитории).

### Требования

Windows 10/11 x64. Python не нужен.
"""

#: Что изменилось в текущем релизе — попадает в описание на GitHub.
CHANGES = """* **Скачивание с YouTube, VK, Rutube, Дзен и других сайтов снова работает.**
  Раньше «быстрая закачка» тянула целый файл по разу на каждую часть:
  ролик на 2 ГБ уходил в две тысячи одинаковых запросов и не заканчивался
  никогда. Теперь части тянутся параллельно, и каждая пишется на своё место.
* **В архив вложен ffmpeg вместе с ffprobe** — отдельно ставить ничего не
  нужно. Без ffprobe yt-dlp не мог определить кодек звука, и склейка видео
  со звуком (а у YouTube они лежат отдельно) падала с «unable to obtain
  file audio codec with ffprobe».
* **Понятные объяснения ошибок.** Раньше любая ошибка выглядела одинаково
  («Не удалось скачать по этой ссылке»), а «ffmpeg не найден» показывалось
  даже когда он был на месте. Теперь видна настоящая причина.
* **Метка фрагмента в имени файла** больше не похожа на дробь:
  «00-02–00-05» вместо «0.02–0.05».
* **Журнал пишет, какой ffmpeg найден** — сразу видно, если он куда-то
  делся.
* **Программа закрывается тихо**, а в тестах больше не остаётся
  «незабранных» фоновых задач.

В 0.5.1 было: **исправления после переименования репозитория** (ссылка на
скачивание, публикация релиза, дубль ffmpeg в архиве), повторный запуск
больше не теряет команду, чистое закрытие программы.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация релиза НоуКо Cloud")
    parser.add_argument("--version", required=True, help="версия, например 0.1.0")
    parser.add_argument("--draft", action="store_true")
    args = parser.parse_args()

    if not ARCHIVE.exists():
        raise SystemExit(f"Нет архива {ARCHIVE}. Соберите его: packaging\\release.ps1")

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

        # 2. Старые файлы с другими именами убираем — остаётся один стабильный.
        for asset in client.get(f"{api}/releases/{release_id}/assets").json():
            if asset["name"] != ASSET_NAME:
                print(f"удаляю устаревший файл: {asset['name']}")
                client.delete(f"{api}/releases/assets/{asset['id']}")
            else:
                print(f"заменяю файл: {asset['name']}")
                client.delete(f"{api}/releases/assets/{asset['id']}")

        # 3. Заливаем архив.
        size = ARCHIVE.stat().st_size
        print(f"загружаю {ASSET_NAME} ({size / 1024 / 1024:.1f} МБ)...")
        started = time.time()
        with open(ARCHIVE, "rb") as handle:
            resp = client.post(
                f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets",
                params={"name": ASSET_NAME},
                content=handle,
                headers={"Content-Type": "application/zip"},
                timeout=3600.0,
            )
        if resp.status_code not in (200, 201):
            print("ОШИБКА загрузки:", resp.status_code, resp.text[:400])
            return 1
        data = resp.json()
        print(f"загружено за {time.time() - started:.0f} с")
        print(f"ссылка: {data['browser_download_url']}")

        rel = client.get(f"{api}/releases/{release_id}").json()
        print(f"итог: {rel['name']} | draft={rel['draft']} | файлы: "
              f"{[(a['name'], round(a['size'] / 1024 / 1024, 1)) for a in rel['assets']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
