"""Persistent cache for target-style Qzone post operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .json_store import AtomicItemStoreFile
from .social import QzoneComment, QzonePost


@dataclass(slots=True)
class SavedPost:
    id: int
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
    comments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_post(cls, post: QzonePost, post_id: int) -> "SavedPost":
        return cls(
            id=post_id,
            hostuin=post.hostuin,
            fid=post.fid,
            appid=post.appid,
            summary=post.summary,
            nickname=post.nickname,
            created_at=post.created_at,
            like_count=post.like_count,
            comment_count=post.comment_count,
            liked=post.liked,
            images=list(post.images),
            comments=[comment.to_dict() for comment in post.comments],
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SavedPost":
        return cls(
            id=int(data.get("id") or 0),
            hostuin=int(data.get("hostuin") or 0),
            fid=str(data.get("fid") or ""),
            appid=int(data.get("appid") or 311),
            summary=str(data.get("summary") or ""),
            nickname=str(data.get("nickname") or ""),
            created_at=int(data.get("created_at") or 0),
            like_count=int(data.get("like_count") or 0),
            comment_count=int(data.get("comment_count") or 0),
            liked=bool(data.get("liked") or False),
            images=[str(item) for item in data.get("images") or []],
            comments=[item for item in data.get("comments") or [] if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_post(self) -> QzonePost:
        comments = [
            QzoneComment(
                commentid=str(item.get("commentid") or ""),
                uin=int(item.get("uin") or 0),
                nickname=str(item.get("nickname") or ""),
                content=str(item.get("content") or ""),
                created_at=int(item.get("created_at") or item.get("date") or 0),
                parent_id=str(item.get("parent_id") or item.get("parentId") or ""),
            )
            for item in self.comments
            if isinstance(item, dict)
        ]
        return QzonePost(
            hostuin=self.hostuin,
            fid=self.fid,
            appid=self.appid,
            summary=self.summary,
            nickname=self.nickname,
            created_at=self.created_at,
            like_count=self.like_count,
            comment_count=max(self.comment_count, len(comments)),
            liked=self.liked,
            images=list(self.images),
            comments=comments,
            saved_id=self.id,
        )


class PostStore:
    def __init__(self, path: Path):
        self.path = path
        self._store = AtomicItemStoreFile(path)

    def _read_payload(self) -> dict[str, Any]:
        return self._store.read()

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._store.write(payload)

    def list(self) -> list[SavedPost]:
        payload = self._read_payload()
        return sorted(
            [SavedPost.from_dict(item) for item in payload.get("items") or [] if isinstance(item, dict)],
            key=lambda item: item.id,
        )

    def get(self, post_id: int | None = None) -> SavedPost | None:
        try:
            target_id = int(post_id or 0)
        except (TypeError, ValueError):
            return None
        if target_id <= 0:
            return None
        items = self.list()
        for item in items:
            if item.id == target_id:
                return item
        return None

    def latest(self) -> SavedPost | None:
        items = self.list()
        return items[-1] if items else None

    def upsert(self, post: QzonePost) -> SavedPost:
        def update(payload: dict[str, Any]) -> SavedPost:
            items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
            next_id = int(payload.get("next_id") or 1)
            matched_id = 0
            updated_items: list[dict[str, Any]] = []
            for item in items:
                if (
                    str(item.get("fid") or "") == str(post.fid or "")
                    and int(item.get("hostuin") or 0) == int(post.hostuin or 0)
                    and str(post.fid or "")
                ):
                    matched_id = int(item.get("id") or 0) or next_id
                    updated_items.append(SavedPost.from_post(post, matched_id).to_dict())
                else:
                    updated_items.append(item)

            if not matched_id:
                matched_id = next_id
                updated_items.append(SavedPost.from_post(post, matched_id).to_dict())
                next_id += 1

            payload["items"] = updated_items
            payload["next_id"] = max(next_id, matched_id + 1)
            post.saved_id = matched_id
            return SavedPost.from_post(post, matched_id)

        return self._store.transact(update)
