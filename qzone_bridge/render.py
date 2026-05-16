"""Text renderers for human and LLM-facing output."""

from __future__ import annotations

from collections.abc import Iterable

from .models import FeedEntry
from .utils import to_local_time_text, truncate


def cookie_summary(cookies: dict[str, str]) -> str:
    if not cookies:
        return "无 Cookie"
    keys = [
        "uin",
        "p_uin",
        "skey",
        "p_skey",
        "pskey",
        "g_tk",
        "gtk",
        "bkn",
        "csrf_token",
        "pt4_token",
        "pt_key",
        "qqmusic_key",
        "lvkey",
    ]
    found = [key for key in keys if key in cookies]
    extras = len(cookies) - len(found)
    return f"{len(cookies)} 个 Cookie: " + ", ".join(found + ([f"另 {extras} 个"] if extras > 0 else []))


def format_status(status: dict) -> str:
    lines = [
        "QQ 空间状态",
        f"- daemon: {status.get('daemon_state', 'unknown')}",
        f"- login: {status.get('login_uin') or '-'}",
        f"- source: {status.get('session_source') or '-'}",
        f"- cookie: {status.get('cookie_summary', '-')}",
        f"- needs_rebind: {status.get('needs_rebind', False)}",
        f"- last_ok: {status.get('last_ok_at') or '-'}",
        f"- last_error: {status.get('last_error', '-')}",
    ]
    if status.get("daemon_port"):
        lines.append(f"- endpoint: 127.0.0.1:{status['daemon_port']}")
    if status.get("daemon_pid"):
        lines.append(f"- pid: {status['daemon_pid']}")
    start_error = status.get("daemon_start_error")
    if isinstance(start_error, dict):
        message = start_error.get("message") or "daemon 启动失败"
        lines.append(f"- daemon_error: {message}")
        detail = start_error.get("detail")
        if isinstance(detail, dict):
            if detail.get("returncode") is not None:
                lines.append(f"- daemon_returncode: {detail['returncode']}")
            if detail.get("log_path"):
                lines.append(f"- daemon_log: {detail['log_path']}")
            if detail.get("log_tail"):
                lines.append(f"- daemon_log_tail: {truncate(str(detail['log_tail']), 300)}")
    return "\n".join(lines)


def format_feed_entry(entry: FeedEntry, index: int | None = None, *, include_internal: bool = True) -> str:
    prefix = f"{index}. " if index is not None else "- "
    headline = truncate(entry.summary or "(empty)", 90)
    lines = [f"{prefix}{to_local_time_text(entry.created_at)} | {entry.nickname or entry.hostuin}"]
    if include_internal:
        lines.append(
            f"   fid={entry.fid} appid={entry.appid} "
            f"like={entry.like_count} comment={entry.comment_count} liked={entry.liked}"
        )
    else:
        liked_text = "已赞" if entry.liked else "未赞"
        lines.append(f"   {liked_text} | {entry.like_count} 赞 | {entry.comment_count} 评论")
    lines.append(f"   {headline}")
    return "\n".join(lines)


def format_feed_list(
    entries: Iterable[FeedEntry],
    *,
    cursor: str = "",
    has_more: bool = False,
    include_internal: bool = True,
    include_pagination: bool = True,
) -> str:
    rendered = [
        format_feed_entry(entry, i + 1, include_internal=include_internal)
        for i, entry in enumerate(entries)
    ]
    footer = []
    if include_pagination:
        if cursor:
            footer.append(f"cursor={cursor}")
        footer.append(f"has_more={has_more}")
    body = "\n".join(rendered) if rendered else "(no feeds)"
    return "\n".join([body, *footer])


def format_llm_feed_list(entries: Iterable[FeedEntry]) -> str:
    entries = list(entries)
    if not entries:
        return "没有找到可展示的说说。"
    body = format_feed_list(entries, include_internal=False, include_pagination=False)
    return f"{body}\n可以用上面的序号继续指定要查看或操作的说说。"


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


def format_like_result(payload: dict) -> str:
    action = "取消点赞" if payload.get("action") == "unlike" else "点赞"
    summary = truncate(str(payload.get("summary") or ""), 80)
    suffix = f"「{summary}」" if summary else ""
    if payload.get("verified"):
        if payload.get("already"):
            state = "已经是目标状态"
        else:
            state = "已完成"
        liked = "当前已点赞" if payload.get("liked") else "当前未点赞"
        return f"{action}{state}{suffix}，{liked}。"
    return f"{action}已受理，QQ 空间可能还在同步{suffix}。"
