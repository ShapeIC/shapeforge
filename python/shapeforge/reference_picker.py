"""Nonmodal reference selection using KLayout's ordinary object selection."""

from dataclasses import dataclass

import pya


@dataclass(frozen=True)
class ReferenceRequest:
    candidate_ids: tuple
    prompt: str
    required_ids: tuple = ()


class ReferencePicker:
    """Wait for one eligible selection, restore the destination, then continue.

    A short Qt timer observes selection without replacing KLayout event handlers.
    No nested event loop, hit testing, layout edits, or undo transaction is needed.
    """

    def __init__(self, session, request, completed, session_getter, main_window):
        self.session, self.request = session, request
        self.completed, self.session_getter = completed, session_getter
        self.main_window = main_window
        self.view = session.cellview.view()
        self.saved_selection = list(self.view.object_selection)
        objects = session.placement_objects()
        self.saved_ids = []
        for obj in self.saved_selection:
            try:
                identity = session.selected_placement_object(obj, objects)["id"]
            except Exception:
                identity = None
            self.saved_ids.append(identity)
        self.mode = None
        self.dialog = self.timer = self.shortcut = None
        self.finished = False

    def start(self):
        try:
            self.dialog = pya.QDialog(self.main_window)
            self.dialog.windowTitle = "ShapeForge — Select Reference"
            self.dialog.modal = False
            layout = pya.QVBoxLayout(self.dialog)
            self.label = pya.QLabel(self.request.prompt + "\nClick one reference object in the layout. Escape cancels.", self.dialog)
            self.label.wordWrap = True
            layout.addWidget(self.label)
            cancel = pya.QPushButton("Cancel", self.dialog)
            cancel.clicked = self.cancel
            layout.addWidget(cancel)
            self.dialog.rejected = self.cancel
            self.shortcut = pya.QShortcut(pya.QKeySequence("Esc"), self.main_window)
            self.shortcut.context = pya.Qt.ApplicationShortcut
            self.shortcut.activated = self.cancel
            self.shortcut.activatedAmbiguously = self.cancel
            self.timer = pya.QTimer(self.dialog)
            self.timer.timeout = self.poll
            self.mode = self.view.mode_name()
            self.view.switch_mode("select")
            self.view.object_selection = []
            self.dialog.show()
            self.timer.start(100)
        except Exception:
            self.finish(None, notify=False)
            raise

    def poll(self, *args):
        if self.finished:
            return
        try:
            self.session.validate()
            if self.session_getter() is not self.session:
                self.finish(None)
                return
            if self.main_window.current_view() != self.view:
                self.finish(None)
                return
            if self.session.cellview.cell != self.session.cell:
                self.finish(None)
                return
            objects = self.session.placement_objects()
            if any(identity not in objects for identity in self.request.required_ids):
                raise RuntimeError("The destination or an original selected object was removed. Retry the action.")
        except Exception as error:
            self.finish(None)
            pya.MessageBox.warning("ShapeForge — Select Reference", str(error), pya.MessageBox.Ok)
            return
        selection = list(self.view.object_selection)
        if not selection:
            return
        try:
            if len(selection) != 1 or selection[0].cv_index != self.session.cellview.index():
                raise RuntimeError("Select exactly one reference in the working cell.")
            item = self.session.selected_placement_object(selection[0], objects)
            if item["id"] not in self.request.candidate_ids:
                raise RuntimeError("This object cannot be used as the reference for this operation.")
        except Exception as error:
            self.label.text = self.request.prompt + "\n" + str(error) + "\nSelect another object, or press Escape to cancel."
            return
        self.finish(item["id"])

    def cancel(self, *args):
        self.finish(None)

    def restore_selection(self):
        self.session.validate()
        if self.session.cellview.cell != self.session.cell:
            return
        objects = self.session.placement_objects()
        selection = []
        for identity, original in zip(self.saved_ids, self.saved_selection):
            if identity is None:
                selection.append(original)
                continue
            item = objects.get(identity)
            if item is None:
                continue
            obj = pya.ObjectInstPath()
            obj.cv_index = self.session.cellview.index()
            obj.top = self.session.cell.cell_index()
            if item["type"] == "instance":
                obj.path = [pya.InstElement(item["instance"])]
            else:
                obj.layer = original.layer
                obj.shape = item["shape"]
            selection.append(obj)
        self.view.object_selection = selection

    def finish(self, identity, notify=True):
        if self.finished:
            return
        self.finished = True
        if self.timer is not None:
            self.timer.stop()
        if self.shortcut is not None:
            self.shortcut.enabled = False
            self.shortcut.deleteLater()
        try:
            self.restore_selection()
        except Exception:
            # A closed layout or removed object cannot have its selection restored.
            identity = None
        try:
            if self.mode is not None:
                self.view.switch_mode(self.mode)
        except Exception:
            pass
        if self.dialog is not None:
            self.dialog.hide()
            self.dialog.deleteLater()
        if notify:
            self.completed(identity)
