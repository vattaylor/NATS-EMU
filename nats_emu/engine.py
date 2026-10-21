"""Run nats-server and publish the emulated signals onto it."""

from __future__ import annotations

import atexit
import asyncio
import collections
import subprocess
import threading
import time
from pathlib import Path

from nats_emu.client import NatsClient, NatsError, client_url
from nats_emu.model import SignalModel


class Engine:
    """Owns the server process and the publish loop.

    The window polls ``snapshot`` and ``logs_since``. Those are safe to call
    from the UI thread while the server is running.
    """

    def __init__(self, model: SignalModel) -> None:
        self.model = model
        self.lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()
        self.state = "stopped"
        self.detail = "Stopped"
        self.url = ""
        self.published = 0
        self.rate = 0.0
        self._rate_window: collections.deque[tuple[float, int]] = collections.deque()
        self._logs: collections.deque[tuple[int, str]] = collections.deque(maxlen=400)
        self._log_i = 0
        atexit.register(self._shutdown_proc)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "url": self.url,
                "published": self.published,
                "rate": self.rate,
            }

    def add_log(self, line: str) -> None:
        text = str(line).strip()
        if not text:
            return
        with self.lock:
            self._log_i += 1
            self._logs.append((self._log_i, text))

    def logs_since(self, seen: int) -> list[tuple[int, str]]:
        with self.lock:
            return [(number, line) for number, line in self._logs if number > seen]

    def fail(self, message: str) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._set_error(message)

    def start(self, binary: Path) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._halt.clear()
        with self.model.lock:
            host = self.model.host
            port = self.model.port
        with self.lock:
            self.state = "starting"
            self.detail = "Starting NATS server"
            self.url = ""
            self.published = 0
            self.rate = 0.0
            self._rate_window.clear()
        self.add_log(f"Starting nats-server on {host}:{port}")
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(binary, host, port),
            name="nats-emu",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._shutdown_proc()
        with self.lock:
            self.state = "stopped"
            self.detail = "Stopped"
            self.url = ""
            self.rate = 0.0

    def _thread_main(self, binary: Path, host: str, port: int) -> None:
        try:
            self._proc = subprocess.Popen(
                [str(binary), "-a", host, "-p", str(port), "--name", "nats-emu"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(binary.parent),
            )
        except OSError as exc:
            self._set_error(f"Could not start nats-server: {exc}")
            return
        threading.Thread(target=self._pump_logs, name="nats-emu-log", daemon=True).start()
        try:
            asyncio.run(self._publish_loop(host, port))
        except Exception as exc:
            if not self._halt.is_set():
                self._set_error(str(exc))
        finally:
            self._shutdown_proc()
            with self.lock:
                # Keep an error on screen until the user starts again or presses stop.
                if self._halt.is_set() or self.state != "error":
                    self.state = "stopped"
                    self.detail = "Stopped"
                    self.url = ""
                    self.rate = 0.0

    async def _publish_loop(self, host: str, port: int) -> None:
        url = client_url(host, port)
        client = NatsClient()
        deadline = time.monotonic() + 8
        last_error = "nats-server did not accept a connection"
        while time.monotonic() < deadline and not self._halt.is_set():
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(self._recent_log() or f"nats-server exited ({proc.returncode})")
            try:
                await client.connect(url, timeout=1)
                break
            except (NatsError, OSError) as exc:
                last_error = str(exc)
                await client.close()
                client = NatsClient()
                await asyncio.sleep(0.15)
        else:
            if self._halt.is_set():
                await client.close()
                return
            raise RuntimeError(last_error)

        self._set_running(url)
        try:
            while not self._halt.is_set():
                proc = self._proc
                if proc is not None and proc.poll() is not None:
                    raise RuntimeError("NATS server stopped")
                if client.error:
                    raise RuntimeError(client.error)
                now = time.monotonic()
                batch = self.model.tick(now)
                for subject, payload in batch:
                    await client.publish(subject, payload)
                if batch:
                    self._note_published(len(batch))
                delay = self.model.seconds_until_next(time.monotonic())
                await asyncio.sleep(delay)
        finally:
            await client.close()

    def _set_running(self, url: str) -> None:
        with self.model.lock:
            payload = self.model.payload
        with self.lock:
            self.state = "running"
            self.detail = "Listening"
            self.url = url
        self.add_log(f"Server is ready at {url}")
        if payload == "json":
            self.add_log('Publishing JSON, for example {"value": 0, "unit": "deg/s"}')
        else:
            self.add_log("Publishing the raw value as text")

    def _set_error(self, message: str) -> None:
        with self.lock:
            self.state = "error"
            self.detail = message
            self.rate = 0.0
        self.add_log(f"error: {message}")

    def _note_published(self, count: int) -> None:
        now = time.monotonic()
        with self.lock:
            self.published += count
            self._rate_window.append((now, count))
            while self._rate_window and now - self._rate_window[0][0] > 2:
                self._rate_window.popleft()
            total = sum(item_count for _, item_count in self._rate_window)
            self.rate = total / 2.0

    def _recent_log(self) -> str:
        with self.lock:
            if not self._logs:
                return ""
            return self._logs[-1][1]

    def _pump_logs(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.add_log(line)
        except (OSError, ValueError):
            return

    def _shutdown_proc(self) -> None:
        with self._proc_lock:
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            stdout = proc.stdout
            self._proc = None
        if stdout is not None and not stdout.closed:
            stdout.close()
