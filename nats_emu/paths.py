"""Locations for the config file and the downloaded NATS server."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "nats-emu.json"
BIN_DIR = ROOT / "bin"
