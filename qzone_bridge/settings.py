"""Plugin settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _as_mapping(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    if hasattr(config, "items"):
        try:
            return dict(config.items())
        except Exception:
            pass
    if hasattr(config, "model_dump"):
        try:
            return dict(config.model_dump())
        except Exception:
            pass
    if hasattr(config, "__dict__"):
        return {k: v for k, v in vars(config).items() if not k.startswith("_")}
    return {}


def _pick(mapping: dict[str, Any], key: str, default: Any) -> Any:
    if key in mapping:
        return mapping[key]
    nested = mapping.get("qzone")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return default


@dataclass(slots=True)
class PluginSettings:
    daemon_port: int = 18999
    keepalive_interval: int = 120
    request_timeout: float = 15.0
    start_timeout: float = 20.0
    public_feed_limit: int = 5
    max_feed_limit: int = 20
    auto_start_daemon: bool = True
    admin_uins: list[int] = field(default_factory=list)
    user_agent: str = DEFAULT_USER_AGENT
    preview_writes: bool = True

    @classmethod
    def from_mapping(cls, config: Any) -> "PluginSettings":
        mapping = _as_mapping(config)
        admin_uins = _pick(mapping, "admin_uins", [])
        if isinstance(admin_uins, str):
            admin_uins = [int(item.strip()) for item in admin_uins.split(",") if item.strip().isdigit()]
        if not isinstance(admin_uins, list):
            admin_uins = []
        return cls(
            daemon_port=int(_pick(mapping, "daemon_port", 18999) or 18999),
            keepalive_interval=int(_pick(mapping, "keepalive_interval", 120) or 120),
            request_timeout=float(_pick(mapping, "request_timeout", 15.0) or 15.0),
            start_timeout=float(_pick(mapping, "start_timeout", 20.0) or 20.0),
            public_feed_limit=int(_pick(mapping, "public_feed_limit", 5) or 5),
            max_feed_limit=int(_pick(mapping, "max_feed_limit", 20) or 20),
            auto_start_daemon=bool(_pick(mapping, "auto_start_daemon", True)),
            admin_uins=[int(v) for v in admin_uins if str(v).isdigit()],
            user_agent=str(_pick(mapping, "user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT),
            preview_writes=bool(_pick(mapping, "preview_writes", True)),
        )
