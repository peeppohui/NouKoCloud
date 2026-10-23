"""Проверка новой версии на GitHub.

Это не автообновление: задача модуля — узнать, что на GitHub вышел более
новый релиз, и дать главному окну открыть его страницу в браузере. Проверка
не должна мешать работе, поэтому все ошибки сети здесь считаются «новостей
нет» и только пишутся в журнал.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/repos/peeppohui/NouKoCloud/releases/latest"
RELEASES_URL = "https://github.com/peeppohui/NouKoCloud/releases"
#: Как часто проверять GitHub, если программа долго не закрывается.
CHECK_INTERVAL = 24 * 60 * 60
#: Проверка не должна задерживать запуск: пять секунд — уже много для GitHub.
REQUEST_TIMEOUT = 5.0

_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")
_ALLOWED_HOSTS = {"github.com", "www.github.com"}


@dataclass(frozen=True)
class ReleaseInfo:
    """Сведения о последнем релизе, которых достаточно для уведомления."""

    version: str
    url: str
    name: str = ""
    published_at: str = ""


def parse_version(value: str) -> tuple[int, int, int] | None:
    """«v0.5.4» / «0.5.4» → (0, 5, 4). Некорректная версия → None."""
    match = _VERSION_RE.fullmatch((value or "").strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(candidate: str, current: str) -> bool:
    """Строго ли новее версия-кандидат. Неизвестные версии не считаем новее."""
    left = parse_version(candidate)
    right = parse_version(current)
    if left is None or right is None:
        return False
    return left > right


def should_notify(release: ReleaseInfo | None, current: str, notified: str) -> bool:
    """Пора ли открывать браузер: релиз новее и о нём ещё не сообщали."""
    if release is None:
        return False
    return is_newer(release.version, current) and release.version != notified


def is_check_due(last_check: float, now: float | None = None,
                 interval: float = CHECK_INTERVAL) -> bool:
    """Проверяли ли мы GitHub в последние ``interval`` секунд."""
    if last_check <= 0:
        return True
    current = time.time() if now is None else now
    return current - last_check >= interval


def _is_github_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS


def parse_release(payload: object) -> ReleaseInfo | None:
    """Достаёт версию и ссылку из ответа GitHub Releases API."""
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    version = tag.strip().lstrip("vV")
    url = payload.get("html_url")
    if not isinstance(url, str) or not _is_github_url(url):
        url = RELEASES_URL
    name = payload.get("name")
    published = payload.get("published_at")
    return ReleaseInfo(
        version=version,
        url=url,
        name=name if isinstance(name, str) else "",
        published_at=published if isinstance(published, str) else "",
    )


async def fetch_latest_release(
    client: httpx.AsyncClient,
    url: str = API_URL,
    timeout: float = REQUEST_TIMEOUT,
) -> ReleaseInfo | None:
    """Запрашивает последний релиз. Любая ошибка — просто None."""
    try:
        response = await client.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        log.info("Проверка обновлений: GitHub недоступен (%s)", exc)
        return None
    if response.status_code != 200:
        log.info("Проверка обновлений: GitHub ответил %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        log.info("Проверка обновлений: GitHub прислал не JSON")
        return None
    release = parse_release(payload)
    if release is None:
        log.info("Проверка обновлений: в ответе нет подходящего релиза")
    return release
