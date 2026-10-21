"""Starter vehicle signals.

Numbers wander around their default. ``vehicle.YawRate`` is centred on 0 and
stays between -20 and +20 degrees per second. Text signals publish one entry
from their list.
"""

from __future__ import annotations

from nats_emu.model import Signal


def _number(
    subject: str,
    minimum: float,
    maximum: float,
    default: float,
    step: float,
    interval_ms: int,
    unit: str,
    motion: str = "any",
    enabled: bool = True,
) -> Signal:
    return Signal(
        subject=subject,
        enabled=enabled,
        kind="numeric",
        minimum=minimum,
        maximum=maximum,
        default=default,
        step_min=step,
        step_max=step,
        interval_ms=interval_ms,
        motion=motion,
        unit=unit,
        current=default,
    )


def _text(
    subject: str,
    choices: list[str],
    selected: str,
    interval_ms: int,
    motion: str,
    enabled: bool = True,
) -> Signal:
    return Signal(
        subject=subject,
        enabled=enabled,
        kind="strings",
        choices=list(choices),
        selected=selected,
        current=selected,
        interval_ms=interval_ms,
        motion=motion,
    )


def default_signals() -> list[Signal]:
    doors = ["Closed", "Open"]
    return [
        _number("vehicle.YawRate", -20, 20, 0, 0.5, 100, "deg/s"),
        _number("vehicle.Speed", 0, 240, 50, 1.2, 200, "km/h"),
        _number("vehicle.SteeringWheelAngle", -540, 540, 0, 8, 100, "deg"),
        _number("vehicle.LateralAcceleration", -10, 10, 0, 0.25, 100, "m/s²"),
        _number("vehicle.LongitudinalAcceleration", -8, 5, 0, 0.2, 100, "m/s²"),
        _number("vehicle.AcceleratorPedal", 0, 100, 18, 2, 150, "%"),
        _number("vehicle.BrakePedal", 0, 100, 0, 5, 200, "%", motion="jump"),
        _number("vehicle.EngineSpeed", 0, 7000, 800, 40, 100, "rpm"),
        _text("vehicle.Gear", ["P", "R", "N", "D"], "P", 3000, "jump"),
        _text("vehicle.Ignition", ["Off", "Accessory", "On", "Start"], "On", 1000, "hold"),
        _text("vehicle.Indicator", ["Off", "Left", "Right", "Hazard"], "Off", 2000, "any"),
        _text("vehicle.Door.Driver", doors, "Closed", 4000, "any", enabled=False),
        _text("vehicle.Door.Passenger", doors, "Closed", 4000, "any", enabled=False),
        _text("vehicle.Door.RearLeft", doors, "Closed", 4000, "any", enabled=False),
        _text("vehicle.Door.RearRight", doors, "Closed", 4000, "any", enabled=False),
    ]
