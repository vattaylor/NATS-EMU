# NATS EMU

NATS EMU is a desktop app that runs a real local NATS server and publishes made-up vehicle data on it. Use it when you need something to subscribe to — a dashboard, a logger, or another service — and you do not have a vehicle on the bench.

Signals are shown as a tree of NATS subjects, such as `vehicle.YawRate` and `vehicle.Door.Driver`. Each one can be switched on or off. A number signal stays inside a minimum and maximum and wanders around a centre value. `vehicle.YawRate` is centred on 0 deg/s and moves between -20 and +20. You choose how often it updates and how far it moves each time: a change range of 1 to 5 steps up or down by 1, 2, 3, 4, or 5 at random. A signal can instead publish text from a list, such as gear `P`, `R`, `N`, and `D`, either picking any entry, stepping to the next one, or holding one value.

Other NATS clients connect to the server and subscribe to those subject names. Nothing here talks to a real car. The values are generated in the app so you can exercise subscribers with data that looks like live vehicle signals.

## Run

```bash
./run.sh
```

That uses the system Python when PySide6 is already installed. Otherwise it creates `.venv` and installs `requirements.txt`.

You can also start it directly:

```bash
python3 -m nats_emu
python3 -m nats_emu --config /path/to/signals.json
```

PySide6 6.6 or newer is required.

Press **Start server**. The first start downloads NATS server 2.15.1 into `bin/` when `nats-server` is not already on `PATH`. Set `NATS_SERVER` to the full path of a binary if you want to use one you already have.

Clients connect to the address shown as **Client URL**, by default `nats://127.0.0.1:4222`.

```bash
nats sub -s nats://127.0.0.1:4222 vehicle.YawRate
```

**Listen** defaults to `127.0.0.1`. Use `0.0.0.0` to accept connections from other machines. The port and listen address can be changed while the server is stopped.

## Signals

The list is a tree of NATS subjects. Expand a group such as `vehicle` or `vehicle.Door`. The tick box publishes that signal. The tick on a group turns every signal under it on or off.

Select a signal to edit it:

| Setting | What it does |
| --- | --- |
| Min, max, default (centre) | Numeric range. The value wanders around the default. |
| Change by | A range for how far a number moves on each update. It goes up or down by a random amount inside the range. `1` to `5` jumps by 1, 2, 3, 4, or 5. Set both ends to `0` to hold the value still. |
| Every | How often the value is updated and published, in milliseconds. |
| Movement | **Drift** can land on any value between min and max, and wanders around the default. **Jump** steps by the change range and turns around at the ends. |
| Value type | **Number**, or **List of strings** such as `P`, `R`, `N`, `D`. |
| Text movement | Pick any entry, jump to the next entry, or hold the selected value. |

**Add signal** creates a subject such as `vehicle.TestMode`. Names use letters, numbers, `.`, `_`, and `-`. **Remove** deletes the selected signal, or every signal under a selected group.

**Payload** chooses the message body:

- **JSON** — `{"value": 0.4, "unit": "deg/s"}`. Text signals omit `unit`.
- **Raw value** — `0.4` or `D`.

The starter set includes yaw rate, speed, steering, acceleration, pedals, engine speed, gear, ignition, indicator, and doors. The door signals are present and switched off.

## Config

Settings are saved to `nats-emu.json` in this directory. Pass `--config` to use another file. A missing file is created from the built-in signals. If the file cannot be read, the window starts from those signals and does not overwrite it until you press **Save**.

## Tests

```bash
python3 -m unittest discover -s tests
```
