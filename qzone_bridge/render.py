"""Text renderers for human and LLM-facing output."""

from __future__ import annotations

from collections.abc import Iterable

from .models import FeedEntry
from .utils import to_local_time_text, truncate


def cookie_summary(cookies: dict[str, str]) -> str:
    if not cookies:
        return "未绑定"
    keys = [
        "uin",
        "p_uin",
        "skey",
        "p_skey",
        "pt4_token",
        "pt_key",
        "qqmusic_key",
        "lvkey",
    ]
    found = [key for key in keys if key in cookies]
    extras = len(cookies) - len(found)
    return f"{len(cookies)}个字段: " + ", ".join(found + ([f"其他{extras}项"] if extras > 0 else []))


def format_status(status: dict) -> str:
    lines = [
        "QQ空间状态",
        f"- daemon: {status.get('daemon_state', 'unknown')}",
        f"- login: {status.get('login_uin') or '-'}",
        f"- cookie: {status.get('cookie_summary', '-')}",
        f"- needs_rebind: {status.get('needs_rebind', False)}",
        f"- last_ok: {status.get('last_ok_at') or '-'}",
        f"- last_error: {status.get('last_error', '-')}",
    ]
    if status.get("daemon_port"):
        lines.append(f"- endpoint: 127.0.0.1:{status['daemon_port']}")
    return "\n".join(lines)


def format_feed_entry(entry: FeedEntry, index: int | None = None) -> str:
    prefix = f"{index}. " if index is not None else "- "
    headline = truncate(entry.summary or "(empty)", 90)
    lines = [
        f"{prefix}{to_local_time_text(entry.created_at)} | {entry.nickname or entry.hostuin}",
        f"   fid={entry.fid} appid={entry.appid} like={entry.like_count} comment={entry.comment_count} liked={entry.liked}",
        f"   {headline}",
    ]
    return "\n".join(lines)


def format_feed_list(entries: Iterable[FeedEntry], *, cursor: str = "", has_more: bool = False) -> str:
    rendered = [format_feed_entry(entry, i + 1) for i, entry in enumerate(entries)]
    footer = []
    if cursor:
        footer.append(f"cursor={cursor}")
    footer.append(f"has_more={has_more}")
    body = "\n".join(rendered) if rendered else "(no feeds)"
    return "\n".join([body, *footer])


def format_feed_detail(entry: FeedEntry) -> str:
    lines = [
        "说说详情",
        f"- hostuin: {entry.hostuin}",
        f"- fid: {entry.fid}",
        f"- appid: {entry.appid}",
        f"- time: {to_local_time_text(entry.created_at)}",
        f"- like: {entry.like_count}",
        f"- comment: {entry.comment_count}",
        f"- liked: {entry.liked}",
        f"- summary: {entry.summary or '(empty)'}",
    ]
    return "\n".join(lines)


def format_action_result(title: str, payload: dict) -> str:
    parts = [title]
    for key, value in payload.items():
        if key in {"raw", "detail"}:
            continue
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"- {key}: {value}")
    return "\n".join(parts)
