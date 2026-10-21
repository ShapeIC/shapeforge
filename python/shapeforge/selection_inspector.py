"""Collapsible selection inspector with per-object drafts and explicit Apply/Revert."""

from copy import deepcopy
import json

import pya

from shapeforge import inspector as model
from shapeforge.inspector_fields import Fold, ValueField, ReferenceField, CenterField, button, combo, label


GROUPS = {
    "geometry": ("Geometry / Generation Parameters", ("edit_primitive_parameters", "make_box_parametric", "define_box_extents", "define_box_center", "relink_project", "edit_project_port")),
    "position": ("Center / Position", ("define_via_center",)),
    "placement": ("Placement", ("panel_place_by_edges", "panel_place_by_ports", "placement_info", "edit_placement", "remove_placement")),
    "array": ("Array", ("create_array", "edit_array", "dissolve_array")),
    "ports": ("Ports", ("show_ports", "hide_ports", "delete_project_port")),
    "information": ("Information / Actions", ("rename_instance", "selection_info", "box_info", "keep_selected_box_id", "keep_selected_instance_id", "regenerate_placement")),
}


class BoxAxis:
    def __init__(self, form, layout, axis):
        self.form, self.axis = form, axis
        definition = form.base["definition"]
        key = (form.base["kind"], "axis_" + axis)
        self.fold = Fold(form.widget, layout, "Axis " + axis.upper(), form.controller.expanded.get(key, True),
                         lambda value: form.controller.expanded.__setitem__(key, value))
        body, parent = self.fold.layout, self.fold.body
        self.extents = definition.get(f"{axis}_extents")
        self.mode = combo(parent, body, ["Dimension", "Two extents"], "Two extents" if self.extents else "Dimension", self.update_mode)
        dimension = "width_um" if axis == "x" else "height_um"
        # When replacing extents, start with the actual size, not an inactive old dimension.
        value = form.base["size"][0 if axis == "x" else 1] if self.extents else definition[dimension]
        self.dimension = ValueField(parent, body, "Width" if axis == "x" else "Height", value,
                                    {"type": "float", "unit": "µm"}, form, port_width=True)
        self.center = CenterField(parent, body, axis, definition.get(f"{axis}_center"), form)
        self.ends = {}
        for end, anchor in (("min", "left" if axis == "x" else "bottom"), ("max", "right" if axis == "x" else "top")):
            title = label(parent, body, end.capitalize() + " extent")
            ref = ReferenceField(parent, body, axis, (self.extents or {}).get(end), form, anchor)
            self.ends[end] = (title, ref)
        self.update_mode()
        self.initial = self.state()

    def update_mode(self, *args):
        if not hasattr(self, "ends"):
            return
        dimension = self.mode.currentIndex == 0
        self.dimension.widget.visible = self.center.widget.visible = dimension
        for title, ref in self.ends.values():
            title.visible = ref.widget.visible = not dimension

    def state(self):
        return [self.mode.currentIndex, self.dimension.state(), self.center.state(),
                {name: ref.state() for name, (_, ref) in self.ends.items()}]

    def merge(self, result):
        if self.state() == self.initial:
            return
        axis = self.axis
        if self.mode.currentIndex == 0:
            result["width_um" if axis == "x" else "height_um"] = self.dimension.read()
            if self.extents is not None:
                result.pop(f"{axis}_extents", None)
            if self.center.state() != self.center.initial:
                center = self.center.read()
                if center is None:
                    result.pop(f"{axis}_center", None)
                else:
                    result[f"{axis}_center"] = center
        else:
            result[f"{axis}_extents"] = {name: ref.read() for name, (_, ref) in self.ends.items()}
            result.pop(f"{axis}_center", None)


class Form:
    def __init__(self, controller, parent, layout, base, section):
        self.controller, self.base, self.section = controller, deepcopy(base), section
        specs = None
        if section == "generation":
            from shapeforge import runtime
            with runtime.scope(controller.panel.session_getter(), refresh=getattr(controller, "reload_sources", False), publish=True):
                specs = model.generation_specs(base)
            controller.reload_sources = False
        self.widget = pya.QWidget(parent)
        self.layout = pya.QVBoxLayout(self.widget)
        self.fields, self.axes, self.centers = {}, {}, {}
        self.conflict, self.error = False, ""
        if section == "generation":
            provider = base["recipe"]["provider"]
            project = provider == "shapeforge-project"
            for name, original_spec in specs.items():
                spec = {**original_spec, "expression_dict": provider in ("external-python", "shapeic-cellkit-technology")}
                self.fields[name] = ValueField(self.widget, self.layout, name, base["values"].get(name), spec, self,
                    port_width=base["kind"] == "via_stack" and name in ("width", "height"), project=project)
            if not specs:
                label(self.widget, self.layout, "No external generation parameters. Expose parameters in the child project and save it first.")
            self.effective = label(self.widget, self.layout, "Effective at last generation: " + json.dumps(
                base["recipe"].get("arguments", base["recipe"].get("geometry", {})), ensure_ascii=False))
        elif section == "geometry":
            for axis in ("x", "y"):
                self.axes[axis] = BoxAxis(self, self.layout, axis)
        else:
            for axis in ("x", "y"):
                self.centers[axis] = CenterField(self.widget, self.layout, axis, (base["position"] or {}).get(f"{axis}_center"), self)
            if base["kind"] == "project_port":
                label(self.widget, self.layout, "Orientation (degrees)")
                self.angles = [0, 90, 180, 270]
                if base["angle"] not in self.angles:
                    self.angles.append(base["angle"])
                self.angle = combo(self.widget, self.layout, [str(v) for v in self.angles], str(base["angle"]))
        self.status = label(self.widget, self.layout, "")
        self.apply_button = button(self.layout, self.widget, "Apply", self.apply)
        self.revert_button = button(self.layout, self.widget, "Revert / Reload", self.revert)
        layout.addWidget(self.widget)
        self.initial = self.state()
        self.sync(base)

    def state(self):
        return {"fields": {name: field.state() for name, field in self.fields.items()},
                "axes": {axis: field.state() for axis, field in self.axes.items()},
                "centers": {axis: field.state() for axis, field in self.centers.items()},
                "angle": self.angle.currentIndex if hasattr(self, "angle") else None}

    def dirty(self):
        return self.state() != self.initial

    def sync(self, current):
        if current["tokens"][self.section] != self.base["tokens"][self.section] and self.dirty():
            self.conflict = True
        reason = current["reasons"][self.section]
        self.apply_button.enabled = not self.conflict and reason is None
        self.status.text = ("This section changed outside the inspector. Use Revert / Reload before applying. Your draft is preserved."
                            if self.conflict else reason or self.error)

    def read(self):
        if self.section == "generation":
            values = deepcopy(self.base["values"])
            for name, field in self.fields.items():
                value = field.read()
                if value is None and self.base["recipe"]["provider"] == "shapeforge-project":
                    values.pop(name, None)
                else:
                    values[name] = value
            return values
        if self.section == "geometry":
            definition = deepcopy(self.base["definition"])
            for axis in self.axes.values():
                axis.merge(definition)
            return definition
        position = deepcopy(self.base["position"])
        if any(field.state() != field.initial for field in self.centers.values()):
            position = {"version": 1}
            for axis, field in self.centers.items():
                value = field.read()
                if value is not None:
                    position[f"{axis}_center"] = value
            if len(position) == 1:
                position = None
        angle = self.angles[self.angle.currentIndex] if hasattr(self, "angle") and self.angles[self.angle.currentIndex] != self.base["angle"] else None
        return {"position": position, "angle": angle}

    def apply(self, *args):
        try:
            self.error = ""
            if self.conflict:
                raise ValueError("Use Revert / Reload before applying this conflicted draft.")
            session = self.controller.panel.session_getter()
            current = self.controller.current
            if current is None or current["key"] != self.base["key"]:
                raise ValueError("Select the original object before applying this draft.")
            if not self.dirty():
                return
            model.apply(session, self.base, self.section, self.read())
            self.controller.discard(self)
            self.controller.panel.refresh()
        except Exception as error:
            self.error = str(error)
            self.status.text = self.error

    def revert(self, *args):
        if self.section == "generation":
            self.controller.reload_sources = True
        self.controller.discard(self)
        self.controller.panel.refresh()

    def reference_ports(self, identity):
        session = self.controller.panel.session_getter()
        try:
            item = session.placement_objects(raw=True)[identity]
            return [port.name for port in item["component"].ports] if item["component"] is not None else []
        except (KeyError, RuntimeError, ValueError):
            return []

    def pick_object(self, field):
        self.controller.pick(self, field, False)

    def pick_port(self, field):
        self.controller.pick(self, field, True)


class SelectionInspector:
    def __init__(self, panel, parent, layout):
        from shapeforge.panel import ACTION_TITLES
        self.panel = panel
        self.forms, self.folds, self.expanded, self.current = {}, {}, {}, None
        self.form_layouts = {}
        self.information = None
        for group, (title, actions) in GROUPS.items():
            fold = Fold(parent, layout, title, group == "geometry", lambda value, group=group: self.toggle(group, value))
            self.folds[group] = fold
            area = pya.QWidget(fold.body)
            self.form_layouts[group] = (area, pya.QVBoxLayout(area))
            fold.layout.addWidget(area)
            for name in actions:
                title = ACTION_TITLES[name]
                panel.buttons[name] = button(fold.layout, fold.body, title, lambda *a, name=name: panel.execute(name))
            if group == "information":
                self.information = label(fold.body, fold.layout, "")

    def toggle(self, group, expanded):
        if self.current is not None:
            self.expanded[(self.current["kind"], group)] = expanded
            self.show_form(group)

    def section_for(self, group):
        if self.current is None or not self.current["id"]:
            return None
        if group == "geometry":
            if self.current["kind"] == "box":
                return "geometry" if self.current["definition"] is not None else None
            if self.current["recipe"].get("provider") in ("shapeic-cellkit", "external-python", "shapeic-cellkit-technology", "shapeforge-project"):
                return "generation"
        if group == "position" and self.current["kind"] in ("via_stack", "project_port"):
            return "position"
        return None

    def form_key(self, section):
        return (*self.current["key"], section)

    def discard(self, form):
        self.panel._state_token = None
        key = (*form.base["key"], form.section)
        self.forms.pop(key, None)
        form.widget.visible = False
        form.widget.deleteLater()

    def show_form(self, group):
        section = self.section_for(group)
        if section is None or not self.folds[group].expanded:
            return
        key = self.form_key(section)
        form = self.forms.get(key)
        if form is not None and form.base["tokens"][section] != self.current["tokens"][section] and not form.dirty() and not form.conflict:
            self.discard(form)
            form = None
        if form is None:
            parent, layout = self.form_layouts[group]
            try:
                form = Form(self, parent, layout, self.current, section)
                self.forms[key] = form
                parent.toolTip = ""
            except Exception as error:
                # Contract discovery is lazy; errors must not hide the actions.
                self.folds[group].header.toolTip = str(error)
                self.errors[group].text = str(error)
                return
        self.errors[group].text = ""
        form.sync(self.current)
        form.widget.visible = True

    def refresh(self, session, context):
        self.current = model.snapshot(session, context) if session is not None else None
        for form in list(self.forms.values()):
            if self.current is None or form.base["key"] != self.current["key"]:
                if not form.dirty() and not form.conflict:
                    self.discard(form)
                else:
                    form.widget.visible = False
        if not hasattr(self, "errors"):
            self.errors = {group: label(fold.body, fold.layout, "") for group, fold in self.folds.items()}
        for group, fold in self.folds.items():
            applicable = any(name in context["actions"] for name in GROUPS[group][1]) or self.section_for(group) is not None
            fold.widget.visible = applicable
            kind = self.current["kind"] if self.current else ""
            fold.set_expanded(self.expanded.get((kind, group), group == "geometry"))
            if applicable:
                self.show_form(group)
        self.information.text = context["summary"]

    def pick(self, form, field, port_width):
        from shapeforge import ui
        base_key = form.base["key"]
        def execute(session):
            from shapeforge.selection import selection_context
            if selection_context(session)["key"] != base_key:
                raise ValueError("Select the original object before choosing a reference.")
            objects = session.placement_objects(raw=True)
            candidates = [item for identity, item in objects.items() if identity != form.base["id"] and
                (not port_width or item["type"] == "instance" and item["component"] is not None and len(item["component"].ports))]
            if not candidates:
                raise ValueError("No other registered reference object is available.")
            identity = yield from ui.pick_reference(session, candidates, "Select the reference object.", [form.base["id"]])
            if identity is None:
                return
            item = session.placement_objects(raw=True)[identity]
            ports = [port.name for port in item["component"].ports] if item["component"] is not None else []
            if port_width:
                name = pya.InputDialog.ask_item("ShapeForge — Port Width", "Port:", ports, 0)
                if name is None:
                    return
                field.set_reference({"kind": "port_width", "reference_id": identity, "port": name})
            else:
                field.set_reference(identity, ports)
        ui.run_session_action(execute, session=self.panel.session_getter())
