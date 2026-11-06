"""Define package errors."""

from __future__ import annotations

from ..runtime import ConfigEntryAuthFailed, HomeAssistantError



class MbapiError(HomeAssistantError):
    """Define a base error."""


class WebsocketError(MbapiError):
    """Define an error related to generic websocket errors."""


class RequestError(MbapiError):
    """Define an error related to generic websocket errors."""


class MBAuthError(ConfigEntryAuthFailed):
    """Define an error related to authentication."""


class MBAuth2FAError(ConfigEntryAuthFailed):
    """Define an error related to two-factor authentication (2FA)."""


class MBLegalTermsError(ConfigEntryAuthFailed):
    """Define an error related to acceptance of legal terms."""


class MercedesRateLimitError(Exception):
    """Safe rate-limit signal; never retain provider bodies or authorization headers."""
    status = 429

    def __init__(self, retry_after=""):
        import time
        from email.utils import parsedate_to_datetime
        self.retry_after_until = 0
        try:
            self.retry_after_until = time.time() + max(0, float(retry_after))
        except (TypeError, ValueError):
            try:
                self.retry_after_until = parsedate_to_datetime(retry_after).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
        super().__init__("Mercedes rate limited the request.")
