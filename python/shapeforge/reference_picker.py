"""Nonmodal reference selection with explicit disambiguation of overlapping objects."""

from dataclasses import dataclass, replace

import pya


@dataclass(frozen=True)
class ReferenceRequest:
    candidate_ids: tuple
    prompt: str
    required_ids: tuple = ()
    select_object: bool = False


class ReferencePicker:
    """Capture clicks, choose a valid hit, restore the destination and continue.

    The timer checks session health and processes queued clicks outside native
    mouse callbacks. Native selection remains available for non-GUI test views.
    """

    def __init__(self, session, request, completed, session_getter, main_window):
        self.session, self.request = session, request
        self.completed, self.session_getter = completed, session_getter
        self.main_window = main_window
        self.view = session.cellview.view()
        self.saved_selection = list(self.view.object_selection)
        objects = self.objects()
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
        self.click_source = None
        self.pending_click = None
        self.choice_dialog = None
        self.choice_point = None
        self.choice_ids = ()
        self.choice_buttons = {}

    def objects(self):
        return self.session.placement_objects(raw=True)

    def selected_item(self, obj, objects):
        return self.session.selected_placement_object(obj, objects)

    def start(self):
        try:
            self.dialog = pya.QDialog(self.main_window)
            self.dialog.windowTitle = "ShapeForge — Select Object" if self.request.select_object else "ShapeForge — Select Reference"
            self.dialog.modal = False
            layout = pya.QVBoxLayout(self.dialog)
            noun = "object" if self.request.select_object else "reference object"
            self.label = pya.QLabel(self.request.prompt + f"\nClick one {noun} in the layout. Escape cancels.", self.dialog)
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
            from shapeforge.reference_clicks import attach
            self.click_source = attach(self)
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
            objects = self.objects()
            if any(identity not in objects for identity in self.request.required_ids):
                raise RuntimeError("The destination or an original selected object was removed. Retry the action.")
        except Exception as error:
            self.finish(None)
            pya.MessageBox.warning("ShapeForge — Select Reference", str(error), pya.MessageBox.Ok)
            return
        if self.pending_click is not None:
            point, self.pending_click = self.pending_click, None
            self.handle_click(point, objects)
            return
        if self.click_source is not None or self.choice_dialog is not None:
            return
        selection = list(self.view.object_selection)
        if not selection:
            return
        try:
            if len(selection) != 1 or selection[0].cv_index != self.session.cellview.index():
                raise RuntimeError("Select exactly one reference in the working cell.")
            item = self.selected_item(selection[0], objects)
            if item["id"] not in self.request.candidate_ids:
                raise RuntimeError("This object cannot be used as the reference for this operation.")
        except Exception as error:
            self.label.text = self.request.prompt + "\n" + str(error) + "\nSelect another object, or press Escape to cancel."
            return
        self.finish(item["id"])

    def queue_click(self, point):
        if not self.finished:
            # Keep an independent value; event argument wrappers are temporary.
            self.pending_click = pya.DPoint(point.x, point.y)

    def hit_ids(self, point, objects):
        # Plugin positions are micron coordinates in the displayed context cell.
        # Convert to the session cell when it is shown through a hierarchy path.
        context = getattr(self.session.cellview, "context_dtrans", None)
        context = context() if callable(context) else context or pya.DCplxTrans()
        point = context.inverted() * point
        tolerance = 0.0
        if hasattr(self.view, "viewport_trans"):
            scale = abs(self.view.viewport_trans().mag) * abs(context.mag)
            if scale:
                tolerance = 2.0 / scale
        result = []
        for identity in self.request.candidate_ids:
            item = objects.get(identity)
            if item is None:
                continue
            if item["type"] == "instance":
                instance = item["instance"]
                if instance.is_regular_array() or instance.size() != 1:
                    if self.request.select_object and instance.dbbox().enlarged(tolerance).contains(point):
                        result.append(identity)
                    continue
                transform = instance.dcplx_trans
                if not transform.mag:
                    continue
                bounds = self.session.layout.cell(instance.cell_index).dbbox()
                local = transform.inverted() * point
                margin = tolerance / abs(transform.mag)
            else:
                bounds = item["shape"].box.to_dtype(self.session.layout.dbu)
                local, margin = point, tolerance
            if not bounds.empty() and bounds.enlarged(margin).contains(local):
                result.append(identity)
        return sorted(set(result))

    def describe_hit(self, item):
        identity = item["id"]
        if item["type"] == "box":
            layer = self.session.layout.get_info(item["layer"])
            name = f"Box · {layer.layer}/{layer.datatype}"
            bounds = item["shape"].box.to_dtype(self.session.layout.dbu)
        else:
            recipe = item.get("recipe") or {}
            name = recipe.get("primitive", recipe.get("function", recipe.get("generator", item["name"])))
            if recipe.get("provider") == "shapeforge-project":
                from pathlib import Path
                name = Path(recipe["source"]).name
            bounds = item["instance"].dbbox()
        short_id = identity[:8]
        if any(other != identity and other.startswith(short_id) for other in self.choice_ids):
            short_id = identity
        return f"{name} · {bounds.width():g} × {bounds.height():g} µm [{short_id}]"

    def handle_click(self, point, objects):
        try:
            self.close_choices()
            hits = self.hit_ids(point, objects)
            if len(hits) == 1:
                self.finish(hits[0])
            elif hits:
                self.choice_point = point
                self.choice_ids = tuple(hits)
                self.choice_dialog = pya.QDialog(self.main_window)
                self.choice_dialog.windowTitle = "ShapeForge — Choose Object" if self.request.select_object else "ShapeForge — Choose Reference"
                self.choice_dialog.modal = False
                layout = pya.QVBoxLayout(self.choice_dialog)
                layout.addWidget(pya.QLabel("Objects at this point:", self.choice_dialog))
                for identity in hits:
                    button = pya.QPushButton(self.describe_hit(objects[identity]), self.choice_dialog)
                    self.choice_buttons[identity] = button
                    button.toolTip = ("Selection candidate: " if self.request.select_object else "ShapeForge ID: ") + identity
                    button.clicked = lambda *args, identity=identity: self.choose(identity)
                    layout.addWidget(button)
                cancel = pya.QPushButton("Cancel", self.choice_dialog)
                cancel.clicked = self.cancel
                layout.addWidget(cancel)
                self.choice_dialog.rejected = self.cancel
                self.choice_dialog.show()
            else:
                self.label.text = self.request.prompt + "\nNo eligible reference at this point. Click another object, or press Escape to cancel."
        except Exception as error:
            self.close_choices()
            self.label.text = self.request.prompt + "\n" + str(error) + "\nClick another reference, or press Escape to cancel."

    def choose(self, identity):
        if self.finished:
            return
        try:
            self.session.validate()
            if (self.session_getter() is not self.session or self.main_window.current_view() != self.view
                    or self.session.cellview.cell != self.session.cell):
                self.finish(None)
                return
            objects = self.objects()
            if any(required not in objects for required in self.request.required_ids):
                self.finish(None)
                return
            if identity not in self.choice_ids or identity not in self.hit_ids(self.choice_point, objects):
                raise RuntimeError("This reference moved or was removed. Click another object.")
        except Exception as error:
            self.close_choices()
            self.label.text = self.request.prompt + "\n" + str(error)
            return
        self.finish(identity)

    def close_choices(self):
        if self.choice_dialog is not None:
            self.choice_dialog.hide()
            self.choice_dialog.deleteLater()
        self.choice_dialog, self.choice_point, self.choice_ids = None, None, ()
        self.choice_buttons = {}

    def cancel(self, *args):
        self.finish(None)

    def restore_selection(self):
        self.session.validate()
        if self.session.cellview.cell != self.session.cell:
            return
        objects = self.session.placement_objects(raw=True)
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
        self.pending_click = None
        self.close_choices()
        from shapeforge.reference_clicks import detach
        try:
            detach(self.click_source)
        except RuntimeError:
            pass  # A closed view may have already destroyed its native plugin.
        finally:
            self.click_source = None
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


class ObjectPicker(ReferencePicker):
    """Select direct native objects without requiring or changing persistent IDs."""

    def __init__(self, session, request, *args):
        self.candidates = self.scan(session)
        self.selected_identity = None
        request = replace(request, candidate_ids=tuple(self.candidates), select_object=True)
        super().__init__(session, request, *args)

    @staticmethod
    def scan(session):
        items = []
        for instance in session.cell.each_inst():
            items.append(dict(type="instance", instance=instance,
                              name=session.layout.cell(instance.cell_index).name))
        for layer in session.layout.layer_indexes():
            for shape in session.cell.shapes(layer).each():
                if shape.is_box():
                    items.append(dict(type="box", shape=shape, layer=layer))
        return {str(index + 1): dict(item, id=str(index + 1)) for index, item in enumerate(items)}

    def objects(self):
        # Tokens belong to native objects, not IDs that copies may share. Match
        # fresh wrappers so deleting a candidate never selects its replacement.
        current = self.scan(self.session)
        result = {}
        for identity, item in self.candidates.items():
            key = "instance" if item["type"] == "instance" else "shape"
            try:
                if any(other["type"] == item["type"] and other[key] == item[key]
                       for other in current.values()):
                    result[identity] = item
            except RuntimeError:
                continue
        return result

    def selected_item(self, obj, objects):
        if obj.top != self.session.cell.cell_index() or len(obj.path) != (1 if obj.is_cell_inst() else 0):
            raise RuntimeError("Select a direct box or instance in the working cell.")
        for item in objects.values():
            if item["type"] == "instance" and obj.is_cell_inst() and obj.inst() == item["instance"]:
                return item
            if item["type"] == "box" and not obj.is_cell_inst() and obj.shape == item["shape"]:
                return item
        raise RuntimeError("Select a box or instance in the working cell.")

    def restore_selection(self):
        self.session.validate()
        if self.session.cellview.cell != self.session.cell:
            return
        if self.selected_identity is None:
            self.view.object_selection = self.saved_selection
            return
        item = self.objects().get(self.selected_identity)
        if item is None:
            raise RuntimeError("The selected object was removed. Retry selection.")
        path = pya.ObjectInstPath()
        path.cv_index, path.top = self.session.cellview.index(), self.session.cell.cell_index()
        if item["type"] == "instance":
            path.append_path(pya.InstElement(item["instance"]))
        else:
            path.layer, path.shape = item["layer"], item["shape"]
        self.view.object_selection = [path]

    def finish(self, identity, notify=True):
        self.selected_identity = identity
        super().finish(identity, notify)
