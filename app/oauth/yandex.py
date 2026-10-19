"""OAuth Яндекса (консольное приложение: код подтверждения вводит пользователь).

Сверено с официальной документацией (https://yandex.ru/dev/id/doc/en/):
* адрес выдачи кода — `https://oauth.yandex.com/verification_code`;
  именно его надо указать в поле «Redirect URI» приложения
  (для приложений «For API access» он задан жёстко и не меняется);
* код подтверждения — **7 цифр**, живёт 10 минут;
* если `scope` не передавать, токен выдаётся с теми правами,
  которые заданы при регистрации приложения (так надёжнее всего);
* ошибки приходят в формате `{"error": "...", "error_description": "..."}`;
* обновление токена — `grant_type=refresh_token` на том же `/token`.

Код вводится вручную — это штатный сценарий для консольных приложений
(см. раздел «Receiving a confirmation code from the user» в документации).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx

from ..core.errors import AuthError
from ..core.models import ProviderId
from .base import OAuthProvider, OAuthTokens

log = logging.getLogger(__name__)

AUTH_URL = "https://oauth.yandex.com/authorize"
TOKEN_URL = "https://oauth.yandex.com/token"
INFO_URL = "https://login.yandex.ru/info?format=json"

#: Куда Яндекс перенаправляет пользователя, показав код.
REDIRECT_URIS = (
    "https://oauth.yandex.com/verification_code",
    "https://oauth.yandex.ru/verification_code",
    "https://oauth.yandex.ru/redirect",
)

#: Права по умолчанию НЕ запрашиваем: Яндекс выдаст те, что заданы
#: при регистрации приложения (иначе можно нарваться на invalid_scope).
DEFAULT_SCOPES: tuple[str, ...] = ()

_ERROR_TEXT = {
    "invalid_grant": "Код не подошёл: он мог истечь (код живёт 10 минут). "
                     "Попросите новый код в браузере.",
    "bad_verification_code": "Код введён неверно — Яндекс ожидает 7 цифр.",
    "invalid_client": "Ключи приложения неверны. Проверьте Client ID и Client Secret в настройках.",
    "invalid_scope": "У приложения нет прав на Яндекс.Диск. "
                     "Включите «Чтение диска» в настройках приложения на oauth.yandex.com.",
    "unauthorized_client": "Приложение отклонено или ждёт проверки модератором.",
    "invalid_request": "Яндекс отклонил запрос. Проверьте ключи приложения.",
}


class YandexOAuth(OAuthProvider):
    id = ProviderId.YANDEX
    display_name = "Яндекс.Диск"
    auth_url = AUTH_URL
    token_url = TOKEN_URL

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        device_id: str = "",
        device_name: str = "svsCloud",
    ) -> None:
        super().__init__(client_id, client_secret)
        self.scopes = scopes
        self.device_id = device_id
        self.device_name = device_name

    # ------------------------------------------------------------------
    def authorization_url(self, redirect_uri: str = REDIRECT_URIS[0], state: str = "") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        if self.device_id:
            params["device_id"] = self.device_id
            params["device_name"] = self.device_name
        if state:
            params["state"] = state
        return f"{AUTH_URL}?{urlencode(params)}"

    # ------------------------------------------------------------------
    async def exchange_code(self, code: str, redirect_uri: str = REDIRECT_URIS[0]) -> OAuthTokens:
        code = code.strip()
        if not code:
            raise AuthError("Код авторизации пустой. Скопируйте его со страницы Яндекса.")
        if not code.isdigit():
            raise AuthError(
                "Код авторизации — это 7 цифр. Скопируйте его целиком со страницы Яндекса."
            )

        # Redirect URI должен совпадать с тем, что был указан при авторизации.
        # Пробуем основной, а на всякий случай и запасные.
        candidates = [redirect_uri] + [uri for uri in REDIRECT_URIS if uri != redirect_uri]
        last_error: Exception | None = None
        for uri in candidates:
            try:
                return await self._exchange(code, uri)
            except AuthError as exc:
                last_error = exc
                log.info("Обмен кода не удался с %s: %s", uri, exc.technical)
        raise last_error or AuthError("Яндекс не принял код авторизации.")

    async def _exchange(self, code: str, redirect_uri: str) -> OAuthTokens:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            raise self._error(response)
        payload = response.json()
        return OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=time.time() + float(payload.get("expires_in") or 3600),
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
        )

    def _error(self, response: httpx.Response) -> AuthError:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        code = str(payload.get("error") or "")
        description = str(payload.get("error_description") or "")
        text = _ERROR_TEXT.get(code)
        if text is None:
            text = description or "Яндекс не принял код авторизации."
        return AuthError(text, technical=f"{response.status_code} {code}: {description}")

    # ------------------------------------------------------------------
    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        if not tokens.refresh_token:
            raise AuthError("Нет сохранённого refresh-токена. Войдите заново.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            raise self._error(response)
        payload = response.json()
        return OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", tokens.refresh_token),
            expires_at=time.time() + float(payload.get("expires_in") or 3600),
            scope=payload.get("scope", tokens.scope),
            token_type=payload.get("token_type", "Bearer"),
        )

    # ------------------------------------------------------------------
    async def account_info(self, tokens: OAuthTokens) -> str:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    INFO_URL, headers={"Authorization": f"OAuth {tokens.access_token}"}
                )
            if response.status_code != 200:
                return "Яндекс.Диск"
            data = response.json()
            return data.get("default_email") or data.get("login") or data.get("display_name") or "Яндекс.Диск"
        except Exception:
            return "Яндекс.Диск"
