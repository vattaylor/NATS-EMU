"""Start the NATS EMU window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from nats_emu.paths import CONFIG_PATH

    parser = argparse.ArgumentParser(
        description="Run a local NATS server that publishes emulated signals."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="JSON file for the signals and server settings (default: nats-emu.json)",
    )
    args = parser.parse_args(argv)
    try:
        from nats_emu.gui import run_app
    except ImportError as exc:
        print("PySide6 is required. Install it with: pip install PySide6", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    return run_app(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
