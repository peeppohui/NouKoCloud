"""OAuth Google (тип «installed app» — редирект на 127.0.0.1).

Поток: приложение поднимает локальный сервер на случайном порту,
открывает браузер, Google возвращает код на http://127.0.0.1:PORT/.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx

from ..core.errors import AuthError, ServerError
from ..core.models import ProviderId
from .base import OAuthProvider, OAuthTokens

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# drive.readonly хватает и на просмотр списка, и на скачивание.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "openid",
    "email",
)


DRIVE_SCOPE = DEFAULT_SCOPES[0]

NO_DRIVE_ACCESS = (
    "Google не дал программе доступ к Диску: на странице входа не отмечена галочка "
    "«Просмотр и скачивание всех файлов на Google Диске». Нажмите «Войти через "
    "Google Диск» ещё раз и отметьте её."
)


def has_drive_access(scope: str) -> bool:
    """Есть ли в выданных правах доступ к файлам Диска.

    Пустой scope считаем «неизвестно» (старые сохранённые входы) — не мешаем.
    """
    granted = set((scope or "").split())
    if not granted:
        return True
    return bool(granted & {DRIVE_SCOPE, "https://www.googleapis.com/auth/drive"})


class GoogleOAuth(OAuthProvider):
    id = ProviderId.GOOGLE
    display_name = "Google Диск"
    auth_url = AUTH_URL
    token_url = TOKEN_URL

    def __init__(self, client_id: str, client_secret: str, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> None:
        super().__init__(client_id, client_secret)
        self.scopes = scopes

    def authorization_url(self, redirect_uri: str, state: str = "") -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if state:
            params["state"] = state
        return f"{AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthTokens:
        data = {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            log.warning("Google отклонил код: %s %s", response.status_code, response.text[:300])
            raise AuthError("Google не принял код авторизации. Попробуйте войти ещё раз.",
                            technical=f"{response.status_code}: {response.text[:500]}")
        payload = response.json()
        tokens = OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=time.time() + float(payload.get("expires_in") or 3600),
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
        )
        if not has_drive_access(tokens.scope):
            # Google показывает права галочками: без отмеченного Диска вход
            # «успешен», но любой запрос к файлам — 403. Говорим сразу.
            log.warning("Google не выдал доступ к Диску: scope=%s", tokens.scope)
            raise AuthError(NO_DRIVE_ACCESS, technical=f"scope без Диска: {tokens.scope}")
        return tokens

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
        if response.status_code in (400, 401):
            # invalid_grant и т. п.: refresh-токен отозван — нужен новый вход.
            raise AuthError("Не удалось обновить вход в Google. Войдите заново.",
                            technical=f"{response.status_code}: {response.text[:500]}")
        if response.status_code != 200:
            # Сбой на стороне Google — вход не теряем, повторим позже.
            raise ServerError(technical=f"refresh Google: {response.status_code}: {response.text[:300]}")
        payload = response.json()
        return OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", tokens.refresh_token),
            expires_at=time.time() + float(payload.get("expires_in") or 3600),
            scope=payload.get("scope", tokens.scope),
            token_type=payload.get("token_type", "Bearer"),
        )

    async def account_info(self, tokens: OAuthTokens) -> str:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    INFO_URL, headers={"Authorization": f"Bearer {tokens.access_token}"}
                )
            if response.status_code != 200:
                return "Google Диск"
            data = response.json()
            return data.get("email") or data.get("name") or "Google Диск"
        except Exception:
            return "Google Диск"
