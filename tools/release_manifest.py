"""Манифест релиза: SHA256 архива и `latest.json`.

Рядом с архивом `NoukoCloud-win64.zip` появляются два файла:

* `NoukoCloud-win64.zip.sha256` — контрольная сумма в формате `sha256sum`
  (проверяется командой `sha256sum -c` и в Windows через `certutil`);
* `latest.json` — манифест релиза: версия, время публикации, размер и
  SHA256 архива. Его читает будущий автообновлятор (в 1.1).

Модуль не зависит от остального проекта, чтобы его можно было запускать
отдельно и тестировать без Qt.

    python tools/release_manifest.py --version 1.0.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Имя архива постоянное — ссылка releases/latest/download не должна ломаться.
ASSET_NAME = "NoukoCloud-win64.zip"
SHA_NAME = ASSET_NAME + ".sha256"
LATEST_NAME = "latest.json"

#: Поля, которые не должны попадать в публичный архив с настоящими значениями.
SECRET_FIELDS = (
    "yandex_client_id",
    "yandex_client_secret",
    "google_client_id",
    "google_client_secret",
    "google_api_key",
)

#: Минимальная версия, для которой манифест ещё имеет смысл.
MIN_VERSION = "0.5.0"


def sha256_file(path: Path) -> str:
    """SHA256 файла — по кускам, чтобы не тянуть архив в память."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256(archive: Path, out_path: Path | None = None) -> Path:
    """Пишет `<хэш>  <имя файла>` — такой файл понимает `sha256sum -c`."""
    archive = Path(archive)
    target = Path(out_path) if out_path else archive.with_name(SHA_NAME)
    digest = sha256_file(archive)
    target.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return target


def published_at_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_latest_json(version: str, archive: Path, *,
                       published_at: str | None = None,
                       min_version: str = MIN_VERSION,
                       mandatory: bool = False,
                       notes_url: str | None = None) -> dict:
    """Собирает манифест релиза в формате из ТЗ."""
    archive = Path(archive)
    size = archive.stat().st_size
    if notes_url is None:
        notes_url = f"https://github.com/peeppohui/NouKoCloud/releases/tag/v{version}"
    return {
        "version": version,
        "published_at": published_at or published_at_now(),
        "notes_url": notes_url,
        "min_version": min_version,
        "mandatory": bool(mandatory),
        "asset": {
            "name": archive.name,
            "size": size,
            "sha256": sha256_file(archive),
        },
    }


def write_latest_json(data: dict, out_path: Path | None = None) -> Path:
    target = Path(out_path) if out_path else Path(ROOT) / "release" / LATEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def write_release_artifacts(version: str, archive: Path,
                            release_dir: Path | None = None) -> tuple[Path, Path]:
    """Создаёт `.sha256` и `latest.json` рядом с архивом."""
    archive = Path(archive)
    folder = Path(release_dir) if release_dir else archive.parent
    folder.mkdir(parents=True, exist_ok=True)
    sha_path = write_sha256(archive, folder / SHA_NAME)
    manifest = build_latest_json(version, archive)
    json_path = write_latest_json(manifest, folder / LATEST_NAME)
    return sha_path, json_path


def archive_secret_fields(archive: Path) -> dict[str, str]:
    """Настоящие OAuth-ключи внутри архива (пусто — всё в порядке).

    Смотрим `config.json` прямо в zip: если в нём есть непустые ключи,
    архив нельзя выкладывать публично — их заберёт кто угодно.
    """
    found: dict[str, str] = {}
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if Path(name).name.lower() != "config.json":
                    continue
                try:
                    data = json.loads(zf.read(name).decode("utf-8-sig"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                for field in SECRET_FIELDS:
                    value = str(data.get(field) or "").strip()
                    if value:
                        found.setdefault(field, value)
    except (OSError, zipfile.BadZipFile):
        return found
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="SHA256 и latest.json для релиза")
    parser.add_argument("--version", required=True, help="версия, например 1.0.0")
    parser.add_argument("--archive", default=str(Path(ROOT) / "release" / ASSET_NAME))
    parser.add_argument("--allow-secrets", action="store_true",
                        help="не ругаться на настоящие ключи OAuth внутри архива")
    args = parser.parse_args()
    archive = Path(args.archive)
    if not archive.exists():
        raise SystemExit(f"Нет архива {archive}")
    sha_path, json_path = write_release_artifacts(args.version, archive)
    print(f"==> {sha_path}")
    print(f"==> {json_path}")
    secrets = archive_secret_fields(archive)
    if secrets and not args.allow_secrets:
        print("ВНИМАНИЕ: в архиве есть настоящие ключи OAuth:", ", ".join(sorted(secrets)))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
