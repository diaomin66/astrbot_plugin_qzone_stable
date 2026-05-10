"""AstrBot side daemon controller."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from .errors import DaemonUnavailableError, QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .models import BridgeState
from .protocol import SECRET_HEADER
from .storage import StateStore, ensure_state_secret
from .utils import now_iso


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


class QzoneDaemonController:
    def __init__(
        self,
        *,
        plugin_root: Path,
        data_dir: Path,
        default_port: int = 18999,
        request_timeout: float = 15.0,
        start_timeout: float = 20.0,
        keepalive_interval: int = 120,
        user_agent: str = "",
    ) -> None:
        self.plugin_root = plugin_root
        self.data_dir = data_dir
        self.store = StateStore(data_dir)
        self.default_port = default_port
        self.request_timeout = request_timeout
        self.start_timeout = start_timeout
        self.keepalive_interval = keepalive_interval
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(request_timeout), trust_env=False)
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen | None = None

    def _runtime(self):
        state = ensure_state_secret(self.store.read())
        if not state.runtime.daemon_port:
            state.runtime.daemon_port = self.default_port
        if not state.runtime.secret:
            import secrets

            state.runtime.secret = secrets.token_urlsafe(32)
        self.store.write(state)
        return state.runtime

    def _base_url(self, port: int | None = None) -> str:
        runtime = self._runtime()
        return f"http://127.0.0.1:{port or runtime.daemon_port}"

    def _secret(self) -> str:
        return self._runtime().secret

    def _daemon_script(self) -> Path:
        return self.plugin_root / "daemon_main.py"

    def _spawn_daemon(self, port: int) -> subprocess.Popen:
        runtime = self._runtime()
        cmd = [
            sys.executable,
            str(self._daemon_script()),
            "--data-dir",
            str(self.data_dir),
            "--port",
            str(port),
            "--secret",
            runtime.secret,
            "--keepalive-interval",
            str(self.keepalive_interval),
            "--request-timeout",
            str(self.request_timeout),
        ]
        if self.user_agent:
            cmd.extend(["--user-agent", self.user_agent])
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, cwd=str(self.plugin_root), **kwargs)

    async def _probe_health(self, port: int | None = None) -> bool:
        runtime = self._runtime()
        try:
            response = await self._client.get(
                f"{self._base_url(port)}/health",
                headers={SECRET_HEADER: runtime.secret},
            )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except Exception:
            return False
        return bool(payload.get("ok"))

    async def ensure_running(self) -> None:
        async with self._lock:
            runtime = self._runtime()
            port = runtime.daemon_port or self.default_port
            if await self._probe_health(port):
                return

            if not _port_is_free(port):
                candidate = port
                for _ in range(32):
                    candidate += 1
                    if _port_is_free(candidate):
                        port = candidate
                        break
                state = self.store.read()
                state.runtime.daemon_port = port
                self.store.write(state)

            if not self._daemon_script().exists():
                raise DaemonUnavailableError("找不到 daemon_main.py")

            self._process = self._spawn_daemon(port)

            deadline = asyncio.get_running_loop().time() + self.start_timeout
            while asyncio.get_running_loop().time() < deadline:
                if await self._probe_health(port):
                    runtime.daemon_port = port
                    runtime.daemon_pid = self._process.pid if self._process else 0
                    runtime.started_at = now_iso()
                    runtime.last_seen_at = now_iso()
                    state = self.store.read()
                    state.runtime = runtime
                    self.store.write(state)
                    return
                if self._process and self._process.poll() is not None:
                    break
                await asyncio.sleep(0.5)

            raise DaemonUnavailableError("QQ空间 daemon 启动失败")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        await self.ensure_running()
        runtime = self._runtime()
        response = await self._client.request(
            method,
            f"{self._base_url()}{path}",
            headers={SECRET_HEADER: runtime.secret},
            params=params,
            json=json_body,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DaemonUnavailableError("daemon 返回了非 JSON 响应", detail={"text": response.text[:500]}) from exc
        if not payload.get("ok", False):
            error = payload.get("error") or {}
            code = str(error.get("code") or "DAEMON_ERROR")
            message = str(error.get("message") or "daemon error")
            detail = error.get("detail")
            if code == "QZONE_AUTH":
                raise QzoneNeedsRebind(message, detail=detail)
            if code == "QZONE_PARSE":
                raise QzoneParseError(message, detail=detail)
            if code == "QZONE_REQUEST":
                raise QzoneRequestError(message, detail=detail)
            raise DaemonUnavailableError(message, detail=detail)
        return payload.get("data")

    async def get_status(self) -> dict[str, Any]:
        state = self.store.read()
        runtime = state.runtime
        daemon_state = "offline"
        if runtime.daemon_port and await self._probe_health(runtime.daemon_port):
            daemon_state = "ready"
        elif state.session.cookies:
            daemon_state = "degraded"
        return {
            "daemon_state": daemon_state,
            "daemon_pid": runtime.daemon_pid,
            "daemon_port": runtime.daemon_port or self.default_port,
            "daemon_version": runtime.version,
            "started_at": runtime.started_at,
            "last_seen_at": runtime.last_seen_at,
            "login_uin": state.session.uin,
            "cookie_summary": self.cookie_summary(state.session.cookies),
            "cookie_count": len(state.session.cookies),
            "needs_rebind": state.session.needs_rebind or not bool(state.session.cookies),
            "last_ok_at": state.session.last_ok_at,
            "last_error": state.session.last_error,
            "qzonetoken_hosts": sorted(int(k) for k in state.session.qzonetokens.keys() if str(k).isdigit()),
            "feed_cache_size": 0,
            "session_revision": state.session.revision,
        }

    @staticmethod
    def cookie_summary(cookies: dict[str, str]) -> str:
        if not cookies:
            return "未绑定"
        keys = ["uin", "p_uin", "skey", "p_skey", "pt4_token", "pt_key"]
        found = [key for key in keys if key in cookies]
        return f"{len(cookies)}个字段: " + ", ".join(found or ["未知字段"])

    async def bind_cookie(self, cookie_text: str, *, uin: int = 0, source: str = "manual") -> dict[str, Any]:
        return await self._request("POST", "/bind", json_body={"cookie_text": cookie_text, "uin": uin, "source": source})

    async def unbind(self) -> dict[str, Any]:
        return await self._request("POST", "/unbind", json_body={})

    async def list_feeds(self, *, hostuin: int = 0, limit: int = 5, cursor: str = "", scope: str = "") -> dict[str, Any]:
        return await self._request(
            "GET",
            "/feeds",
            params={"hostuin": hostuin, "limit": limit, "cursor": cursor, "scope": scope},
        )

    async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        return await self._request(
            "GET",
            "/detail",
            params={"hostuin": hostuin, "fid": fid, "appid": appid, "busi_param": busi_param},
        )

    async def publish_post(self, *, content: str, sync_weibo: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/post", json_body={"content": content, "sync_weibo": sync_weibo})

    async def comment_post(
        self,
        *,
        hostuin: int,
        fid: str,
        content: str,
        appid: int = 311,
        private: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/comment",
            json_body={
                "hostuin": hostuin,
                "fid": fid,
                "content": content,
                "appid": appid,
                "private": private,
            },
        )

    async def like_post(
        self,
        *,
        hostuin: int,
        fid: str,
        appid: int = 311,
        curkey: str = "",
        unlike: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/like",
            json_body={
                "hostuin": hostuin,
                "fid": fid,
                "appid": appid,
                "curkey": curkey,
                "unlike": unlike,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
