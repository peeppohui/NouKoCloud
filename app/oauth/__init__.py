"""OAuth 2.0 для Яндекс.Диска и Google Диска."""

from .base import OAuthProvider, OAuthTokens
from .google import GoogleOAuth
from .manager import OAuthManager
from .yandex import YandexOAuth

__all__ = [
    "OAuthProvider",
    "OAuthTokens",
    "GoogleOAuth",
    "YandexOAuth",
    "OAuthManager",
]
