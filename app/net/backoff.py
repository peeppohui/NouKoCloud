"""Экспоненциальная задержка с джиттером.

Яндекс и Google возвращают 429/503 с Retry-After; если его нет —
используем backoff 1.5с -> 3с -> 6с ... с разбросом, чтобы запросы
от нескольких монтажёров не били по серверу синхронно.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    base_delay: float = 1.5
    max_delay: float = 90.0
    jitter: float = 0.35
    retry_statuses: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)


def compute_delay(attempt: int, retry_after: float | None = None, policy: RetryPolicy | None = None) -> float:
    """Задержка перед следующей попыткой (секунды)."""
    policy = policy or RetryPolicy()
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, policy.max_delay)
    raw = policy.base_delay * (2 ** max(0, attempt - 1))
    raw = min(raw, policy.max_delay)
    spread = raw * policy.jitter
    return max(0.1, raw + random.uniform(-spread, spread))


def parse_retry_after(response: httpx.Response) -> float | None:
    """Читает Retry-After (секунды или HTTP-дата)."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        if target is None:
            return None
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (target - now).total_seconds())
    except Exception:
        return None


async def run_with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, float, BaseException | None], None] | None = None,
    reraise: type[BaseException] | None = None,
) -> T:
    """Выполняет корутину с повторами при сетевых ошибках и 429/5xx."""
    policy = policy or RetryPolicy()
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await factory()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in policy.retry_statuses:
                raise
            last_exc = exc
            delay = compute_delay(attempt, parse_retry_after(exc.response), policy)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            delay = compute_delay(attempt, None, policy)
        except Exception as exc:
            if reraise and isinstance(exc, reraise):
                raise
            last_exc = exc
            delay = compute_delay(attempt, None, policy)
        if attempt >= policy.max_attempts:
            break
        log.warning("Повтор %d/%d через %.1f с: %s", attempt, policy.max_attempts, delay, last_exc)
        if on_retry is not None:
            on_retry(attempt, delay, last_exc)
        await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
