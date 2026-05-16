"""Target-style Qzone post and comment adapters."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from html import unescape
from typing import Any, Iterable

from .models import FeedEntry
from .utils import truncate


TAG_RE = re.compile(r"<[^>]+>")
EM_RE = re.compile(r"\[em\].*?\[/em\]")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def clean_qzone_text(value: Any) -> str:
    text = str(value or "")
    text = EM_RE.sub("", text)
    text = TAG_RE.sub("", text)
    return unescape(text).strip()


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return clean_qzone_text(value)
    return ""


def _iter_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


@dataclass(slots=True)
class QzoneComment:
    commentid: str
    uin: int = 0
    nickname: str = ""
    content: str = ""
    created_at: int = 0
    parent_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        name = self.nickname or str(self.uin or "")
        return f"{prefix}{name}: {truncate(self.content, 80)}"


@dataclass(slots=True)
class QzonePost:
    hostuin: int
    fid: str
    appid: int = 311
    summary: str = ""
    nickname: str = ""
    created_at: int = 0
    like_count: int = 0
    comment_count: int = 0
    liked: bool = False
    images: list[str] = field(default_factory=list)
    comments: list[QzoneComment] = field(default_factory=list)
    busi_param: dict[str, Any] = field(default_factory=dict)
    local_id: int = 0
    saved_id: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["comments"] = [item.to_dict() for item in self.comments]
        return data

    def brief(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        saved = f"稿件 #{self.saved_id} | " if self.saved_id else ""
        liked = "已赞" if self.liked else "未赞"
        name = self.nickname or str(self.hostuin or "")
        return (
            f"{prefix}{saved}{name}({self.hostuin})\n"
            f"{truncate(self.summary, 220)}\n"
            f"{liked} | {self.like_count} 赞 | {self.comment_count} 评论"
        )

    def detail_text(self, index: int | None = None, *, max_comments: int = 8) -> str:
        lines = [self.brief(index)]
        if self.images:
            lines.append("图片: " + ", ".join(self.images[:9]))
        if self.comments:
            lines.append("评论:")
            for offset, comment in enumerate(self.comments[:max_comments]):
                lines.append(comment.brief(offset))
        return "\n".join(lines)


def comment_from_raw(raw: dict[str, Any], *, parent_id: str = "") -> QzoneComment:
    user = _first_mapping(raw.get("user"), raw.get("userinfo"), raw.get("commenter"))
    commentid = raw.get("commentid") or raw.get("commentId") or raw.get("tid") or raw.get("id") or ""
    uin = _to_int(raw.get("uin") or raw.get("commentUin") or user.get("uin") or user.get("user_id"))
    nickname = _first_text(user, "nickname", "name", "uinname") or _first_text(raw, "nickname", "name")
    content = _first_text(raw, "content", "commentContent", "htmlContent", "text")
    created_at = _to_int(raw.get("date") or raw.get("created_at") or raw.get("pubtime") or raw.get("abstime"))
    return QzoneComment(
        commentid=str(commentid),
        uin=uin,
        nickname=nickname,
        content=content,
        created_at=created_at,
        parent_id=str(parent_id or raw.get("parentId") or raw.get("parent_tid") or ""),
        raw=dict(raw),
    )


def _extract_nested_replies(raw: dict[str, Any], parent_id: str) -> list[QzoneComment]:
    replies: list[QzoneComment] = []
    for key in ("replyList", "replylist", "replies", "list_3", "children"):
        for item in _iter_mappings(raw.get(key)):
            replies.append(comment_from_raw(item, parent_id=parent_id))
            replies.extend(_extract_nested_replies(item, parent_id=str(item.get("commentid") or item.get("tid") or parent_id)))
    return replies


def extract_comments(payload: dict[str, Any]) -> list[QzoneComment]:
    candidates: list[Any] = []
    comment_block = payload.get("comment")
    if isinstance(comment_block, dict):
        candidates.extend(
            [
                comment_block.get("comments"),
                comment_block.get("commentlist"),
                comment_block.get("list"),
            ]
        )
    candidates.extend(
        [
            payload.get("comments"),
            payload.get("commentlist"),
            payload.get("list_3"),
        ]
    )
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("comments"), data.get("commentlist")])

    comments: list[QzoneComment] = []
    seen: set[tuple[str, int, str]] = set()
    for candidate in candidates:
        for item in _iter_mappings(candidate):
            comment = comment_from_raw(item)
            key = (comment.commentid, comment.uin, comment.content)
            if key not in seen:
                comments.append(comment)
                seen.add(key)
            for reply in _extract_nested_replies(item, comment.commentid):
                reply_key = (reply.commentid, reply.uin, reply.content)
                if reply_key not in seen:
                    comments.append(reply)
                    seen.add(reply_key)
    return comments


def extract_images(payload: dict[str, Any]) -> list[str]:
    images: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in images:
            images.append(value)

    for key in ("images", "pics", "pic"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    add(item.get("url") or item.get("pic_url") or item.get("smallurl") or item.get("origin_url"))
        elif isinstance(value, dict):
            add(value.get("url") or value.get("pic_url") or value.get("smallurl") or value.get("origin_url"))

    for item in _iter_mappings(payload.get("picdata")):
        add(item.get("url") or item.get("pic_url") or item.get("smallurl") or item.get("origin_url"))
    return images


def post_from_entry(entry: FeedEntry, *, detail: dict[str, Any] | None = None, local_id: int = 0) -> QzonePost:
    raw = detail if isinstance(detail, dict) else entry.raw
    comments = extract_comments(raw or {})
    return QzonePost(
        hostuin=entry.hostuin,
        fid=entry.fid,
        appid=entry.appid,
        summary=clean_qzone_text(entry.summary),
        nickname=entry.nickname,
        created_at=entry.created_at,
        like_count=entry.like_count,
        comment_count=max(entry.comment_count, len(comments)),
        liked=entry.liked,
        images=extract_images(raw or {}),
        comments=comments,
        busi_param=dict(entry.busi_param or {}),
        local_id=local_id,
        raw=dict(raw or {}),
    )
