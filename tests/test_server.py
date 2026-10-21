"""Publish emulated signals through a real local NATS server."""

from __future__ import annotations

import asyncio
import json
import socket
import time
import unittest

from nats_emu.client import NatsClient
from nats_emu.defaults import default_signals
from nats_emu.engine import Engine
from nats_emu.model import SignalModel
from nats_emu.natsbin import ensure_nats_server
from nats_emu.paths import BIN_DIR


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerTests(unittest.TestCase):
    def test_yaw_rate_is_published_on_its_subject(self) -> None:
        binary = ensure_nats_server(BIN_DIR)
        model = SignalModel()
        for sig in default_signals():
            model.add(sig)
        model.port = _free_port()
        model.update("vehicle.Gear", interval_ms=100)
        engine = Engine(model)
        engine.start(binary)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = engine.snapshot()["state"]
                if state in {"running", "error"}:
                    break
                time.sleep(0.05)
            snap = engine.snapshot()
            self.assertEqual(snap["state"], "running", snap["detail"])
            samples = asyncio.run(_collect(str(snap["url"])))
        finally:
            engine.stop()
        self.assertGreaterEqual(len(samples["vehicle.YawRate"]), 3)
        for value in samples["vehicle.YawRate"]:
            self.assertGreaterEqual(value, -20)
            self.assertLessEqual(value, 20)
        self.assertTrue(any(value != 0 for value in samples["vehicle.YawRate"]))
        self.assertIn(samples["vehicle.Gear"][0], {"P", "R", "N", "D"})


async def _collect(url: str) -> dict[str, list]:
    client = NatsClient()
    await client.connect(url)
    yaw = await client.subscribe("vehicle.YawRate")
    gear = await client.subscribe("vehicle.Gear")
    await client.flush()
    found: dict[str, list] = {"vehicle.YawRate": [], "vehicle.Gear": []}
    for _ in range(8):
        subject, payload = await yaw.next_message(timeout=2)
        body = json.loads(payload)
        if body.get("unit") != "deg/s":
            raise AssertionError(body)
        found[subject].append(body["value"])
    subject, payload = await gear.next_message(timeout=2)
    found[subject].append(json.loads(payload)["value"])
    await client.close()
    return found


if __name__ == "__main__":
    unittest.main()
