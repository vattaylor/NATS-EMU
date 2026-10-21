"""Window behaviour without a visible display."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from nats_emu.gui import MainWindow  # noqa: E402
from nats_emu.model import SignalModel  # noqa: E402


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.config = Path(self._dir.name) / "nats-emu.json"
        self.win = MainWindow(self.config)
        self.win.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.win.close()
        self.app.processEvents()
        self._dir.cleanup()

    def test_tree_shows_yaw_rate_and_edits_its_range(self) -> None:
        yaw = self.win._items["vehicle.YawRate"]
        self.assertEqual(yaw.text(0), "YawRate")
        self.assertIn("deg/s", yaw.text(1))
        self.assertEqual(self.win.min_spin.value(), -20)
        self.assertEqual(self.win.max_spin.value(), 20)
        self.assertEqual(self.win.default_spin.value(), 0)
        self.assertGreaterEqual(self.win._items["vehicle.Door"].childCount(), 4)
        self.assertEqual(self.win._items["vehicle"].checkState(0), Qt.CheckState.PartiallyChecked)
        self.assertEqual(
            self.win._items["vehicle.Door.Driver"].checkState(0),
            Qt.CheckState.Unchecked,
        )

        self.win.max_spin.setValue(25)
        self.assertEqual(self.win.model.get("vehicle.YawRate").maximum, 25)

        yaw.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertFalse(self.win.model.get("vehicle.YawRate").enabled)
        self.assertFalse(self.win.enabled_box.isChecked())

        def fill_add() -> None:
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            dialog.subject.setText("vehicle.TestMode")
            dialog.kind.setCurrentIndex(1)
            dialog.accept()

        QTimer.singleShot(0, fill_add)
        self.win.add_button.click()
        added = self.win.model.get("vehicle.TestMode")
        self.assertIsNotNone(added)
        assert added is not None
        self.assertEqual(added.kind, "strings")
        self.assertIn("Off", added.choices)
        self.assertIn("vehicle.TestMode", self.win._items)

        def confirm_remove() -> None:
            box = self.app.activeModalWidget()
            self.assertIsInstance(box, QMessageBox)
            box.button(QMessageBox.StandardButton.Yes).click()

        QTimer.singleShot(0, confirm_remove)
        self.win.remove_button.click()
        self.assertIsNone(self.win.model.get("vehicle.TestMode"))

        self.win._save()
        loaded = SignalModel.load(self.config)
        self.assertEqual(loaded.get("vehicle.YawRate").maximum, 25)
        self.assertFalse(loaded.get("vehicle.YawRate").enabled)
        self.assertIsNone(loaded.get("vehicle.TestMode"))


if __name__ == "__main__":
    unittest.main()
