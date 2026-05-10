"""HTTP envelope helpers for plugin <-> daemon communication."""

from __future__ import annotations

from aiohttp import web

from .models import ApiError, ApiResult

SECRET_HEADER = "X-Qzone-Secret"


def ok(data=None, *, meta=None) -> web.Response:
    return web.json_response(ApiResult(ok=True, data=data, meta=meta or {}).to_dict())


def fail(code: str, message: str, *, detail=None, status: int = 400) -> web.Response:
    return web.json_response(
        ApiResult(ok=False, error=ApiError(code=code, message=message, detail=detail)).to_dict(),
        status=status,
    )

