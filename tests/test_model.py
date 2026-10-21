"""Motion, payload, and config tests. These do not open a window or a socket."""

from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from nats_emu.defaults import default_signals
from nats_emu.model import (
    ConfigError,
    SignalModel,
    encode_payload,
    load_config,
    new_signal,
    next_numeric,
    next_text,
)
from nats_emu.natsbin import parse_checksums, release_asset


class MotionTests(unittest.TestCase):
    def test_default_yaw_rate_is_centred(self) -> None:
        yaw = {sig.subject: sig for sig in default_signals()}["vehicle.YawRate"]
        self.assertEqual(yaw.minimum, -20)
        self.assertEqual(yaw.maximum, 20)
        self.assertEqual(yaw.default, 0)
        self.assertEqual(yaw.motion, "any")
        self.assertTrue(yaw.enabled)
        self.assertEqual(yaw.unit, "deg/s")

    def test_drift_stays_in_range_and_near_the_centre(self) -> None:
        sig = new_signal("vehicle.YawRate", "numeric")
        rng = random.Random(1)
        values = []
        for _ in range(4000):
            previous = float(sig.current)
            sig.current = next_numeric(sig, rng)
            delta = abs(float(sig.current) - previous)
            self.assertLessEqual(delta, sig.step_max + 1e-9)
            if delta > 1e-9:
                self.assertGreaterEqual(delta, sig.step_min - 1e-9)
            self.assertGreaterEqual(sig.current, -20)
            self.assertLessEqual(sig.current, 20)
            values.append(float(sig.current))
        self.assertLess(min(values), -2)
        self.assertGreater(max(values), 2)
        self.assertLess(abs(sum(values) / len(values)), 6)

    def test_jump_moves_by_exactly_one_step(self) -> None:
        sig = new_signal("vehicle.Brake", "numeric")
        sig.minimum = -20
        sig.maximum = 20
        sig.default = 0
        sig.step_min = 0.5
        sig.step_max = 0.5
        sig.motion = "jump"
        sig.current = 0
        rng = random.Random(2)
        for _ in range(40):
            nxt = next_numeric(sig, rng)
            self.assertTrue(math.isclose(abs(nxt - float(sig.current)), 0.5, abs_tol=1e-9))
            sig.current = nxt

    def test_jump_holds_when_the_step_cannot_fit(self) -> None:
        sig = new_signal("vehicle.Brake", "numeric")
        sig.minimum = 0
        sig.maximum = 0.2
        sig.default = 0
        sig.step_min = 1
        sig.step_max = 1
        sig.motion = "jump"
        sig.current = 0
        self.assertEqual(next_numeric(sig, random.Random(0)), 0)

    def test_text_cycle_and_hold(self) -> None:
        sig = new_signal("vehicle.Gear", "strings")
        sig.choices = ["P", "R", "N", "D"]
        sig.selected = "P"
        sig.current = "P"
        sig.motion = "jump"
        self.assertEqual(next_text(sig, random.Random(0)), "R")
        sig.current = "D"
        self.assertEqual(next_text(sig, random.Random(0)), "P")
        sig.motion = "hold"
        sig.current = "R"
        self.assertEqual(next_text(sig, random.Random(0)), "P")
        sig.motion = "any"
        seen = {next_text(sig, random.Random(i)) for i in range(30)}
        self.assertTrue(seen <= set(sig.choices))
        self.assertGreater(len(seen), 1)

    def test_zero_step_does_not_move(self) -> None:
        sig = new_signal("vehicle.Speed", "numeric")
        sig.step_min = 0
        sig.step_max = 0
        sig.current = 12
        self.assertEqual(next_numeric(sig, random.Random(0)), 12)

    def test_change_range_jumps_by_a_random_whole_amount(self) -> None:
        sig = new_signal("vehicle.Speed", "numeric")
        sig.minimum = 0
        sig.maximum = 500
        sig.default = 50
        sig.step_min = 1
        sig.step_max = 5
        sig.motion = "jump"
        sig.current = 50
        rng = random.Random(4)
        sizes = set()
        signs = set()
        for _ in range(80):
            nxt = next_numeric(sig, rng)
            delta = nxt - float(sig.current)
            size = abs(delta)
            self.assertGreaterEqual(size, 1 - 1e-9)
            self.assertLessEqual(size, 5 + 1e-9)
            self.assertTrue(math.isclose(size, round(size), abs_tol=1e-9))
            sizes.add(int(round(size)))
            signs.add(1 if delta > 0 else -1)
            sig.current = nxt
        self.assertEqual(sizes, {1, 2, 3, 4, 5})
        self.assertEqual(signs, {-1, 1})


class PublishTests(unittest.TestCase):
    def test_first_sample_is_the_centre_then_it_moves(self) -> None:
        model = SignalModel(random.Random(0))
        model.add(new_signal("vehicle.YawRate", "numeric"))
        first = json.loads(model.tick(0)[0][1])
        self.assertEqual(first, {"value": 0, "unit": "deg/s"})
        self.assertEqual(model.tick(0.05), [])
        second = json.loads(model.tick(0.1)[0][1])
        self.assertGreaterEqual(second["value"], -20)
        self.assertLessEqual(second["value"], 20)
        self.assertNotEqual(second["value"], 0)

    def test_disabled_signal_is_not_published(self) -> None:
        model = SignalModel()
        model.add(new_signal("vehicle.YawRate", "numeric"))
        model.update("vehicle.YawRate", enabled=False)
        self.assertEqual(model.tick(10), [])

    def test_raw_payload_is_the_value_only(self) -> None:
        sig = new_signal("vehicle.YawRate", "numeric")
        sig.current = -1.5
        self.assertEqual(encode_payload(sig, "raw"), b"-1.5")
        gear = new_signal("vehicle.Gear", "strings")
        gear.choices = ["P", "R"]
        gear.selected = "R"
        gear.current = "R"
        self.assertEqual(encode_payload(gear, "raw"), b"R")

    def test_group_switch_does_not_touch_a_longer_name(self) -> None:
        model = SignalModel()
        model.add(new_signal("vehicle.Door.Driver", "strings"))
        model.add(new_signal("vehicle.DoorHandle", "numeric"))
        model.set_enabled_under("vehicle.Door", False)
        self.assertFalse(model.get("vehicle.Door.Driver").enabled)
        self.assertTrue(model.get("vehicle.DoorHandle").enabled)


class ConfigTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.json"
            model = SignalModel()
            model.port = 4223
            model.add(new_signal("vehicle.YawRate", "numeric"))
            model.save(path)
            loaded = SignalModel.load(path)
            yaw = loaded.get("vehicle.YawRate")
            self.assertIsNotNone(yaw)
            assert yaw is not None
            self.assertEqual(yaw.minimum, -20)
            self.assertEqual(yaw.maximum, 20)
            self.assertEqual(yaw.default, 0)
            self.assertEqual(yaw.step_min, 0.5)
            self.assertEqual(yaw.step_max, 0.5)
            self.assertEqual(loaded.port, 4223)

    def test_older_config_uses_step_as_both_ends_of_the_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.json"
            path.write_text(
                json.dumps(
                    {
                        "signals": [
                            {
                                "subject": "vehicle.Speed",
                                "kind": "numeric",
                                "minimum": 0,
                                "maximum": 100,
                                "default": 10,
                                "step": 5,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            speed = SignalModel.load(path).get("vehicle.Speed")
            assert speed is not None
            self.assertEqual(speed.step_min, 5)
            self.assertEqual(speed.step_max, 5)

    def test_inverted_range_is_rejected(self) -> None:
        model = SignalModel()
        model.add(new_signal("vehicle.YawRate", "numeric"))
        with self.assertRaises(ConfigError):
            model.update("vehicle.YawRate", minimum=10, maximum=0)

    def test_missing_file_uses_the_built_in_signals(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "nope.json"
        model, warning, autosave = load_config(missing)
        self.assertIsNone(warning)
        self.assertTrue(autosave)
        self.assertIsNotNone(model.get("vehicle.YawRate"))

    def test_release_asset_name(self) -> None:
        asset, binary = release_asset("2.15.1", "linux", "x86_64")
        self.assertEqual(asset, "nats-server-v2.15.1-linux-amd64.tar.gz")
        self.assertEqual(binary, "nats-server")
        parsed = parse_checksums(f"abc123  {asset}\n")
        self.assertEqual(parsed[asset], "abc123")


if __name__ == "__main__":
    unittest.main()
