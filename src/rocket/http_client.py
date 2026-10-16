"""Synchronous HTTP client built on httpx + tenacity.
- httpx handles connections, timeouts, TLS verification, streaming reads, and built-in
  redirect following (up to 5);
- tenacity handles bounded retries and backoff (network errors, 429, 5xx) and honors Retry-After;
- only HTTPS is allowed; auth headers are scoped per destination host: GITHUB_TOKEN is sent
  only to api.github.com.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
)

from .errors import (
    HTTPStatusError,
    NetworkError,
    RedirectError,
    RetryableHTTPError,
    UnsafeURLError,
)

_logger = logging.getLogger("rocket.http")

_MAX_REDIRECTS = 5
_RETRY_AFTER_CAP_SECONDS = 60.0
_BACKOFF_CAP_SECONDS = 8.0
_SNIPPET_LIMIT = 300


class HTTPClient:
    """Synchronous HTTP client; API requests and file downloads share one instance."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        github_token: str | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.retries = max(0, retries)
        self._github_token = github_token
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            event_hooks={"response": [self._reject_insecure_redirect]},
        )
        self._retryer = Retrying(
            retry=retry_if_exception_type((NetworkError, RetryableHTTPError)),
            stop=stop_after_attempt(self.retries + 1),
            wait=self._wait,
            sleep=sleep,
            reraise=True,
            before_sleep=before_sleep_log(_logger, logging.WARNING),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HTTPClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_json(self, url: str) -> Any:
        """GET and parse JSON; raise on non-200 or invalid structure."""
        response = self._retryer(self._get_once, url, accept="application/json", stream=False)
        try:
            try:
                return response.json()
            except ValueError as exc:
                raise HTTPStatusError(
                    response.status_code,
                    f"response is not valid JSON: {url}",
                    hint=(
                        "the upstream response contract may have changed; "
                        "update the adapter instead of guessing"
                    ),
                ) from exc
        finally:
            response.close()

    @contextmanager
    def stream(self, url: str) -> Generator[httpx.Response]:
        """GET and stream a 200 response; the caller iterates and this method closes it."""
        response = self._retryer(self._get_once, url, accept="*/*", stream=True)
        try:
            yield response
        finally:
            response.close()

    # ------------------------------------------------------------------
    # internal implementation
    # ------------------------------------------------------------------

    def _get_once(self, url: str, *, accept: str, stream: bool) -> httpx.Response:
        """One request attempt (includes httpx built-in redirect following); retryable errors
        are raised to tenacity."""
        _require_https(url)
        request = self._client.build_request("GET", url, headers=self._headers_for(url, accept))
        try:
            response = self._client.send(request, stream=stream)
        except httpx.TooManyRedirects as exc:
            raise RedirectError(
                f"more than {_MAX_REDIRECTS} redirects or a redirect loop: {url}",
                hint="the upstream entry may point to a redirect loop",
            ) from exc
        except httpx.TransportError as exc:
            raise NetworkError(
                f"network error ({exc.__class__.__name__}): {url}",
                hint="check your network connection or proxy settings",
            ) from exc

        if response.status_code == 200:
            return response

        status = response.status_code
        message = self._error_message(response, url)
        if status == 429 or status >= 500:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            response.close()
            raise RetryableHTTPError(
                status, message, retry_after=retry_after, hint=_status_hint(status)
            )
        response.close()
        raise HTTPStatusError(status, message, hint=_status_hint(status))

    @staticmethod
    def _error_message(response: httpx.Response, url: str) -> str:
        snippet = _body_snippet(response)
        message = f"HTTP {response.status_code}: {url}"
        if snippet:
            message = f"{message} — {snippet}"
        return message

    def _headers_for(self, url: str, accept: str) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        host = (urlparse(url).hostname or "").lower()
        if self._github_token and host == "api.github.com":
            headers["Authorization"] = f"Bearer {self._github_token}"
        return headers

    @staticmethod
    def _reject_insecure_redirect(response: httpx.Response) -> None:
        """httpx response event hook: refuse to follow redirects to a non-HTTPS URL.

        Note: ``response.next_request`` is not set yet when the hook runs, so parse
        the ``Location`` header and resolve the target against the current response URL.
        """
        if not response.has_redirect_location:
            return
        location = response.headers.get("location", "")
        target = urljoin(str(response.url), location)
        if urlparse(target).scheme.lower() != "https":
            raise UnsafeURLError(
                f"refusing to follow a redirect to a non-HTTPS URL: {target}",
                hint="only HTTPS is accepted",
            )

    def _wait(self, state: RetryCallState) -> float:
        outcome = state.outcome
        exception = outcome.exception() if outcome is not None else None
        if isinstance(exception, RetryableHTTPError) and exception.retry_after is not None:
            return max(0.0, min(exception.retry_after, _RETRY_AFTER_CAP_SECONDS))
        return min(0.5 * (2 ** (state.attempt_number - 1)), _BACKOFF_CAP_SECONDS)


def _require_https(url: str) -> None:
    scheme = urlparse(url).scheme.lower()
    if scheme != "https":
        raise UnsafeURLError(
            f"only HTTPS URLs are allowed; got {scheme or 'unknown'} scheme: {url}",
            hint="file/data/ftp/http and other schemes are not supported",
        )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After (seconds or an HTTP date) and cap it at a reasonable wait time."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, _RETRY_AFTER_CAP_SECONDS))


def _body_snippet(response: httpx.Response) -> str:
    """Read a small snippet of the error response body for diagnostics; never dump the full
    response."""
    if response.is_closed:
        return ""
    try:
        return _truncate(response.text)
    except httpx.ResponseNotRead:
        pass
    except httpx.HTTPError:
        return ""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= _SNIPPET_LIMIT:
                break
    except httpx.HTTPError:
        pass
    return _truncate(b"".join(chunks).decode("utf-8", errors="replace"))


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _SNIPPET_LIMIT:
        return text[:_SNIPPET_LIMIT] + "..."
    return text


def _status_hint(status: int) -> str | None:
    if status == 404:
        return (
            "resource not found; check the declared version/tag/project coordinates; "
            "no downgrades or guessing"
        )
    if status in (401, 403):
        return (
            "permission denied or request rejected; check the declaration and network environment"
        )
    if status == 429:
        return "rate limited; retries already applied per policy"
    if status >= 500:
        return "upstream service temporarily unavailable; retries already applied per policy"
    return None
