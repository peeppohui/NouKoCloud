"""Базовые типы для OAuth 2.0."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..core.models import ProviderId


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0
    scope: str = ""
    token_type: str = "Bearer"
    extra: dict = field(default_factory=dict)

    def is_expired(self, skew: float = 120.0) -> bool:
        return self.expires_at > 0 and time.time() + skew >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthTokens":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0) or 0.0),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
            extra=data.get("extra", {}) or {},
        )


class OAuthProvider(ABC):
    """Описание конкретного OAuth-сервиса."""

    id: ProviderId
    display_name: str
    auth_url: str
    token_url: str
    scopes: tuple[str, ...] = ()

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @abstractmethod
    def authorization_url(self, redirect_uri: str, state: str) -> str:
        ...

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthTokens:
        ...

    @abstractmethod
    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        ...

    @abstractmethod
    async def account_info(self, tokens: OAuthTokens) -> str:
        """Человекочитаемое имя аккаунта (почта/логин)."""
        ...
