"""Понятные пользователю ошибки.

В UI не должно быть стектрейсов: наружу отдаётся короткая фраза
(«Файл не скачан: превышен лимит запросов»), а детали уходят в лог.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)


class AppError(Exception):
    """Базовая ошибка приложения с сообщением для пользователя."""

    user_message = "Что-то пошло не так. Попробуйте ещё раз."

    def __init__(self, user_message: str | None = None, *, technical: str | None = None):
        self.user_message = user_message or self.user_message
        self.technical = technical or self.user_message
        super().__init__(self.technical)

    def __str__(self) -> str:  # pragma: no cover - только для логов
        return self.technical


class AuthError(AppError):
    user_message = "Не удалось войти. Проверьте, что приложение открыто в браузере."


class TokenExpiredError(AuthError):
    user_message = "Сеанс входа истёк. Войдите заново."


class PermissionDeniedError(AppError):
    user_message = "Нет доступа к этому файлу. Попросите владельца открыть его для вашего аккаунта."


class NotFoundError(AppError):
    user_message = "Файл не найден. Возможно, его удалили или переместили."


class RateLimitError(AppError):
    user_message = "Слишком много запросов к облаку. Подождём и продолжим."

    def __init__(self, user_message: str | None = None, *, retry_after: float | None = None, technical: str | None = None):
        super().__init__(user_message, technical=technical)
        self.retry_after = retry_after


class QuotaError(AppError):
    user_message = "Закончилась квота запросов. Попробуйте позже."


class NetworkError(AppError):
    user_message = "Нет связи с облаком. Проверьте интернет."


class ServerError(AppError):
    user_message = "Облако временно сбоит. Повторим автоматически."


class UnsupportedFormatError(AppError):
    user_message = "Этот формат нельзя скачать напрямую."


class BadLinkError(AppError):
    user_message = "Не удалось разобрать ссылку. Проверьте, что она ведёт на папку или файл."


_STATUS_TEXT = {
    400: "Облако отклонило запрос. Возможно, ссылка устарела.",
    401: "Сеанс входа истёк. Войдите заново.",
    403: "Нет доступа к этому файлу.",
    404: "Файл не найден. Возможно, его удалили или переместили.",
    409: "Файл уже скачивается или изменился на сервере.",
    412: "Файл на сервере изменился. Начнём заново.",
    416: "Сервер не поддержал докачку. Начнём файл заново.",
    429: "Слишком много запросов к облаку. Подождём и продолжим.",
    500: "Облако временно сбоит. Повторим автоматически.",
    502: "Облако временно сбоит. Повторим автоматически.",
    503: "Облако временно сбоит. Повторим автоматически.",
    504: "Облако отвечает слишком долго. Повторим автоматически.",
}

# Тексты, которые облака возвращают в теле ответа.
_BODY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"оборвал передачу|оборвал соединение", re.I),
     "Облако оборвало передачу файла. Попробуйте ещё раз — или скачайте его через браузер."),
    (re.compile(r"DiskNotFoundError|NotFoundError|Ресурс не найден", re.I),
     "Файл не найден. Возможно, его удалили, переместили или закрыли доступ."),
    (re.compile(r"UnauthorizedError|need authorization|требуется авторизация", re.I),
     "Нужен вход в аккаунт. Войдите и повторите."),
    (re.compile(r"quota\s*exceeded", re.I), "Достигнут дневной лимит запросов к Google. Зайдите позже."),
    (re.compile(r"rateLimitExceeded", re.I), "Облако временно ограничило частоту запросов. Подождём и продолжим."),
    (re.compile(r"userRateLimitExceeded", re.I), "Облако временно ограничило запросы для вашего аккаунта. Подождём и продолжим."),
    (re.compile(r"rate limit exceeded", re.I), "Облако временно ограничило частоту запросов. Подождём и продолжим."),
    (re.compile(r"dailyLimitExceeded", re.I), "Достигнут дневной лимит запросов к Google. Зайдите позже."),
    (re.compile(r"quotaExceeded", re.I), "Достигнут дневной лимит запросов к Google. Зайдите позже."),
    (re.compile(r"storageQuotaExceeded", re.I), "На диске закончилось место. Освободите его и попробуйте снова."),
    (re.compile(r"insufficientPermissions", re.I), "Нет доступа к этому файлу. Попросите владельца открыть его вам."),
    (re.compile(r"fileNotFound", re.I), "Файл не найден. Возможно, его удалили или переместили."),
    (re.compile(r"notFound", re.I), "Файл не найден. Возможно, его удалили или переместили."),
    (re.compile(r"Resource not found", re.I), "Файл не найден. Возможно, его удалили или переместили."),
    (re.compile(r"disc is full", re.I), "На диске закончилось место. Освободите его и попробуйте снова."),
    (re.compile(r"camelCase", re.I), "Облако отклонило запрос. Попробуйте ещё раз."),
    (re.compile(r"unavailable", re.I), "Облако временно недоступно. Повторим автоматически."),
    (re.compile(r"timeout", re.I), "Облако отвечает слишком долго. Повторим автоматически."),
    (re.compile(r"Entity too large", re.I), "Файл слишком большой для скачивания через облако."),
    (re.compile(r"not supported", re.I), "Этот файл нельзя скачать напрямую через облако."),
]


def _extract_body_message(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    try:
        payload: Any = response.json()
    except Exception:
        try:
            return response.text[:400]
        except Exception:
            return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [str(error.get("message", "")), str(error.get("status", ""))]
            for reason in error.get("errors", []) or []:
                if isinstance(reason, dict):
                    parts.append(str(reason.get("reason", "")))
            payload = " ".join(p for p in parts if p)
        else:
            # Формат Яндекса: {"error": "...", "description": "...", "message": "..."}
            parts = [
                str(payload.get("message", "")),
                str(payload.get("description", "")),
                str(error or ""),
            ]
            payload = " ".join(p for p in parts if p)
    if isinstance(payload, str):
        return payload[:400]
    return json.dumps(payload, ensure_ascii=False)[:400]


def friendly_error(exc: BaseException, *, provider: str | None = None, response: httpx.Response | None = None) -> str:
    """Превращает любое исключение в короткое сообщение для пользователя."""
    if isinstance(exc, AppError):
        return exc.user_message

    if isinstance(exc, httpx.TimeoutException):
        return "Облако не отвечает вовремя. Повторим автоматически."
    if isinstance(exc, httpx.ConnectError):
        return "Нет связи с облаком. Проверьте интернет и повторите."
    if isinstance(exc, httpx.ProxyError):
        return "Проблема с прокси-сервером. Проверьте настройки сети."

    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        status = resp.status_code
        body = _extract_body_message(resp)
        for pattern, text in _BODY_PATTERNS:
            if pattern.search(body):
                return text
        if status == 401:
            return "Сеанс входа истёк. Войдите заново."
        if status == 403 and provider == "google" and "cannot be downloaded" in body.lower():
            return "Google не отдаёт этот файл напрямую — возможно, он защищён от скачивания."
        if status == 403 and provider == "yandex" and "not enough" in body.lower():
            return "У Яндекс.Диска закончилась полоса для этой ссылки. Попробуйте другой аккаунт."
        return _STATUS_TEXT.get(status, f"Ошибка облака (код {status}). Повторим автоматически.")

    if isinstance(exc, httpx.HTTPError):
        return "Нет связи с облаком. Проверьте интернет."

    text = str(exc)
    for pattern, message in _BODY_PATTERNS:
        if pattern.search(text):
            return message
    log.debug("Неопознанная ошибка: %r", exc, exc_info=True)
    return "Что-то пошло не так. Подробности в журнале."
