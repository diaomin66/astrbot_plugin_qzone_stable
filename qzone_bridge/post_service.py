"""Business operations for viewing, commenting, and liking Qzone posts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .astrbot_logging import get_logger
from .errors import QzoneParseError
from .models import FeedEntry
from .posts import PostStore
from .selection import PostSelection
from .social import QzoneComment, QzonePost, post_from_entry

log = get_logger(__name__)


class QzonePostService:
    def __init__(self, controller: Any, post_store: PostStore, *, max_feed_limit: int = 20):
        self.controller = controller
        self.post_store = post_store
        self.max_feed_limit = max(1, int(max_feed_limit or 20))

    def _to_feed_entries(self, payload: dict[str, Any]) -> list[FeedEntry]:
        entries: list[FeedEntry] = []
        for item in payload.get("items") or []:
            if isinstance(item, FeedEntry):
                entries.append(item)
            elif isinstance(item, dict):
                entries.append(FeedEntry(**item))
        return entries

    @staticmethod
    def _comments_from_detail(detail_payload: dict[str, Any]) -> list[QzoneComment]:
        comments: list[QzoneComment] = []
        for item in detail_payload.get("comments") or []:
            if not isinstance(item, dict):
                continue
            comments.append(
                QzoneComment(
                    commentid=str(item.get("commentid") or ""),
                    uin=int(item.get("uin") or 0),
                    nickname=str(item.get("nickname") or ""),
                    content=str(item.get("content") or ""),
                    created_at=int(item.get("created_at") or item.get("date") or 0),
                    parent_id=str(item.get("parent_id") or ""),
                )
            )
        return comments

    async def _detail_post(self, entry: FeedEntry, *, local_id: int, required: bool) -> QzonePost:
        detail_payload: dict[str, Any] | None = None
        try:
            detail_payload = await self.controller.detail_feed(
                hostuin=entry.hostuin,
                fid=entry.fid,
                appid=entry.appid,
            )
            entry_data = detail_payload.get("entry")
            if isinstance(entry_data, dict):
                detail_entry = FeedEntry(**entry_data)
                if detail_entry.fid == entry.fid and detail_entry.hostuin == entry.hostuin:
                    entry = detail_entry
        except Exception:
            if required:
                raise
            log.debug("qzone detail fetch for post operation failed", exc_info=True)

        post = post_from_entry(entry, detail=(detail_payload or {}).get("raw"), local_id=local_id)
        if detail_payload and detail_payload.get("comments"):
            post.comments = self._comments_from_detail(detail_payload)
            post.comment_count = max(post.comment_count, len(post.comments))
        return post

    async def _post_from_fid(self, selection: PostSelection, *, with_detail: bool) -> QzonePost:
        if not selection.target_uin or not selection.fid:
            raise QzoneParseError("请同时提供 QQ 号和说说 fid。")
        if with_detail:
            detail_payload = await self.controller.detail_feed(
                hostuin=selection.target_uin,
                fid=selection.fid,
                appid=selection.appid,
            )
            entry_data = detail_payload.get("entry")
            entry = (
                FeedEntry(**entry_data)
                if isinstance(entry_data, dict)
                else FeedEntry(
                    hostuin=selection.target_uin,
                    fid=selection.fid,
                    appid=selection.appid,
                    summary="",
                )
            )
            post = post_from_entry(entry, detail=detail_payload.get("raw"), local_id=1)
            post.comments = self._comments_from_detail(detail_payload)
            post.comment_count = max(post.comment_count, len(post.comments))
            self.post_store.upsert(post)
            return post

        post = QzonePost(
            hostuin=selection.target_uin,
            fid=selection.fid,
            appid=selection.appid,
            local_id=1,
        )
        self.post_store.upsert(post)
        return post

    async def resolve_posts(
        self,
        selection: PostSelection,
        *,
        with_detail: bool = False,
        no_commented: bool = False,
        no_self: bool = False,
        login_uin: int = 0,
    ) -> list[QzonePost]:
        if selection.is_fid:
            if not selection.target_uin and login_uin:
                selection.target_uin = login_uin
            post = await self._post_from_fid(selection, with_detail=with_detail or no_commented)
            if no_self and login_uin and post.hostuin == login_uin:
                return []
            if no_commented and login_uin and any(comment.uin == login_uin for comment in post.comments):
                return []
            return [post]

        limit = self.max_feed_limit if selection.is_last else min(selection.limit, self.max_feed_limit)
        payload = await self.controller.list_feeds(
            hostuin=selection.target_uin,
            limit=limit,
            scope="profile" if selection.target_uin else "",
        )
        entries = self._to_feed_entries(payload)
        if selection.is_last:
            selected_entries = entries[-1:] if entries else []
            local_start = len(entries)
        else:
            start_index = max(selection.start - 1, 0)
            end_index = max(selection.end - 1, start_index)
            selected_entries = entries[start_index : end_index + 1]
            local_start = selection.start

        posts: list[QzonePost] = []
        for offset, entry in enumerate(selected_entries, start=local_start):
            post = await self._detail_post(entry, local_id=offset, required=with_detail)
            if no_self and login_uin and post.hostuin == login_uin:
                continue
            if no_commented and login_uin and any(comment.uin == login_uin for comment in post.comments):
                continue
            self.post_store.upsert(post)
            posts.append(post)
        return posts

    async def comment_post(
        self,
        post: QzonePost,
        content: str,
        *,
        private: bool = False,
    ) -> dict[str, Any]:
        if not str(content or "").strip():
            raise QzoneParseError("评论内容不能为空。")
        return await self.controller.comment_post(
            hostuin=post.hostuin,
            fid=post.fid,
            content=str(content).strip(),
            appid=post.appid,
            private=private,
            busi_param=post.busi_param,
        )

    async def like_post(self, post: QzonePost, *, unlike: bool = False) -> dict[str, Any]:
        return await self.controller.like_post(hostuin=post.hostuin, fid=post.fid, appid=post.appid, unlike=unlike)

    async def delete_post(self, post: QzonePost) -> dict[str, Any]:
        return await self.controller.delete_post(fid=post.fid, appid=post.appid)

    @staticmethod
    def post_payload(post: QzonePost) -> dict[str, Any]:
        payload = post.to_dict()
        payload.pop("raw", None)
        payload["comments"] = [asdict(comment) for comment in post.comments]
        return payload
