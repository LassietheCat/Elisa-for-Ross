from __future__ import annotations


class SiteError(Exception):
    code = "site_error"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or self.__class__.__name__)
        if code:
            self.code = code


class LoginFailed(SiteError):
    code = "login_failed"


class InvalidCredentials(LoginFailed):
    code = "invalid_credentials"


class VerificationRequired(LoginFailed):
    code = "verification_required"


class NavigationFailed(SiteError):
    code = "navigation_failed"


class CalendarNotRendered(SiteError):
    code = "calendar_not_rendered"
