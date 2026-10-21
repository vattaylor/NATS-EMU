"""Signal definitions, motion, and the saved configuration."""

from __future__ import annotations

import json
import math
import random
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_NUMERIC_MOTIONS = {"any", "jump"}
_TEXT_MOTIONS = {"any", "jump", "hold"}
_EDITABLE = {
    "enabled",
    "kind",
    "minimum",
    "maximum",
    "default",
    "step_min",
    "step_max",
    "interval_ms",
    "motion",
    "unit",
    "choices",
    "selected",
    "subject",
}


class ConfigError(ValueError):
    """The signal settings are not usable."""


def validate_subject(subject: str) -> None:
    """Reject subjects NATS could not publish, and names the tree cannot show."""
    if not isinstance(subject, str):
        raise ConfigError("Subject must be text")
    subject = subject.strip()
    if not subject or len(subject) > 255:
        raise ConfigError("Enter a subject such as vehicle.YawRate")
    parts = subject.split(".")
    if any(not _TOKEN.fullmatch(part) for part in parts):
        raise ConfigError("Use names like vehicle.YawRate — letters, numbers, dots, _ and -")


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _finite(value: float, label: str) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{label} must be a finite number")
    return number


def format_number(value: float) -> str:
    value = round(float(value), 4)
    if abs(value) < 5e-7:
        value = 0.0
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def format_value(sig: Signal) -> str:
    if sig.kind == "numeric":
        text = format_number(float(sig.current))
        if sig.unit:
            return f"{text} {sig.unit}"
        return text
    return str(sig.current)


def encode_payload(sig: Signal, payload: str) -> bytes:
    """Encode one publish. JSON carries the unit; raw is the value alone."""
    if sig.kind == "numeric":
        number = round(float(sig.current), 6)
        if abs(number) < 5e-7:
            number = 0.0
        value: object = int(number) if number == int(number) else number
        raw = format_number(number)
    else:
        value = str(sig.current)
        raw = value
    if payload == "raw":
        return raw.encode()
    body: dict[str, object] = {"value": value}
    if sig.kind == "numeric" and sig.unit:
        body["unit"] = sig.unit
    return json.dumps(body, separators=(",", ":")).encode()


def next_numeric(sig: Signal, rng: random.Random) -> float:
    """Next number for one update.

    The move is up or down by a random amount between ``step_min`` and
    ``step_max``. Whole-number ends jump by a whole amount, so 1 to 5 jumps
    by 1, 2, 3, 4, or 5. Drift prefers the direction back toward the default.
    """
    current = _clamp(float(sig.current), sig.minimum, sig.maximum)
    return _move(sig, current, rng, bias=sig.motion != "jump")


def _move(sig: Signal, current: float, rng: random.Random, *, bias: bool) -> float:
    amount = _change_amount(sig, rng)
    if amount <= 0:
        return current
    direction = _change_direction(sig, current, rng, bias=bias)
    for _ in range(2):
        nxt = current + direction * amount
        if sig.minimum - 1e-9 <= nxt <= sig.maximum + 1e-9:
            return _clamp(nxt, sig.minimum, sig.maximum)
        direction *= -1
    return current


def _change_amount(sig: Signal, rng: random.Random) -> float:
    low = min(sig.step_min, sig.step_max)
    high = max(sig.step_min, sig.step_max)
    low = max(0.0, low)
    high = max(0.0, high)
    if high <= 0:
        return 0.0
    if _whole(low) and _whole(high):
        return float(rng.randint(int(round(low)), int(round(high))))
    if math.isclose(low, high, abs_tol=1e-12):
        return high
    return rng.uniform(low, high)


def _whole(value: float) -> bool:
    return abs(value - round(value)) < 1e-9


def _change_direction(sig: Signal, current: float, rng: random.Random, *, bias: bool) -> float:
    if not bias:
        return rng.choice((-1.0, 1.0))
    span = sig.maximum - sig.minimum
    if span <= 0:
        return rng.choice((-1.0, 1.0))
    lean = (float(sig.default) - current) / span
    if rng.random() < _clamp(0.5 + 0.45 * lean, 0.05, 0.95):
        return 1.0
    return -1.0


def next_text(sig: Signal, rng: random.Random) -> str:
    """Next list entry. ``any`` picks from the list, ``jump`` takes the next one."""
    choices = sig.choices
    if not choices:
        return str(sig.current)
    if sig.motion == "hold":
        return sig.selected if sig.selected in choices else choices[0]
    if sig.motion == "jump":
        try:
            index = choices.index(str(sig.current))
        except ValueError:
            index = -1
        return choices[(index + 1) % len(choices)]
    if len(choices) == 1:
        return choices[0]
    pick = rng.choice(choices)
    if pick == sig.current:
        pick = rng.choice([item for item in choices if item != sig.current])
    return pick


@dataclass
class Signal:
    subject: str
    enabled: bool = True
    kind: str = "numeric"
    minimum: float = 0.0
    maximum: float = 100.0
    default: float = 0.0
    step_min: float = 1.0
    step_max: float = 1.0
    interval_ms: int = 200
    motion: str = "any"
    unit: str = ""
    choices: list[str] = field(default_factory=list)
    selected: str = ""
    current: float | str = 0.0
    next_due: float = 0.0
    armed: bool = False
    hold_once: bool = False

    def copy(self) -> Signal:
        return replace(self, choices=list(self.choices))

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "enabled": self.enabled,
            "kind": self.kind,
            "minimum": _json_number(self.minimum),
            "maximum": _json_number(self.maximum),
            "default": _json_number(self.default),
            "step_min": _json_number(self.step_min),
            "step_max": _json_number(self.step_max),
            "interval_ms": self.interval_ms,
            "motion": self.motion,
            "unit": self.unit,
            "choices": list(self.choices),
            "selected": self.selected,
        }


def _json_number(value: float) -> float | int:
    number = round(float(value), 6)
    if number == int(number):
        return int(number)
    return number


def normalize(sig: Signal, *, reset_current: bool = False) -> None:
    """Make one signal consistent. Raises ConfigError when the range is impossible."""
    validate_subject(sig.subject)
    if sig.kind not in {"numeric", "strings"}:
        raise ConfigError(f"{sig.subject}: type must be a number or a list of strings")
    sig.interval_ms = int(sig.interval_ms)
    if sig.interval_ms < 20 or sig.interval_ms > 3_600_000:
        raise ConfigError(f"{sig.subject}: interval must be between 20 ms and 1 hour")
    sig.unit = str(sig.unit).strip()
    if len(sig.unit) > 32:
        raise ConfigError(f"{sig.subject}: unit is too long")

    if sig.kind == "numeric":
        sig.minimum = _finite(sig.minimum, f"{sig.subject}: min")
        sig.maximum = _finite(sig.maximum, f"{sig.subject}: max")
        sig.default = _finite(sig.default, f"{sig.subject}: default")
        sig.step_min = _finite(sig.step_min, f"{sig.subject}: change from")
        sig.step_max = _finite(sig.step_max, f"{sig.subject}: change to")
        if sig.minimum > sig.maximum:
            raise ConfigError(f"{sig.subject}: min cannot be above max")
        if not (sig.minimum <= sig.default <= sig.maximum):
            raise ConfigError(f"{sig.subject}: default must be between min and max")
        if sig.step_min < 0 or sig.step_max < 0:
            raise ConfigError(f"{sig.subject}: change by cannot be negative")
        if sig.step_min > sig.step_max:
            raise ConfigError(f"{sig.subject}: change from cannot be above change to")
        if sig.motion not in _NUMERIC_MOTIONS:
            sig.motion = "any"
        if reset_current or isinstance(sig.current, str):
            sig.current = float(sig.default)
        else:
            sig.current = _clamp(_finite(sig.current, f"{sig.subject}: value"), sig.minimum, sig.maximum)
        return

    cleaned: list[str] = []
    seen: set[str] = set()
    for choice in sig.choices:
        text = str(choice).strip()
        if not text or text in seen:
            continue
        if len(text) > 80:
            raise ConfigError(f"{sig.subject}: a text value is too long")
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        cleaned = ["Off", "On"]
    if len(cleaned) > 100:
        raise ConfigError(f"{sig.subject}: use at most 100 text values")
    sig.choices = cleaned
    if sig.selected not in cleaned:
        sig.selected = cleaned[0]
    if sig.motion not in _TEXT_MOTIONS:
        sig.motion = "any"
    if reset_current or sig.current not in cleaned:
        sig.current = sig.selected


def _step_end(data: dict, key: str, subject: str) -> float:
    """Read one end of the change range. An older file stores a single ``step``."""
    if key in data and data[key] is not None:
        return _finite(data[key], f"{subject}: {key}")
    if "step" in data and data["step"] is not None:
        return _finite(data["step"], f"{subject}: step")
    return 1.0


def _optional_number(data: dict, key: str, subject: str, fallback: float) -> float:
    if key not in data or data[key] is None:
        return fallback
    return _finite(data[key], f"{subject}: {key}")


def signal_from_dict(data: dict) -> Signal:
    if not isinstance(data, dict) or "subject" not in data:
        raise ConfigError("Each signal needs a subject")
    subject = str(data["subject"]).strip()
    validate_subject(subject)
    kind = data.get("kind", "numeric")
    if kind not in {"numeric", "strings"}:
        raise ConfigError(f"{subject}: type must be a number or a list of strings")
    choices = data.get("choices", [])
    if not isinstance(choices, list):
        raise ConfigError(f"{subject}: choices must be a list")
    interval = data.get("interval_ms", 200)
    try:
        interval_ms = int(interval)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{subject}: interval must be a whole number of milliseconds") from exc
    sig = Signal(
        subject=subject,
        enabled=bool(data.get("enabled", True)),
        kind=kind,
        minimum=_optional_number(data, "minimum", subject, 0.0),
        maximum=_optional_number(data, "maximum", subject, 100.0),
        default=_optional_number(data, "default", subject, 0.0),
        step_min=_step_end(data, "step_min", subject),
        step_max=_step_end(data, "step_max", subject),
        interval_ms=interval_ms,
        motion=str(data.get("motion", "any")),
        unit=str(data.get("unit", "")),
        choices=[str(item) for item in choices],
        selected=str(data.get("selected", "")),
    )
    normalize(sig, reset_current=True)
    return sig


def new_signal(subject: str, kind: str) -> Signal:
    """A sensible starter for a subject the user just added."""
    validate_subject(subject)
    if kind == "strings":
        sig = Signal(
            subject=subject,
            kind="strings",
            choices=["Off", "On"],
            selected="Off",
            motion="hold",
            interval_ms=1000,
            current="Off",
        )
    else:
        leaf = subject.rsplit(".", 1)[-1].lower()
        if "yaw" in leaf:
            sig = Signal(
                subject=subject,
                kind="numeric",
                minimum=-20,
                maximum=20,
                default=0,
                step_min=0.5,
                step_max=0.5,
                interval_ms=100,
                motion="any",
                unit="deg/s",
                current=0,
            )
        else:
            sig = Signal(subject=subject, kind="numeric", current=0)
    normalize(sig, reset_current=True)
    return sig


def check_host(host: str) -> str:
    host = host.strip()
    if not host or any(ch.isspace() for ch in host) or len(host) > 255:
        raise ConfigError("Listen address must be a host such as 127.0.0.1 or 0.0.0.0")
    return host


def check_port(port: int) -> int:
    try:
        number = int(port)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Port must be a whole number") from exc
    if not 1 <= number <= 65535:
        raise ConfigError("Port must be between 1 and 65535")
    return number


def check_payload(payload: str) -> str:
    if payload not in {"json", "raw"}:
        raise ConfigError("Payload must be json or raw")
    return payload


class SignalModel:
    """The signals being emulated. Safe to edit while the publisher is running."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.host = "127.0.0.1"
        self.port = 4222
        self.payload = "json"
        self._signals: list[Signal] = []
        self._index: dict[str, Signal] = {}

    def list_signals(self) -> list[Signal]:
        with self.lock:
            return [sig.copy() for sig in self._signals]

    def get(self, subject: str) -> Signal | None:
        with self.lock:
            sig = self._index.get(subject)
            return None if sig is None else sig.copy()

    def add(self, sig: Signal) -> Signal:
        with self.lock:
            stored = sig.copy()
            normalize(stored, reset_current=True)
            if stored.subject in self._index:
                raise ConfigError(f"{stored.subject} already exists")
            self._signals.append(stored)
            self._index[stored.subject] = stored
            return stored.copy()

    def remove_under(self, path: str) -> int:
        with self.lock:
            kept: list[Signal] = []
            removed = 0
            for sig in self._signals:
                if _under(sig.subject, path):
                    removed += 1
                    self._index.pop(sig.subject, None)
                else:
                    kept.append(sig)
            self._signals = kept
            return removed

    def signals_under(self, path: str) -> list[Signal]:
        with self.lock:
            return [sig.copy() for sig in self._signals if _under(sig.subject, path)]

    def update(self, subject: str, **fields: object) -> Signal:
        with self.lock:
            current = self._index.get(subject)
            if current is None:
                raise KeyError(subject)
            unknown = set(fields) - _EDITABLE
            if unknown:
                raise ConfigError(f"Unknown setting {sorted(unknown)[0]}")
            trial = current.copy()
            was_enabled = current.enabled
            for key, value in fields.items():
                if key == "choices":
                    value = list(value)  # type: ignore[arg-type]
                setattr(trial, key, value)
            normalize(trial)
            if trial.kind == "strings" and trial.motion == "hold":
                trial.current = trial.selected
            if "enabled" in fields and trial.enabled and not was_enabled:
                trial.hold_once = True
                trial.next_due = 0.0
            else:
                trial.hold_once = current.hold_once
                trial.next_due = current.next_due
            trial.armed = current.armed
            new_subject = trial.subject
            if new_subject != subject and new_subject in self._index:
                raise ConfigError(f"{new_subject} already exists")
            if new_subject != subject:
                del self._index[subject]
            self._index[new_subject] = trial
            index = self._signals.index(current)
            self._signals[index] = trial
            return trial.copy()

    def set_enabled_under(self, path: str, enabled: bool) -> None:
        with self.lock:
            for sig in self._signals:
                if not _under(sig.subject, path) or sig.enabled == enabled:
                    continue
                sig.enabled = enabled
                if enabled:
                    sig.hold_once = True
                    sig.next_due = 0.0

    def set_current(self, subject: str, value: object) -> Signal:
        with self.lock:
            sig = self._index.get(subject)
            if sig is None:
                raise KeyError(subject)
            if sig.kind == "numeric":
                number = _finite(value, "Value")  # type: ignore[arg-type]
                if not (sig.minimum <= number <= sig.maximum):
                    raise ConfigError("Value must be between min and max")
                sig.current = number
            else:
                text = str(value)
                if text not in sig.choices:
                    raise ConfigError("Value is not in the list")
                sig.current = text
                if sig.motion == "hold":
                    sig.selected = text
            if sig.enabled:
                sig.hold_once = True
                sig.next_due = 0.0
            return sig.copy()

    def reset_current(self, subject: str) -> Signal:
        with self.lock:
            sig = self._index.get(subject)
            if sig is None:
                raise KeyError(subject)
            sig.current = float(sig.default) if sig.kind == "numeric" else sig.selected
            if sig.enabled:
                sig.hold_once = True
                sig.next_due = 0.0
            return sig.copy()

    def set_connection(self, *, host: str | None = None, port: int | None = None, payload: str | None = None) -> None:
        with self.lock:
            if host is not None:
                self.host = check_host(host)
            if port is not None:
                self.port = check_port(port)
            if payload is not None:
                self.payload = check_payload(payload)

    def tick(self, now: float) -> list[tuple[str, bytes]]:
        """Advance every signal that is due and return the messages to publish."""
        ready: list[tuple[str, bytes]] = []
        with self.lock:
            payload = self.payload
            for sig in self._signals:
                if not sig.enabled or sig.next_due > now:
                    continue
                sig.next_due = now + sig.interval_ms / 1000.0
                if sig.hold_once:
                    sig.hold_once = False
                    sig.armed = True
                elif sig.armed:
                    if sig.kind == "numeric":
                        sig.current = next_numeric(sig, self.rng)
                    else:
                        sig.current = next_text(sig, self.rng)
                else:
                    # First sample is the centre (or the selected text), then it moves.
                    sig.armed = True
                ready.append((sig.subject, encode_payload(sig, payload)))
        return ready

    def seconds_until_next(self, now: float) -> float:
        with self.lock:
            dues = [sig.next_due for sig in self._signals if sig.enabled]
        if not dues:
            return 0.25
        return min(0.25, max(0.01, min(dues) - now))

    def snapshot_rows(self) -> list[tuple[str, str, bool]]:
        with self.lock:
            return [(sig.subject, format_value(sig), sig.enabled) for sig in self._signals]

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "host": self.host,
                "port": self.port,
                "payload": self.payload,
                "signals": [sig.to_dict() for sig in self._signals],
            }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SignalModel:
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Could not read {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name} must contain a JSON object")
        model = cls()
        model.host = check_host(str(data.get("host", "127.0.0.1")))
        model.port = check_port(data.get("port", 4222))
        model.payload = check_payload(str(data.get("payload", "json")))
        signals = data.get("signals", [])
        if not isinstance(signals, list):
            raise ConfigError("signals must be a list")
        for item in signals:
            model.add(signal_from_dict(item))
        return model


def _under(subject: str, path: str) -> bool:
    return subject == path or subject.startswith(path + ".")


def load_config(path: Path) -> tuple[SignalModel, str | None, bool]:
    """Return the model, a warning, and whether it is safe to overwrite ``path``.

    A missing file starts from the built-in vehicle signals and may be saved.
    A broken file also starts from those signals, but is not overwritten until
    the user saves on purpose.
    """
    path = Path(path)
    if not path.exists():
        model = _default_model()
        return model, None, True
    try:
        return SignalModel.load(path), None, True
    except ConfigError as exc:
        return _default_model(), str(exc), False


def _default_model() -> SignalModel:
    from nats_emu.defaults import default_signals

    model = SignalModel()
    for sig in default_signals():
        model.add(sig)
    return model
