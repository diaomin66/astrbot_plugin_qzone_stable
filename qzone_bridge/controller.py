"""AstrBot side daemon controller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from .errors import DaemonUnavailableError, QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .media import strip_command_prefix
from .models import SessionState
from .parser import normalize_uin, parse_cookie_text
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


def _run_quiet(args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def _port_owner_pids(port: int) -> set[int]:
    pids: set[int] = set()
    if port <= 0:
        return pids
    if os.name == "nt":
        with contextlib.suppress(Exception):
            result = _run_quiet(["netstat", "-ano", "-p", "tcp"], timeout=8.0)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                local_address = parts[1]
                state = parts[3].upper()
                pid_text = parts[4]
                if state != "LISTENING":
                    continue
                if local_address.rsplit(":", 1)[-1] != str(port):
                    continue
                with contextlib.suppress(ValueError):
                    pids.add(int(pid_text))
        return pids

    with contextlib.suppress(Exception):
        result = _run_quiet(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            timeout=5.0,
        )
        for line in result.stdout.splitlines():
            with contextlib.suppress(ValueError):
                pids.add(int(line.strip()))
    if pids:
        return pids

    with contextlib.suppress(Exception):
        result = _run_quiet(["fuser", f"{port}/tcp"], timeout=5.0)
        for token in (result.stdout + " " + result.stderr).split():
            token = token.strip()
            if token.isdigit():
                pids.add(int(token))
    return pids


def _pid_command_line(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "nt":
        script = f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine"
        with contextlib.suppress(Exception):
            result = _run_quiet(["powershell", "-NoProfile", "-Command", script], timeout=5.0)
            return result.stdout.strip()
        return ""

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    with contextlib.suppress(Exception):
        return proc_cmdline.read_text(encoding="utf-8", errors="ignore").replace("\x00", " ").strip()
    with contextlib.suppress(Exception):
        result = _run_quiet(["ps", "-p", str(pid), "-o", "command="], timeout=5.0)
        return result.stdout.strip()
    return ""


def _is_plugin_daemon_pid(pid: int, plugin_root: Path) -> bool:
    command_line = _pid_command_line(pid).lower()
    if not command_line:
        return False
    root = str(plugin_root).lower()
    return "daemon_main.py" in command_line and root in command_line


def _terminate_pid_tree(pid: int, *, force: bool = False) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        with contextlib.suppress(Exception):
            _run_quiet(args, timeout=8.0)
        return

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


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
        auto_start_daemon: bool = True,
    ) -> None:
        self.plugin_root = plugin_root
        self.data_dir = data_dir
        self.store = StateStore(data_dir)
        self.default_port = default_port
        self.request_timeout = request_timeout
        self.start_timeout = start_timeout
        self.keepalive_interval = keepalive_interval
        self.user_agent = user_agent
        self.auto_start_daemon = auto_start_daemon
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(request_timeout), trust_env=False)
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen | None = None
        self._health_cache: tuple[int, str, bool, float] | None = None
        self._health_cache_ttl = 1.0

    def _runtime(self):
        state = self.store.read()
        original_secret = state.runtime.secret
        original_started_at = state.runtime.started_at
        original_port = state.runtime.daemon_port
        state = ensure_state_secret(state)
        if not state.runtime.daemon_port:
            state.runtime.daemon_port = self.default_port
        if (
            state.runtime.secret != original_secret
            or state.runtime.started_at != original_started_at
            or state.runtime.daemon_port != original_port
        ):
            self.store.write(state)
        return state.runtime

    def _base_url(self, port: int | None = None) -> str:
        if port is None:
            port = self._runtime().daemon_port
        return f"http://127.0.0.1:{port}"

    def _secret(self) -> str:
        return self._runtime().secret

    def _daemon_script(self) -> Path:
        return self.plugin_root / "daemon_main.py"

    def _invalidate_health_cache(self) -> None:
        self._health_cache = None

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
        env = os.environ.copy()
        env["QZONE_BRIDGE_PLUGIN_ROOT"] = str(self.plugin_root)
        kwargs["env"] = env
        return subprocess.Popen(cmd, cwd=str(self.plugin_root), **kwargs)

    async def _probe_health(
        self,
        port: int | None = None,
        *,
        secret: str | None = None,
        use_cache: bool = True,
    ) -> bool:
        runtime = self._runtime()
        resolved_port = int(port or runtime.daemon_port or self.default_port or 0)
        resolved_secret = secret or runtime.secret
        if not resolved_port or not resolved_secret:
            self._invalidate_health_cache()
            return False

        now = asyncio.get_running_loop().time()
        if use_cache and self._health_cache:
            cached_port, cached_secret, cached_ok, expires_at = self._health_cache
            if cached_port == resolved_port and cached_secret == resolved_secret and expires_at > now:
                return cached_ok

        try:
            response = await self._client.get(
                f"{self._base_url(resolved_port)}/health",
                headers={SECRET_HEADER: resolved_secret},
            )
        except httpx.HTTPError:
            self._invalidate_health_cache()
            return False
        if response.status_code != 200:
            self._invalidate_health_cache()
            return False
        try:
            payload = response.json()
        except Exception:
            self._invalidate_health_cache()
            return False
        ok = bool(payload.get("ok"))
        if ok:
            self._health_cache = (
                resolved_port,
                resolved_secret,
                True,
                now + self._health_cache_ttl,
            )
        else:
            self._invalidate_health_cache()
        return ok

    def _status_from_state(self, state, *, daemon_state: str) -> dict[str, Any]:
        runtime = state.runtime
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

    async def ensure_running(self) -> dict[str, Any]:
        async with self._lock:
            runtime = self._runtime()
            port = runtime.daemon_port or self.default_port
            if await self._probe_health(port):
                return self._status_from_state(self.store.read(), daemon_state="ready")

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
                    self._health_cache = (
                        port,
                        runtime.secret,
                        True,
                        asyncio.get_running_loop().time() + self._health_cache_ttl,
                    )
                    return self._status_from_state(state, daemon_state="ready")
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
        last_exc: httpx.HTTPError | None = None
        response: httpx.Response | None = None
        for attempt in range(2):
            runtime = self._runtime()
            if self.auto_start_daemon:
                await self.ensure_running()
                runtime = self._runtime()
            elif not await self._probe_health(runtime.daemon_port):
                raise DaemonUnavailableError("daemon 未运行")
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url(runtime.daemon_port)}{path}",
                    headers={SECRET_HEADER: runtime.secret},
                    params=params,
                    json=json_body,
                )
                break
            except httpx.HTTPError as exc:
                self._invalidate_health_cache()
                last_exc = exc
                if not self.auto_start_daemon or attempt > 0:
                    raise DaemonUnavailableError(
                        "daemon 请求失败",
                        detail={"error": str(exc), "path": path},
                    ) from exc
        if response is None:
            raise DaemonUnavailableError(
                "daemon 请求失败",
                detail={"error": str(last_exc), "path": path},
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

    async def get_status(self, *, probe_daemon: bool = True) -> dict[str, Any]:
        state = self.store.read()
        runtime = state.runtime
        daemon_state = "offline"
        if probe_daemon and runtime.daemon_port and await self._probe_health(runtime.daemon_port):
            daemon_state = "ready"
        elif state.session.cookies:
            daemon_state = "degraded"
        return self._status_from_state(state, daemon_state=daemon_state)

    @staticmethod
    def cookie_summary(cookies: dict[str, str]) -> str:
        if not cookies:
            return "未绑定"
        keys = ["uin", "p_uin", "skey", "p_skey", "pt4_token", "pt_key"]
        found = [key for key in keys if key in cookies]
        return f"{len(cookies)}个字段: " + ", ".join(found or ["未知字段"])

    async def bind_cookie(self, cookie_text: str, *, uin: int = 0, source: str = "manual") -> dict[str, Any]:
        return await self._request("POST", "/bind", json_body={"cookie_text": cookie_text, "uin": uin, "source": source})

    async def bind_cookie_local(self, cookie_text: str, *, uin: int = 0, source: str = "manual") -> dict[str, Any]:
        """Bind cookies directly into the persistent store when the daemon is unavailable."""

        try:
            return await self.bind_cookie(cookie_text, uin=uin, source=source)
        except DaemonUnavailableError:
            cookies = parse_cookie_text(cookie_text)
            if not cookies:
                raise QzoneParseError("Cookie 为空，无法绑定")
            resolved_uin = normalize_uin(cookies, override=uin)
            if not resolved_uin:
                raise QzoneParseError("Cookie 中未找到 uin / p_uin，请补齐后重试")

            state = self.store.read()
            runtime = self._runtime()
            state.runtime = runtime
            state.session = SessionState(
                uin=resolved_uin,
                cookies=cookies,
                qzonetokens={},
                source=source,
                updated_at=now_iso(),
                last_ok_at="",
                last_error=None,
                revision=state.session.revision + 1,
                needs_rebind=False,
            )
            self.store.write(state)
            return await self.get_status()

    async def unbind(self) -> dict[str, Any]:
        return await self._request("POST", "/unbind", json_body={})

    async def unbind_local(self) -> dict[str, Any]:
        """Clear cookies even when the daemon is unavailable."""

        try:
            return await self.unbind()
        except DaemonUnavailableError:
            state = self.store.read()
            runtime = self._runtime()
            state.runtime = runtime
            state.session = SessionState(
                uin=0,
                cookies={},
                qzonetokens={},
                source="manual",
                updated_at=now_iso(),
                last_ok_at="",
                last_error=None,
                revision=state.session.revision + 1,
                needs_rebind=True,
            )
            self.store.write(state)
            return await self.get_status()

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

    async def publish_post(
        self,
        *,
        content: str,
        sync_weibo: bool = False,
        media: list[dict[str, Any]] | None = None,
        content_sanitized: bool = False,
    ) -> dict[str, Any]:
        content = str(content or "")
        if not content_sanitized:
            content = strip_command_prefix(content, ("qzone post",)).strip()
        return await self._request(
            "POST",
            "/post",
            json_body={
                "content": content,
                "sync_weibo": sync_weibo,
                "media": media or [],
                "content_sanitized": True,
            },
        )

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
        latest: bool = False,
        index: int = 0,
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
                "latest": latest,
                "index": index,
            },
        )

    async def _daemon_accepts_secret(self, port: int, secret: str) -> bool:
        if not port or not secret:
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{port}/health",
                    headers={SECRET_HEADER: secret},
                )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        with contextlib.suppress(Exception):
            payload = response.json()
            return bool(payload.get("ok"))
        return False

    async def _request_daemon_shutdown(self, port: int, secret: str) -> bool:
        if not port or not secret:
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/shutdown",
                    headers={SECRET_HEADER: secret},
                )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def _wait_for_port_release(self, port: int, timeout: float = 3.0) -> bool:
        if not port:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if _port_is_free(port):
                return True
            await asyncio.sleep(0.2)
        return _port_is_free(port)

    async def _terminate_tracked_process(self) -> None:
        process = self._process
        self._process = None
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(process.wait, 2.0)

    async def _kill_plugin_port_owners(
        self,
        port: int,
        *,
        expected_pids: set[int],
        trusted_by_secret: bool,
    ) -> set[int]:
        killed: set[int] = set()
        owners = await asyncio.to_thread(_port_owner_pids, port)
        for pid in owners:
            if pid <= 0 or pid == os.getpid():
                continue
            is_expected = pid in expected_pids
            is_plugin_daemon = await asyncio.to_thread(_is_plugin_daemon_pid, pid, self.plugin_root)
            if not (trusted_by_secret or is_expected or is_plugin_daemon):
                continue
            await asyncio.to_thread(_terminate_pid_tree, pid, force=False)
            killed.add(pid)

        if killed and not await self._wait_for_port_release(port, 2.0):
            for pid in killed:
                await asyncio.to_thread(_terminate_pid_tree, pid, force=True)
        return killed

    def _clear_runtime_process_state(self) -> None:
        self._invalidate_health_cache()
        state = self.store.read()
        state.runtime.daemon_pid = 0
        state.runtime.started_at = ""
        state.runtime.last_seen_at = ""
        self.store.write(state)

    async def stop_daemon(self) -> None:
        state = self.store.read()
        runtime = state.runtime
        port = int(runtime.daemon_port or self.default_port or 0)
        secret = runtime.secret
        expected_pids = {int(runtime.daemon_pid or 0)}
        if self._process and self._process.pid:
            expected_pids.add(int(self._process.pid))
        expected_pids.discard(0)

        trusted_by_secret = await self._daemon_accepts_secret(port, secret)
        if trusted_by_secret:
            await self._request_daemon_shutdown(port, secret)
            await self._wait_for_port_release(port, 3.0)

        await self._terminate_tracked_process()
        if (expected_pids or trusted_by_secret) and not await self._wait_for_port_release(port, 0.5):
            await self._kill_plugin_port_owners(
                port,
                expected_pids=expected_pids,
                trusted_by_secret=trusted_by_secret,
            )
            await self._wait_for_port_release(port, 2.0)

        self._clear_runtime_process_state()

    async def close(self) -> None:
        try:
            await self.stop_daemon()
        except Exception:
            log.warning("failed to stop qzone daemon during plugin close", exc_info=True)
        await self._client.aclose()
