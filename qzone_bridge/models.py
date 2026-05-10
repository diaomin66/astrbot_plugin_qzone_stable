"""Dataclasses used by the daemon and plugin."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class FeedEntry:
    hostuin: int
    fid: str
    appid: int
    summary: str
    nickname: str = ""
    created_at: int = 0
    like_count: int = 0
    comment_count: int = 0
    liked: bool = False
    curkey: str = ""
    unikey: str = ""
    busi_param: dict[str, Any] = field(default_factory=dict)
    topic_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class SessionState:
    uin: int = 0
    cookies: dict[str, str] = field(default_factory=dict)
    qzonetokens: dict[str, str] = field(default_factory=dict)
    source: str = "manual"
    updated_at: str = ""
    last_ok_at: str = ""
    last_error: dict[str, Any] | None = None
    revision: int = 0
    needs_rebind: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionState":
        data = data or {}
        return cls(
            uin=int(data.get("uin") or 0),
            cookies=dict(data.get("cookies") or {}),
            qzonetokens=dict(data.get("qzonetokens") or {}),
            source=str(data.get("source") or "manual"),
            updated_at=str(data.get("updated_at") or ""),
            last_ok_at=str(data.get("last_ok_at") or ""),
            last_error=data.get("last_error"),
            revision=int(data.get("revision") or 0),
            needs_rebind=bool(data.get("needs_rebind") or False),
        )


@dataclass(slots=True)
class RuntimeState:
    daemon_pid: int = 0
    daemon_port: int = 0
    secret: str = ""
    started_at: str = ""
    last_seen_at: str = ""
    version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuntimeState":
        data = data or {}
        return cls(
            daemon_pid=int(data.get("daemon_pid") or 0),
            daemon_port=int(data.get("daemon_port") or 0),
            secret=str(data.get("secret") or ""),
            started_at=str(data.get("started_at") or ""),
            last_seen_at=str(data.get("last_seen_at") or ""),
            version=str(data.get("version") or "0.1.0"),
        )


@dataclass(slots=True)
class BridgeState:
    version: int = 1
    session: SessionState = field(default_factory=SessionState)
    runtime: RuntimeState = field(default_factory=RuntimeState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session": self.session.to_dict(),
            "runtime": self.runtime.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BridgeState":
        data = data or {}
        return cls(
            version=int(data.get("version") or 1),
            session=SessionState.from_dict(data.get("session")),
            runtime=RuntimeState.from_dict(data.get("runtime")),
        )


@dataclass(slots=True)
class ApiError:
    code: str
    message: str
    detail: Any = None


@dataclass(slots=True)
class ApiResult:
    ok: bool
    data: Any = None
    error: ApiError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "data": self.data, "meta": self.meta}
        if self.error:
            payload["error"] = {
                "code": self.error.code,
                "message": self.error.message,
                "detail": self.error.detail,
            }
        return payload

