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


def _nested(mapping: dict[str, Any], section: str, key: str, default: Any) -> Any:
    section_value = mapping.get(section)
    if isinstance(section_value, dict) and key in section_value:
        return section_value[key]
    nested = mapping.get("qzone")
    if isinstance(nested, dict):
        section_value = nested.get(section)
        if isinstance(section_value, dict) and key in section_value:
            return section_value[key]
    return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


@dataclass(slots=True)
class PluginSettings:
    daemon_port: int = 18999
    keepalive_interval: int = 120
    request_timeout: float = 15.0
    start_timeout: float = 20.0
    public_feed_limit: int = 5
    max_feed_limit: int = 20
    auto_start_daemon: bool = True
    auto_bind_cookie: bool = True
    cookie_domain: str = "user.qzone.qq.com"
    admin_uins: list[int] = field(default_factory=list)
    user_agent: str = DEFAULT_USER_AGENT
    preview_writes: bool = True
    render_publish_result: bool = True
    render_result_width: int = 900
    render_remote_timeout: float = 0.35
    manage_group: int = 0
    pillowmd_style_dir: str = ""
    post_provider_id: str = ""
    post_prompt: str = "找出一个你感兴趣的主题来写一段适合 QQ 空间的说说，简短、有个性、不要解释。"
    comment_provider_id: str = ""
    comment_prompt: str = "生成一句简短、直接、贴题的评论，不要解释。"
    reply_provider_id: str = ""
    reply_prompt: str = "这条帖子收到了一条评论，请自然回复此条评论，不要解释。"
    ignore_groups: list[str] = field(default_factory=list)
    ignore_users: list[str] = field(default_factory=list)
    post_max_msg: int = 500
    publish_cron: str = ""
    publish_offset: int = 0
    comment_cron: str = ""
    comment_offset: int = 0
    read_prob: float = 0.0
    send_admin: bool = False
    like_when_comment: bool = False
    cookies_str: str = ""
    show_name: bool = True

    @classmethod
    def from_mapping(cls, config: Any) -> "PluginSettings":
        mapping = _as_mapping(config)
        admin_uins = _pick(mapping, "admin_uins", [])
        if not admin_uins:
            admin_uins = _pick(mapping, "admins_id", [])
        if isinstance(admin_uins, str):
            admin_uins = [int(item.strip()) for item in admin_uins.split(",") if item.strip().isdigit()]
        if not isinstance(admin_uins, list):
            admin_uins = []
        timeout = _pick(mapping, "timeout", None)
        return cls(
            daemon_port=int(_pick(mapping, "daemon_port", 18999) or 18999),
            keepalive_interval=int(_pick(mapping, "keepalive_interval", 120) or 120),
            request_timeout=float(_pick(mapping, "request_timeout", timeout if timeout is not None else 15.0) or 15.0),
            start_timeout=float(_pick(mapping, "start_timeout", 20.0) or 20.0),
            public_feed_limit=int(_pick(mapping, "public_feed_limit", 5) or 5),
            max_feed_limit=int(_pick(mapping, "max_feed_limit", 20) or 20),
            auto_start_daemon=_as_bool(_pick(mapping, "auto_start_daemon", True), True),
            auto_bind_cookie=_as_bool(_pick(mapping, "auto_bind_cookie", True), True),
            cookie_domain=str(_pick(mapping, "cookie_domain", "user.qzone.qq.com") or "user.qzone.qq.com").strip()
            or "user.qzone.qq.com",
            admin_uins=[int(v) for v in admin_uins if str(v).isdigit()],
            user_agent=str(_pick(mapping, "user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT),
            preview_writes=_as_bool(_pick(mapping, "preview_writes", True), True),
            render_publish_result=_as_bool(_pick(mapping, "render_publish_result", True), True),
            render_result_width=int(_pick(mapping, "render_result_width", 900) or 900),
            render_remote_timeout=float(_pick(mapping, "render_remote_timeout", 0.35) or 0.35),
            manage_group=int(_pick(mapping, "manage_group", 0) or 0),
            pillowmd_style_dir=str(_pick(mapping, "pillowmd_style_dir", "") or ""),
            post_provider_id=str(_nested(mapping, "llm", "post_provider_id", "") or ""),
            post_prompt=str(_nested(mapping, "llm", "post_prompt", cls.post_prompt) or cls.post_prompt),
            comment_provider_id=str(_nested(mapping, "llm", "comment_provider_id", "") or ""),
            comment_prompt=str(_nested(mapping, "llm", "comment_prompt", cls.comment_prompt) or cls.comment_prompt),
            reply_provider_id=str(_nested(mapping, "llm", "reply_provider_id", "") or ""),
            reply_prompt=str(_nested(mapping, "llm", "reply_prompt", cls.reply_prompt) or cls.reply_prompt),
            ignore_groups=[str(item) for item in _as_list(_nested(mapping, "source", "ignore_groups", []))],
            ignore_users=[str(item) for item in _as_list(_nested(mapping, "source", "ignore_users", []))],
            post_max_msg=int(_nested(mapping, "source", "post_max_msg", 500) or 500),
            publish_cron=str(_nested(mapping, "trigger", "publish_cron", "") or ""),
            publish_offset=int(
                _nested(
                    mapping,
                    "trigger",
                    "publish_offset",
                    _nested(mapping, "trigger", "publish_offset_minutes", 0),
                )
                or 0
            ),
            comment_cron=str(_nested(mapping, "trigger", "comment_cron", "") or ""),
            comment_offset=int(
                _nested(
                    mapping,
                    "trigger",
                    "comment_offset",
                    _nested(mapping, "trigger", "comment_offset_minutes", 0),
                )
                or 0
            ),
            read_prob=float(_nested(mapping, "trigger", "read_prob", 0.0) or 0.0),
            send_admin=_as_bool(_nested(mapping, "trigger", "send_admin", False), False),
            like_when_comment=_as_bool(_nested(mapping, "trigger", "like_when_comment", False), False),
            cookies_str=str(_pick(mapping, "cookies_str", "") or ""),
            show_name=_as_bool(_pick(mapping, "show_name", True), True),
        )
