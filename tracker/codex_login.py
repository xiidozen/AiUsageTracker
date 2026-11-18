"""Manage a ChatGPT device sign-in through the Codex app-server on stdio."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from .oauth import AuthError

DEVICE_URL = "https://auth.openai.com/codex/device"
START_TIMEOUT = 45
DEVICE_TIMEOUT = 15 * 60 + 15


class CodexAppServer:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.request_id = 0

    async def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self) -> dict:
        line = await self.process.stdout.readline()
        if not line:
            raise AuthError("The Codex sign-in process ended unexpectedly.")
        return json.loads(line)

    async def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        await self.send({"id": self.request_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise AuthError("Codex rejected the request. Check that device-code sign-in is enabled in ChatGPT.")
                return message["result"]


@asynccontextmanager
async def app_server(home: Path):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "codex", "app-server", "--listen", "stdio://",
        "-c", 'cli_auth_credentials_store="file"',
        "-c", 'forced_login_method="chatgpt"',
        env={**os.environ, "CODEX_HOME": str(home.resolve())},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    server = CodexAppServer(process)
    try:
        async with asyncio.timeout(START_TIMEOUT):
            await server.request("initialize", {
                "clientInfo": {"name": "ai-usage-tracker", "version": "1"},
            })
            await server.send({"method": "initialized"})
        yield server
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.wait()


class CodexLogin:
    def __init__(self, home: Path, on_connected):
        self.home = home
        self.on_connected = on_connected
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.attempt: str | None = None
        self.state = {"status": "idle"}

    async def start(self) -> dict:
        if self.lock.locked() or (self.task and not self.task.done()):
            raise AuthError("A Codex sign-in or refresh is already in progress.")
        self.attempt = secrets.token_urlsafe(24)
        self.state = {"status": "pending"}
        started = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run(started), name="codex-device-login")
        result = await asyncio.shield(started)
        if "error" in result:
            raise AuthError(result["error"])
        return result

    async def _run(self, started: asyncio.Future) -> None:
        error = "Codex sign-in could not start. Check that the Codex CLI is installed in the tracker environment."
        try:
            async with self.lock, app_server(self.home) as server:
                async with asyncio.timeout(START_TIMEOUT):
                    result = await server.request("account/login/start", {"type": "chatgptDeviceCode"})
                if result.get("verificationUrl") != DEVICE_URL or not result.get("userCode") or not result.get("loginId"):
                    raise AuthError("Codex returned an unexpected device sign-in response.")
                started.set_result({"attempt": self.attempt, "url": DEVICE_URL, "code": result["userCode"]})
                async with asyncio.timeout(DEVICE_TIMEOUT):
                    while True:
                        event = await server.receive()
                        if event.get("method") != "account/login/completed":
                            continue
                        params = event.get("params", {})
                        if params.get("loginId") != result["loginId"]:
                            continue
                        if not params.get("success") or not (self.home / "auth.json").is_file():
                            raise AuthError("ChatGPT sign-in did not complete. Start again and check your device-code settings.")
                        break
            self.on_connected()
            self.state = {"status": "connected"}
        except TimeoutError:
            self.state = {"status": "failed", "error": "Codex sign-in timed out. Start again to get a new code."}
        except AuthError as exc:
            self.state = {"status": "failed", "error": str(exc)}
        except (OSError, ValueError, KeyError):
            self.state = {"status": "failed", "error": error}
        finally:
            if not started.done():
                started.set_result({"error": self.state.get("error", error)})

    async def refresh(self) -> None:
        if self.lock.locked() or (self.task and not self.task.done()):
            raise AuthError("Codex sign-in is in progress. Try polling again after it completes.")
        try:
            async with self.lock, app_server(self.home) as server, asyncio.timeout(START_TIMEOUT):
                await server.request("account/read", {"refreshToken": True})
        except (AuthError, OSError, ValueError, KeyError, TimeoutError) as exc:
            raise AuthError("The Codex sign-in could not be refreshed. Sign in again from the tracker.") from exc

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
