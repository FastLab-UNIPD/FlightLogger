"""Small dialog to choose the notes export format and whether to include PX4 messages."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout)

from .export import FORMATS

LEVELS = {"warnings": "Warnings and errors", "all": "All"}


class ExportNotesDialog(QDialog):
    def __init__(self, parent=None, fmt="txt", include_messages=False, level="warnings",
                 has_log=True, hidden_sets=False):
        super().__init__(parent)
        self.setWindowTitle("Export notes")
        form = QFormLayout(self)
        self.format_combo = QComboBox()
        for k, label in FORMATS.items():
            self.format_combo.addItem(label, k)
        self.format_combo.setCurrentIndex(max(0, self.format_combo.findData(fmt)))
        form.addRow("Format", self.format_combo)
        self.msgs_check = QCheckBox("Include PX4 messages")
        self.msgs_check.setChecked(include_messages and has_log)
        self.msgs_check.setEnabled(has_log)
        form.addRow(self.msgs_check)
        self.level_combo = QComboBox()
        for k, label in LEVELS.items():
            self.level_combo.addItem(label, k)
        self.level_combo.setCurrentIndex(max(0, self.level_combo.findData(level)))
        self.level_combo.setEnabled(self.msgs_check.isChecked())
        self.msgs_check.toggled.connect(self.level_combo.setEnabled)
        form.addRow("Messages", self.level_combo)
        self.visible_check = QCheckBox("Only visible note sets")
        self.visible_check.setChecked(hidden_sets)
        self.visible_check.setEnabled(hidden_sets)
        self.visible_check.setToolTip("Leave out notes of the sets hidden in the Notes panel")
        form.addRow(self.visible_check)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> tuple[str, bool, str]:
        return (self.format_combo.currentData(), self.msgs_check.isChecked(),
                self.level_combo.currentData())

    def only_visible(self) -> bool:
        return self.visible_check.isChecked()
