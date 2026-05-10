"""AstrBot side daemon controller."""

from __future__ import annotations

import contextlib
import asyncio
import os
import socket
import subprocess
import sys
import logging
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .errors import DaemonUnavailableError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .protocol import SECRET_HEADER
from .storage import StateStore, ensure_state_secret
from .utils import now_iso


log = logging.getLogger(__name__)


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
        changed = False
        if not state.runtime.daemon_port:
            state.runtime.daemon_port = self.default_port
            changed = True
        if not state.runtime.secret:
            import secrets

            state.runtime.secret = secrets.token_urlsafe(32)
            changed = True
        if changed:
            self.store.write(state)
        return state.runtime

    def _current_runtime(self):
        return self.store.read().runtime

    def _base_url(self, port: int | None = None) -> str:
        if port is not None:
            return f"http://127.0.0.1:{port}"
        runtime = self._runtime()
        return f"http://127.0.0.1:{runtime.daemon_port}"

    def _secret(self) -> str:
        return self._runtime().secret

    def _daemon_script(self) -> Path:
        return self.plugin_root / "daemon_main.py"

    async def _probe_health(self, port: int, *, secret: str) -> bool:
        try:
            response = await self._client.get(
                f"{self._base_url(port)}/health",
                headers={SECRET_HEADER: secret},
                timeout=min(2.0, float(self.request_timeout)),
            )
            payload = response.json()
        except Exception:
            return False
        return response.status_code == 200 and bool(payload.get("ok"))

    async def _request_remote(
        self,
        method: str,
        path: str,
        *,
        port: int,
        secret: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url(port)}{path}",
                headers={SECRET_HEADER: secret},
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise DaemonUnavailableError(
                "daemon 请求失败",
                detail={"method": method, "path": path, "port": port, "error": str(exc)},
            ) from exc
        try:
            payload = response.json()
        except Exception as exc:
            raise DaemonUnavailableError("daemon returned non-JSON response", detail={"text": response.text[:500]}) from exc
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        runtime = self._runtime()
        return await self._request_remote(
            method,
            path,
            port=runtime.daemon_port,
            secret=runtime.secret,
            params=params,
            json_body=json_body,
        )

    async def get_status(self) -> dict[str, Any]:
        state = self.store.read()
        runtime = state.runtime
        daemon_state = "offline"
        if runtime.daemon_port and runtime.secret and await self._probe_health(runtime.daemon_port, secret=runtime.secret):
            try:
                payload = await self._request_remote("GET", "/status", port=runtime.daemon_port, secret=runtime.secret)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                log.debug("status probe failed: %s", exc)
            daemon_state = "degraded"
        elif state.session.needs_rebind or not bool(state.session.cookies):
            daemon_state = "needs_rebind"
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

    def _choose_port(self, preferred: int) -> int:
        if _port_is_free(preferred):
            return preferred
        for port in range(preferred + 1, preferred + 21):
            if _port_is_free(port):
                return port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _popen_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.plugin_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return kwargs

    async def _wait_for_health(self, port: int, secret: str, *, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, timeout)
        while loop.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            if await self._probe_health(port, secret=secret):
                return True
            await asyncio.sleep(0.2)
        return await self._probe_health(port, secret=secret)

    async def _wait_for_shutdown(self, port: int, secret: str, *, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, timeout)
        while loop.time() < deadline:
            if not await self._probe_health(port, secret=secret) and _port_is_free(port):
                return True
            await asyncio.sleep(0.2)
        return not await self._probe_health(port, secret=secret) and _port_is_free(port)

    async def ensure_running(self) -> dict[str, Any]:
        async with self._lock:
            runtime = self._runtime()
            if runtime.daemon_port and runtime.secret and await self._probe_health(runtime.daemon_port, secret=runtime.secret):
                payload = await self._request_remote(
                    "GET",
                    "/status",
                    port=runtime.daemon_port,
                    secret=runtime.secret,
                )
                return payload if isinstance(payload, dict) else {}

            await self._stop_recorded_daemon(runtime)
            state = ensure_state_secret(self.store.read())
            preferred_port = state.runtime.daemon_port or self.default_port
            port = self._choose_port(int(preferred_port))
            secret = state.runtime.secret
            state.runtime.daemon_port = port
            state.runtime.daemon_pid = 0
            state.runtime.started_at = ""
            state.runtime.last_seen_at = now_iso()
            state.runtime.version = __version__
            self.store.write(state)

            script = self._daemon_script()
            if not script.exists():
                raise DaemonUnavailableError("daemon_main.py 不存在，无法启动 daemon", detail={"path": str(script)})

            cmd = [
                sys.executable,
                "-u",
                str(script),
                "--data-dir",
                str(self.data_dir),
                "--port",
                str(port),
                "--secret",
                secret,
                "--keepalive-interval",
                str(self.keepalive_interval),
                "--request-timeout",
                str(self.request_timeout),
                "--user-agent",
                self.user_agent,
                "--version",
                __version__,
            ]
            try:
                self._process = subprocess.Popen(cmd, **self._popen_kwargs())
            except OSError as exc:
                raise DaemonUnavailableError("daemon 启动失败", detail={"error": str(exc), "cmd": cmd}) from exc

            state = self.store.read()
            state.runtime.daemon_pid = int(self._process.pid or 0)
            state.runtime.daemon_port = port
            state.runtime.started_at = now_iso()
            state.runtime.last_seen_at = now_iso()
            state.runtime.version = __version__
            self.store.write(state)

            if not await self._wait_for_health(port, secret, timeout=self.start_timeout):
                exit_code = self._process.poll() if self._process else None
                await self._terminate_process_handle()
                raise DaemonUnavailableError(
                    "daemon 启动超时",
                    detail={"port": port, "pid": state.runtime.daemon_pid, "exit_code": exit_code},
                )

            payload = await self._request_remote("GET", "/status", port=port, secret=secret)
            return payload if isinstance(payload, dict) else {}

    async def _terminate_process_handle(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self._process = None
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(process.wait, timeout=3)
        finally:
            self._process = None

    async def _stop_recorded_daemon(self, runtime) -> None:
        port = int(runtime.daemon_port or 0)
        secret = str(runtime.secret or "")
        if port and secret and await self._probe_health(port, secret=secret):
            with contextlib.suppress(QzoneBridgeError):
                await self._request_remote("POST", "/shutdown", port=port, secret=secret, json_body={})
            await self._wait_for_shutdown(port, secret, timeout=5.0)
        await self._terminate_process_handle()

    async def close(self) -> None:
        async with self._lock:
            runtime = self._current_runtime()
            await self._stop_recorded_daemon(runtime)
            state = self.store.read()
            state.runtime.daemon_pid = 0
            state.runtime.started_at = ""
            state.runtime.last_seen_at = now_iso()
            self.store.write(state)
            self._process = None
            await self._client.aclose()
