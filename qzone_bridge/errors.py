"""Error types for the QQ空间 bridge."""

from __future__ import annotations


class QzoneBridgeError(Exception):
    """Base error for the bridge."""

    def __init__(self, message: str, *, code: str = "QZONE_ERROR", detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail


class QzoneCookieAcquireError(QzoneBridgeError):
    def __init__(self, message: str = "无法获取 QQ 空间 Cookie", *, detail=None):
        super().__init__(message, code="QZONE_COOKIE_ACQUIRE", detail=detail)


class QzoneAuthError(QzoneBridgeError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, code="QZONE_AUTH", detail=detail)


class QzoneNeedsRebind(QzoneAuthError):
    def __init__(self, message: str = "QQ空间登录态已失效，需要重新绑定 Cookie", *, detail=None):
        super().__init__(message, detail=detail)


class QzoneRequestError(QzoneBridgeError):
    def __init__(self, message: str, *, status_code: int | None = None, detail=None):
        super().__init__(message, code="QZONE_REQUEST", detail=detail)
        self.status_code = status_code


class QzoneParseError(QzoneBridgeError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, code="QZONE_PARSE", detail=detail)


class QzoneCookieAcquireError(QzoneBridgeError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, code="QZONE_COOKIE", detail=detail)


class DaemonUnavailableError(QzoneBridgeError):
    def __init__(self, message: str = "Qzone daemon is unavailable", *, detail=None):
        super().__init__(message, code="DAEMON_UNAVAILABLE", detail=detail)
