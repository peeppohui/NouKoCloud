"""Менеджер авторизации: единая точка входа для обоих облаков.

Хранит токены, обновляет их заранее (до истечения), отдаёт провайдерам
готовый access_token.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from typing import Optional

from ..core.bus import Bus
from ..core.config import AppConfig
from ..core.errors import AuthError
from ..core.models import ProviderId
from ..core.secrets_store import TokenStore
from .base import OAuthProvider, OAuthTokens
from .google import GoogleOAuth, has_drive_access
from .loopback import LoopbackRedirectServer
from .yandex import REDIRECT_URIS, YandexOAuth

log = logging.getLogger(__name__)


class OAuthManager:
    def __init__(self, config: AppConfig, store: TokenStore, bus: Optional[Bus] = None) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._tokens: dict[ProviderId, OAuthTokens] = {}
        self._names: dict[ProviderId, str] = {}
        self._locks: dict[ProviderId, asyncio.Lock] = {}
        self._providers: dict[ProviderId, OAuthProvider] = {}
        self._build_providers()
        self._load_tokens()

    # ------------------------------------------------------------------
    def _build_providers(self) -> None:
        self._providers = {
            ProviderId.YANDEX: YandexOAuth(
                self._config.yandex_client_id,
                self._config.yandex_client_secret,
                device_id=self._config.device_id,
                device_name=self._config.device_name,
            ),
            ProviderId.GOOGLE: GoogleOAuth(
                self._config.google_client_id, self._config.google_client_secret
            ),
        }

    def reload_config(self) -> None:
        """Вызывать после изменения ключей в настройках."""
        self._build_providers()

    def _load_tokens(self) -> None:
        for pid in ProviderId:
            data = self._store.get(pid.value)
            if data:
                try:
                    tokens = OAuthTokens.from_dict(data)
                except Exception as exc:
                    log.warning("Битый токен %s: %s", pid.value, exc)
                    continue
                if pid is ProviderId.GOOGLE and not has_drive_access(tokens.scope):
                    # Вход без галочки «Google Диск»: с ним не открыть ни одного
                    # файла. Считаем, что входа нет, — программа предложит войти.
                    log.warning("Сохранённый вход Google без доступа к Диску — нужен новый вход")
                    self._store.clear(pid.value)
                    continue
                self._tokens[pid] = tokens

    # ------------------------------------------------------------------
    def provider(self, pid: ProviderId) -> OAuthProvider:
        return self._providers[pid]

    def is_logged_in(self, pid: ProviderId) -> bool:
        tokens = self._tokens.get(pid)
        return bool(tokens and tokens.access_token)

    def account_name(self, pid: ProviderId) -> str:
        return self._names.get(pid, "")

    def configured(self, pid: ProviderId) -> bool:
        return self._providers[pid].configured

    def auth_url(self, pid: ProviderId, redirect_uri: str | None = None) -> str:
        provider = self._providers[pid]
        if redirect_uri is None:
            redirect_uri = REDIRECT_URIS[0] if pid is ProviderId.YANDEX else "http://127.0.0.1:8765"
        return provider.authorization_url(redirect_uri, state="noukocloud")

    # ------------------------------------------------------------------
    async def complete_code(self, pid: ProviderId, code: str, redirect_uri: str | None = None) -> str:
        """Договаривает вход по коду (используется для Яндекса)."""
        provider = self._providers[pid]
        uri = redirect_uri or (REDIRECT_URIS[0] if pid is ProviderId.YANDEX else "")
        tokens = await provider.exchange_code(code, uri)
        await self._store_tokens(pid, tokens)
        return self._names.get(pid, provider.display_name)

    async def login_with_browser(self, pid: ProviderId) -> str:
        """Полный вход с перехватом кода на локальном сервере (Google)."""
        provider = self._providers[pid]
        server = LoopbackRedirectServer()
        await server.start()
        # Тот же адрес обязан уйти и в обмен кода — берём его, пока сервер жив.
        redirect_uri = server.redirect_uri
        url = provider.authorization_url(redirect_uri, state="noukocloud")
        log.info("Открываем браузер: %s", url)
        await asyncio.to_thread(webbrowser.open, url)
        try:
            code = await server.wait_for_code(timeout=300)
        except asyncio.TimeoutError:
            raise AuthError("Вход не завершён за 5 минут. Попробуйте ещё раз.") from None
        finally:
            await server.stop()
        tokens = await provider.exchange_code(code, redirect_uri)
        await self._store_tokens(pid, tokens)
        return self._names.get(pid, provider.display_name)

    async def _store_tokens(self, pid: ProviderId, tokens: OAuthTokens) -> None:
        self._tokens[pid] = tokens
        self._store.set(pid.value, tokens.to_dict())
        name = ""
        try:
            name = await self._providers[pid].account_info(tokens)
        except Exception as exc:
            log.debug("Не удалось получить имя аккаунта: %s", exc)
        self._names[pid] = name
        log.info("Вход выполнен: %s (%s)", pid.value, name or "-")
        if self._bus is not None:
            self._bus.logged_in.emit(pid.value, name or self._providers[pid].display_name)

    # ------------------------------------------------------------------
    async def access_token(self, pid: ProviderId) -> str:
        """Готовый access_token; при необходимости обновляет его."""
        tokens = self._tokens.get(pid)
        if tokens is None:
            raise AuthError(f"Нет входа в {self._providers[pid].display_name}. Войдите в аккаунт.")
        if not tokens.is_expired():
            return tokens.access_token

        lock = self._locks.setdefault(pid, asyncio.Lock())
        async with lock:
            tokens = self._tokens.get(pid)
            if tokens is None:
                raise AuthError(f"Нет входа в {self._providers[pid].display_name}. Войдите в аккаунт.")
            if not tokens.is_expired():
                return tokens.access_token
            log.info("Обновляем токен %s", pid.value)
            try:
                fresh = await self._providers[pid].refresh(tokens)
            except AuthError:
                self._store.clear(pid.value)
                self._tokens.pop(pid, None)
                raise
            self._tokens[pid] = fresh
            self._store.set(pid.value, fresh.to_dict())
            return fresh.access_token

    async def logout(self, pid: ProviderId) -> None:
        self._tokens.pop(pid, None)
        self._names.pop(pid, None)
        self._store.clear(pid.value)
        if self._bus is not None:
            self._bus.logged_out.emit(pid.value)
