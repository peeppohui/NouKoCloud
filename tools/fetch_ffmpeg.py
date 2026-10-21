"""Скачивает статическую сборку ffmpeg в папку bin/.

ffmpeg нужен только для превью форматов, которые не играет браузер
(mkv, avi, prores) и для кадров-постеров. Сама загрузка файлов
работает без него.

Запуск:  python tools/fetch_ffmpeg.py
"""

from __future__ import annotations

import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = ROOT / "bin"

# Официальная сборка для Windows (статическая, с libx264 и AAC).
ARCHIVE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# Запасные адреса на случай, если основной изменится.
FALLBACKS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)

WANTED = ("ffmpeg.exe",)          # ffprobe программе не нужен
#: Лицензия ffmpeg кладётся рядом: распространять сборку можно только вместе с ней.
LICENSE_NAMES = ("LICENSE", "LICENSE.txt", "COPYING.GPLv3", "COPYING.GPLv3.txt")


def download(url: str, target: Path) -> None:
    print(f"Качаем {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "NoukoCloud/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done * 100 // total}%", end="", flush=True)
    print()


def extract(archive: Path) -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    found = 0
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            name = Path(member).name
            if name in WANTED:
                with bundle.open(member) as source, open(TARGET_DIR / name, "wb") as target:
                    shutil.copyfileobj(source, target)
                print(f"  распакован bin/{name}")
                found += 1
            elif name in LICENSE_NAMES and not (TARGET_DIR / "LICENSE-ffmpeg.txt").exists():
                with bundle.open(member) as source, open(TARGET_DIR / "LICENSE-ffmpeg.txt", "wb") as target:
                    shutil.copyfileobj(source, target)
                print("  распакована лицензия ffmpeg")
    (TARGET_DIR / "README-ffmpeg.txt").write_text(
        "ffmpeg — свободная программа (https://ffmpeg.org), в составе НоуКо Cloud\n"
        "используется без изменений для превью редких форматов.\n"
        "Лицензия — LICENSE-ffmpeg.txt. Исходный код: https://ffmpeg.org/download.html\n"
        "Сборка для Windows: https://www.gyan.dev/ffmpeg/builds/\n",
        encoding="utf-8",
    )
    return found


def main() -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "ffmpeg.zip"
    urls = (ARCHIVE_URL,) + FALLBACKS
    for url in urls:
        try:
            download(url, archive)
        except Exception as exc:
            print(f"  не получилось: {exc}")
            continue
        count = extract(archive)
        archive.unlink(missing_ok=True)
        if count:
            print(f"Готово. ffmpeg лежит в {TARGET_DIR}")
            return 0
    print("Не удалось скачать ffmpeg. Скачайте его вручную и положите в bin/.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
