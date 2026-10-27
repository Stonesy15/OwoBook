"""Shared HTTP helper: timeouts plus retries with exponential backoff for 429 and 5xx."""

import asyncio
import logging
import random

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HTTPError(Exception):
    """A request failed for good (after retries, or a non-retryable status)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 20.0)
        except ValueError:
            pass
    return min(0.5 * 2**attempt, 8.0) + random.uniform(0, 0.3)


async def request_with_retries(
    client: httpx.AsyncClient, method: str, url: str, *, retries: int = 3, label: str = "", **kwargs
) -> httpx.Response:
    """Send a request; retry on network errors, 429 and 5xx. Raises HTTPError on final failure."""
    label = label or url
    for attempt in range(retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == retries:
                raise HTTPError(f"{label}: network error after {retries + 1} tries: {exc!r}") from exc
            wait = _backoff(attempt, None)
            log.warning("%s: %r — retrying in %.1fs", label, exc, wait)
            await asyncio.sleep(wait)
            continue

        if resp.status_code < 400:
            return resp
        body = resp.text[:500]
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            wait = _backoff(attempt, resp.headers.get("retry-after"))
            log.warning("%s: HTTP %s — retrying in %.1fs", label, resp.status_code, wait)
            await asyncio.sleep(wait)
            continue
        raise HTTPError(f"{label}: HTTP {resp.status_code}: {body}", status=resp.status_code)
    raise HTTPError(f"{label}: gave up")  # unreachable
