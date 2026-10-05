"""Shared HTTP helper with retries for rate limits and server errors."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApiError(Exception):
    """A data source failed or is unavailable. The token is retried next cycle."""


class NotFound(ApiError):
    """The source answered but has no data for this request."""


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> Any:
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise ApiError(f"{url}: {exc}") from exc
        else:
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    raise ApiError(f"{url}: HTTP {resp.status_code}")
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            elif resp.status_code >= 400:
                raise ApiError(f"{url}: HTTP {resp.status_code} {resp.text[:200]}")
            else:
                return resp.json()
        log.debug("Retrying %s in %.0fs", url, delay)
        await asyncio.sleep(delay)
        delay *= 2
    raise ApiError(url)
