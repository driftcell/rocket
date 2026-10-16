"""Error types for rocket.

All user-facing failures derive from :class:`RocketError`, carrying an error
category and an optional remediation hint.
Exit code convention: config errors use 2, all other failures use 1.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


class RocketError(Exception):
    """Base class for all expected failures."""

    category = "Error"
    exit_code = EXIT_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def format(self) -> str:
        """Return a one-line error description for the user."""
        if self.hint:
            return f"{self.category}: {self.message} (hint: {self.hint})"
        return f"{self.category}: {self.message}"


class ConfigError(RocketError):
    """Config type/field errors, unknown provider or resolver."""

    category = "Config error"
    exit_code = EXIT_CONFIG


class ProviderError(RocketError):
    """Provider failed to query a project or version, or check compatibility."""

    category = "Provider error"


class ResolutionError(RocketError):
    """External link resolution or attachment selection failed."""

    category = "Resolution error"


class ExternalNotFollowedError(ResolutionError):
    """follow_external=false rejected the external entry."""

    category = "External link error"


class NetworkError(RocketError):
    """Network error or timeout (retryable)."""

    category = "Network error"


class UnsafeURLError(RocketError):
    """Non-HTTPS or unsupported scheme (not retried)."""

    category = "URL error"


class RedirectError(RocketError):
    """Redirect loop or too many redirects (not retried)."""

    category = "Redirect error"


class HTTPStatusError(RocketError):
    """Non-success HTTP status."""

    category = "HTTP error"

    def __init__(self, status_code: int, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code


class RetryableHTTPError(HTTPStatusError):
    """429 or 5xx, handled by the retry policy."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retry_after: float | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(status_code, message, hint=hint)
        self.retry_after = retry_after


class StreamInterruptedError(NetworkError):
    """Network interrupted during a streaming download (whole-file retry allowed)."""


class DownloadError(RocketError):
    """Local write or download pipeline failed."""

    category = "Download error"


class ValidationError(RocketError):
    """Size/hash mismatch or invalid JAR."""

    category = "Validation error"


class LockError(RocketError):
    """rocket.lock could not be written."""

    category = "Lock error"
