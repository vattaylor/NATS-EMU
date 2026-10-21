"""A small NATS client for publishing and subscribing.

The wire protocol is the core NATS protocol: INFO, CONNECT, PUB, SUB, PING
and PONG. It is enough for this emulator and does not need the nats-py package.
"""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import urlparse


class NatsError(ConnectionError):
    """The server refused the connection or the socket closed."""


class Subscription:
    def __init__(self, queue: asyncio.Queue[tuple[str, bytes] | None], owner: NatsClient) -> None:
        self._queue = queue
        self._owner = owner

    async def next_message(self, timeout: float = 5) -> tuple[str, bytes]:
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError as exc:
            raise NatsError("Timed out waiting for a NATS message") from exc
        if item is None:
            raise NatsError(self._owner.error or "NATS connection closed")
        return item


class NatsClient:
    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.error: str | None = None
        self.max_payload = 1024 * 1024
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._subs: dict[str, asyncio.Queue[tuple[str, bytes] | None]] = {}
        self._sid = 0
        self._pong_waiters: list[asyncio.Future[None]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    async def connect(self, url: str, timeout: float = 5) -> None:
        host, port = split_server(url)
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise NatsError(f"Could not connect to {url}: {exc}") from exc
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
        except asyncio.TimeoutError as exc:
            raise NatsError(f"{url} did not send a server greeting") from exc
        if not line.startswith(b"INFO "):
            raise NatsError(f"Unexpected greeting from NATS server: {line!r}")
        try:
            info = json.loads(line.decode().split(" ", 1)[1])
        except (UnicodeError, json.JSONDecodeError, IndexError) as exc:
            raise NatsError("Could not read the NATS server greeting") from exc
        if info.get("tls_required"):
            raise NatsError("This server requires TLS, which NATS EMU does not use")
        if info.get("auth_required"):
            raise NatsError("This server requires authentication")
        self.max_payload = int(info.get("max_payload") or self.max_payload)
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._read_loop(), name="nats-read"),
            loop.create_task(self._write_loop(), name="nats-write"),
        ]
        greeting = {
            "verbose": False,
            "pedantic": False,
            "lang": "nats-emu",
            "version": "1",
            "protocol": 1,
            "name": "nats-emu",
            "echo": True,
        }
        message = ("CONNECT " + json.dumps(greeting, separators=(",", ":")) + "\r\nPING\r\n").encode()
        waiter = self._expect_pong()
        await self._out.put(message)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError as exc:
            raise NatsError(f"{url} did not answer the connection") from exc

    async def publish(self, subject: str, payload: bytes) -> None:
        self._raise_if_down()
        if len(payload) > self.max_payload:
            raise NatsError("Payload is larger than the server allows")
        header = f"PUB {subject} {len(payload)}\r\n".encode()
        await self._out.put(header + payload + b"\r\n")

    async def subscribe(self, subject: str) -> Subscription:
        self._raise_if_down()
        self._sid += 1
        sid = str(self._sid)
        queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        self._subs[sid] = queue
        await self._out.put(f"SUB {subject} {sid}\r\n".encode())
        return Subscription(queue, self)

    async def flush(self, timeout: float = 2) -> None:
        self._raise_if_down()
        waiter = self._expect_pong()
        await self._out.put(b"PING\r\n")
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError as exc:
            raise NatsError("Timed out waiting for the NATS server") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._out.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if len(self._tasks) >= 2:
            try:
                await asyncio.wait_for(self._tasks[1], 1)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        writer = self.writer
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail(self.error or "closed")

    def _raise_if_down(self) -> None:
        if self.error:
            raise NatsError(self.error)
        if self._closed:
            raise NatsError("NATS connection closed")

    def _expect_pong(self) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pong_waiters.append(future)
        return future

    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = message
        while self._pong_waiters:
            future = self._pong_waiters.pop(0)
            if not future.done():
                future.set_exception(NatsError(message))
        for queue in self._subs.values():
            queue.put_nowait(None)
        self._subs.clear()

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self._closed:
                line = await self.reader.readline()
                if not line:
                    self._fail("NATS connection closed")
                    return
                await self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(str(exc))

    async def _handle_line(self, line: bytes) -> None:
        if line.startswith(b"MSG "):
            await self._handle_msg(line)
        elif line.startswith(b"PING"):
            self._out.put_nowait(b"PONG\r\n")
        elif line.startswith(b"PONG"):
            if self._pong_waiters:
                future = self._pong_waiters.pop(0)
                if not future.done():
                    future.set_result(None)
        elif line.startswith(b"-ERR"):
            self._fail(line.decode(errors="replace").strip())
        elif line.startswith((b"+OK", b"INFO")):
            return

    async def _handle_msg(self, line: bytes) -> None:
        assert self.reader is not None
        parts = line.decode(errors="replace").strip().split(" ")
        if len(parts) < 4:
            self._fail(f"Bad MSG from server: {line!r}")
            return
        subject = parts[1]
        sid = parts[2]
        try:
            size = int(parts[-1])
        except ValueError:
            self._fail(f"Bad MSG from server: {line!r}")
            return
        payload = await self.reader.readexactly(size)
        trailer = await self.reader.readexactly(2)
        if trailer != b"\r\n":
            self._fail("Truncated NATS message")
            return
        queue = self._subs.get(sid)
        if queue is not None:
            queue.put_nowait((subject, payload))

    async def _write_loop(self) -> None:
        assert self.writer is not None
        try:
            while True:
                item = await self._out.get()
                if item is None:
                    return
                self.writer.write(item)
                await self.writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(f"Could not write to the NATS server: {exc}")


def split_server(url: str) -> tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"nats://{url}")
    host = parsed.hostname
    port = parsed.port or 4222
    if not host:
        raise NatsError(f"Not a NATS url: {url}")
    return host, port


def client_url(host: str, port: int) -> str:
    """Url clients on this machine should use. A wildcard bind still dials localhost."""
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"nats://{host}:{port}"
