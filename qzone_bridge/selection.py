"""User-facing post target selection for Qzone operations."""

from __future__ import annotations

import re
from dataclasses import dataclass

LATEST_ALIASES = {
    "",
    "0",
    "1",
    "latest",
    "lastest",
    "最新",
    "最新一条",
    "最近",
    "第一条",
    "第1条",
    "第 1 条",
}
LAST_ALIASES = {"-1", "最后", "最后一条"}


@dataclass(slots=True)
class PostSelection:
    target_uin: int = 0
    start: int = 1
    end: int = 1
    selector: str = "latest"
    fid: str = ""
    appid: int = 311
    comment_text: str = ""

    @property
    def is_fid(self) -> bool:
        return bool(self.fid)

    @property
    def is_last(self) -> bool:
        return self.start < 0 or self.end < 0

    @property
    def limit(self) -> int:
        if self.is_fid or self.is_last:
            return 1
        return max(self.start, self.end, 1)


def strip_command_prefix(text: str, command_names: tuple[str, ...] = ()) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(?:[!/／]\s*)", "", value).strip()
    for name in sorted(command_names, key=len, reverse=True):
        if value == name:
            return ""
        if value.startswith(name):
            rest = value[len(name) :]
            if not rest or rest[0].isspace() or rest[0] in {":", "："}:
                return rest.lstrip(" \t:：")
    return value


def _extract_at_targets(text: str) -> tuple[list[int], str]:
    targets: list[int] = []

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1) or match.group(2)
        if raw and raw.isdigit():
            targets.append(int(raw))
        return " "

    cleaned = re.sub(r"\[CQ:at,qq=(\d+)[^\]]*\]|@(\d{5,})", replace, text, count=1)
    return targets, cleaned.strip()


def _parse_selector(token: str) -> tuple[int, int, str] | None:
    value = str(token or "").strip()
    value_lower = value.lower()
    if value in LATEST_ALIASES or value_lower in LATEST_ALIASES:
        return 1, 1, "latest"
    if value in LAST_ALIASES:
        return -1, -1, "last"

    normalized = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    ordinal = re.fullmatch(r"第\s*(\d+)\s*(?:条|條)?", normalized)
    if ordinal:
        index = max(1, int(ordinal.group(1)))
        return index, index, "index"

    match = re.fullmatch(r"(\d+)(?:\s*(?:~|～|-|－)\s*(\d+))?", normalized)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) if match.group(2) is not None else start)
    if start <= 0:
        return 1, 1, "latest"
    if end <= 0:
        end = start
    if end < start:
        start, end = end, start
    return start, end, "range" if start != end else "index"


def _looks_like_fid(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _parse_selector(text) is not None:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{6,}", text))


def parse_post_selection(text: str, command_names: tuple[str, ...] = ()) -> PostSelection:
    raw = strip_command_prefix(text, command_names)
    at_targets, raw = _extract_at_targets(raw)
    tokens = raw.split()

    target_uin = at_targets[0] if at_targets else 0
    if not target_uin and tokens and re.fullmatch(r"\d{5,}", tokens[0]):
        target_uin = int(tokens.pop(0))

    if tokens and target_uin and _looks_like_fid(tokens[0]):
        fid = tokens.pop(0)
        appid = 311
        if tokens and tokens[0].isdigit():
            appid = int(tokens.pop(0))
        return PostSelection(
            target_uin=target_uin,
            fid=fid,
            appid=appid,
            selector="fid",
            comment_text=" ".join(tokens).strip(),
        )

    start, end, selector = 1, 1, "latest"
    if tokens:
        parsed = _parse_selector(tokens[0])
        if parsed is not None:
            start, end, selector = parsed
            tokens.pop(0)

    return PostSelection(
        target_uin=target_uin,
        start=start,
        end=end,
        selector=selector,
        comment_text=" ".join(tokens).strip(),
    )


def selection_from_tool_args(
    *,
    target_uin: int = 0,
    selector: str = "latest",
    hostuin: int = 0,
    fid: str = "",
    appid: int = 311,
    latest: bool = False,
    index: int = 0,
) -> PostSelection:
    effective_target = int(target_uin or hostuin or 0)
    if fid and not latest and index <= 0:
        return PostSelection(
            target_uin=effective_target,
            fid=str(fid),
            appid=int(appid or 311),
            selector="fid",
        )
    if latest:
        return PostSelection(target_uin=effective_target, start=1, end=1, selector="latest")
    if index > 0:
        return PostSelection(target_uin=effective_target, start=int(index), end=int(index), selector="index")

    parsed = _parse_selector(str(selector or "latest"))
    if parsed is None:
        if _looks_like_fid(str(selector or "")):
            return PostSelection(
                target_uin=effective_target,
                fid=str(selector).strip(),
                appid=int(appid or 311),
                selector="fid",
            )
        parsed = (1, 1, "latest")
    start, end, mode = parsed
    return PostSelection(target_uin=effective_target, start=start, end=end, selector=mode)
