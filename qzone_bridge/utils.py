"""Small helpers shared across the bridge."""

from __future__ import annotations

import ast
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Iterable, TypeVar

MATCH_PAIR = {"{": "}", "[": "]"}
CALLBACK_RE = re.compile(r"(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*\(\s*([\{\[].*[\}\]])\s*\)", re.S)
SCRIPT_RE = re.compile(
    r"<script[^>]+type=[\"']application/javascript[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)

T = TypeVar("T")
D = TypeVar("D")
_MISSING = object()


def hash33(key: str, phash: int = 0) -> int:
    for char in key:
        phash += (phash << 5) + ord(char)
    return 0x7FFFFFFF & phash


def gtk(skey: str | None) -> int:
    if not skey:
        return 0
    return hash33(skey, phash=5381)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_local_time_text(value: int | float | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")


def truncate(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def first(it: Iterable[T], pred=None, *, default=_MISSING):
    filtered = filter(pred, it)
    if default is _MISSING:
        return next(filtered)
    return next(filtered, default)


def firstn(it: Iterable[T], pred=None):
    return first(it, pred, default=None)


def entire_closing(string: str, inc: str = "{") -> int:
    dec = MATCH_PAIR[inc]
    count = 0
    for index, char in enumerate(string):
        if char == inc:
            count += 1
        elif char == dec:
            count -= 1
        if count == 0:
            return index
    return -1


def json_loads(js: str) -> Any:
    js = dedent(js).replace(r"\/", "/")
    node = ast.parse(js, mode="eval")

    class RewriteUndef(ast.NodeTransformer):
        const = {
            "undefined": ast.Constant(value=None),
            "null": ast.Constant(value=None),
            "true": ast.Constant(value=True),
            "false": ast.Constant(value=False),
        }

        def visit_Name(self, node: ast.Name):
            return self.const.get(node.id, ast.Constant(value=node.id))

    node = ast.fix_missing_locations(RewriteUndef().visit(node))
    return ast.literal_eval(node)


def extract_scripts(html_text: str) -> list[str]:
    return [html.unescape(match.group(1)) for match in SCRIPT_RE.finditer(html_text)]


def extract_callback_json(text: str) -> Any | None:
    match = CALLBACK_RE.search(text)
    if not match:
        return None
    return json_loads(match.group(1))


def merge_unique(*parts: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        merged.update(part)
    return merged


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
