"""One chronological undo / redo for note edits and graph-panel plot changes."""
from __future__ import annotations

import copy


class UndoManager:
    def __init__(self, win):
        self.win = win
        self.undo_stack: list[tuple] = []     # ("notes",) | ("plots", panel_id, plot specs)
        self.redo_stack: list[tuple] = []
        self._snap: dict[str, list] = {}      # last known plot specs per panel
        self._notes_depth = 0
        self._busy = False

    # --- recording ---
    def reset(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._snap = {pid: copy.deepcopy(p.to_dict()["plots"])
                      for pid, (p, _) in self.win.panels.items()}
        self._notes_depth = len(self.win.notes._undo)
        self._changed()

    def forget_panel(self, pid: str):
        self._snap.pop(pid, None)

    def on_notes_changed(self):
        if self._busy:
            return
        depth = len(self.win.notes._undo)
        if depth > self._notes_depth or (depth == self._notes_depth == 100):
            self.undo_stack.append(("notes",))
            self.redo_stack.clear()
        self._notes_depth = depth
        self._changed()

    def on_panel_changed(self, panel):
        if self._busy:
            return
        cur = copy.deepcopy(panel.to_dict()["plots"])
        prev = self._snap.get(panel.panel_id, [])
        if cur != prev:
            self.undo_stack.append(("plots", panel.panel_id, prev))
            self.redo_stack.clear()
        self._snap[panel.panel_id] = cur
        self._changed()

    # --- applying ---
    def undo(self):
        self._step(self.undo_stack, self.redo_stack, undo=True)

    def redo(self):
        self._step(self.redo_stack, self.undo_stack, undo=False)

    def _step(self, src: list, dst: list, undo: bool):
        while src:
            entry = src.pop()
            self._busy = True
            try:
                if entry[0] == "notes":
                    ok = self.win.notes.undo() if undo else self.win.notes.redo()
                    self._notes_depth = len(self.win.notes._undo)
                    if ok:
                        dst.append(entry)
                        break
                else:
                    _, pid, specs = entry
                    panel = self.win.panels.get(pid, (None, None))[0]
                    if panel is None or self.win.log is None:
                        continue
                    now = copy.deepcopy(panel.to_dict()["plots"])
                    panel.set_plots(copy.deepcopy(specs))
                    self._snap[pid] = copy.deepcopy(specs)
                    dst.append(("plots", pid, now))
                    break
            finally:
                self._busy = False
        self._changed()

    def _changed(self):
        self.win.update_undo_actions(bool(self.undo_stack), bool(self.redo_stack))
