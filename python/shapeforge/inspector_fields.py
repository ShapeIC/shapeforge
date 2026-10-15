"""Qt fields for inspector drafts. All references remain ordinary recipe dictionaries."""

from copy import deepcopy

import pya

from shapeforge.inspector import numeric


def button(layout, parent, text, callback):
    widget = pya.QPushButton(text, parent)
    widget.clicked = callback
    layout.addWidget(widget)
    return widget


def combo(parent, layout, choices, current, changed=None):
    widget = pya.QComboBox(parent)
    widget.addItems(choices)
    widget.currentIndex = choices.index(current) if current in choices else 0
    if changed is not None:
        widget.currentIndexChanged = changed
    layout.addWidget(widget)
    return widget


def line(parent, layout, text):
    widget = pya.QLineEdit(parent)
    widget.text = str(text)
    layout.addWidget(widget)
    return widget


def label(parent, layout, text):
    widget = pya.QLabel(text, parent)
    widget.wordWrap = True
    widget.textFormat = pya.Qt.PlainText
    layout.addWidget(widget)
    return widget


class Fold:
    def __init__(self, parent, layout, title, expanded=False, changed=None):
        self.title, self.expanded, self.changed = title, expanded, changed
        self.widget = pya.QWidget(parent)
        outer = pya.QVBoxLayout(self.widget)
        self.header = button(outer, self.widget, "", self.toggle)
        self.body = pya.QWidget(self.widget)
        self.layout = pya.QVBoxLayout(self.body)
        outer.addWidget(self.body)
        layout.addWidget(self.widget)
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        self.header.text = ("▼ " if self.expanded else "▶ ") + self.title
        self.body.visible = self.expanded

    def toggle(self, *args):
        self.set_expanded(not self.expanded)
        if self.changed is not None:
            self.changed(self.expanded)


class ValueField:
    def __init__(self, parent, layout, name, value, spec, host, port_width=False, project=False):
        self.original, self.spec, self.host = deepcopy(value), spec, host
        self.project, self.port_width = project, port_width
        self.widget = pya.QWidget(parent)
        body = pya.QVBoxLayout(self.widget)
        unit = " (" + spec["unit"] + ")" if spec.get("unit") else ""
        label(self.widget, body, name + unit)
        self.reference = deepcopy(value) if isinstance(value, dict) and value.get("kind") == "port_width" else None
        self.mode = None
        if project:
            modes = ["Remove override"] if spec.get("orphan") else ["Use child default", "Override"]
            self.mode = combo(self.widget, body, modes, "Override" if value is not None else modes[0], self.update_mode)
            self.modes = modes
            if spec.get("orphan"):
                # Removing a stale override must always be explicit.
                self.mode.addItem("Keep orphan override (invalid)")
                self.modes.append("Keep orphan override (invalid)")
                self.mode.currentIndex = 1
            else:
                label(self.widget, body, f"Child default: {spec['child_default']} (currently {spec['default_value']:g} µm)")
        elif port_width:
            self.modes = ["Value / Expression", "Port Width"]
            self.mode = combo(self.widget, body, self.modes, self.modes[1 if self.reference else 0], self.update_mode)
        if "choices" in spec or spec["type"] == "bool":
            self.choices = spec.get("choices", [False, True])
            names = [str(spec.get("choice_labels", {}).get(v, str(v).lower() if isinstance(v, bool) else v)) for v in self.choices]
            self.choice = combo(self.widget, body, names, names[self.choices.index(value)] if value in self.choices else names[0])
            self.text = None
        else:
            default = spec.get("default_value", "") if value is None else value
            if isinstance(default, dict):
                text = default.get("expression", "") if not self.reference else ""
            elif isinstance(default, (int, float)) and not isinstance(default, bool):
                text = f"{default / spec.get('scale', 1):.15g}"
            else:
                text = str(default)
            self.text = line(self.widget, body, text)
            self.choice = None
        if port_width:
            self.ref_label = label(self.widget, body, "")
            self.pick = button(body, self.widget, "Reference to Port…", self.pick_port)
        layout.addWidget(self.widget)
        self.update_mode()
        self.initial = self.state()

    def update_mode(self, *args):
        if not hasattr(self, "text"):
            return
        mode = self.modes[self.mode.currentIndex] if self.mode is not None else ""
        if self.text is not None:
            self.text.enabled = mode not in ("Use child default", "Port Width", "Remove override", "Keep orphan override (invalid)")
        if self.port_width:
            self.ref_label.text = (f"Port Width: {self.reference['reference_id']} / {self.reference['port']}" if self.reference else "No port reference selected.")

    def pick_port(self, *args):
        self.host.pick_port(self)

    def set_reference(self, reference):
        self.reference = deepcopy(reference)
        self.mode.currentIndex = self.modes.index("Port Width")
        self.update_mode()

    def state(self):
        return [self.mode.currentIndex if self.mode is not None else None,
                self.text.text if self.text is not None else self.choice.currentIndex, deepcopy(self.reference)]

    def read(self):
        if self.state() == self.initial:
            return deepcopy(self.original)
        if self.project:
            mode = self.modes[self.mode.currentIndex]
            if mode in ("Use child default", "Remove override"):
                return None
            if mode == "Keep orphan override (invalid)":
                raise ValueError("Remove the orphan override explicitly before applying.")
        if self.port_width and self.modes[self.mode.currentIndex] == "Port Width":
            if self.reference is None:
                raise ValueError("Select a reference port.")
            return deepcopy(self.reference)
        if self.choice is not None:
            return self.choices[self.choice.currentIndex]
        if self.spec["type"] == "str":
            return self.text.text
        return numeric(self.text.text, self.spec["type"], self.spec.get("scale", 1), self.spec.get("expression_dict", False))


class ReferenceField:
    def __init__(self, parent, layout, axis, original, host, default_anchor="center"):
        self.original, self.reference, self.host, self.axis = deepcopy(original), deepcopy(original), host, axis
        self.widget = pya.QWidget(parent)
        body = pya.QVBoxLayout(self.widget)
        self.owner = label(self.widget, body, "")
        button(body, self.widget, "Select Reference…", lambda *a: host.pick_object(self))
        self.anchors = ["left", "center", "right", "port"] if axis == "x" else ["bottom", "center", "top", "port"]
        self.anchor = combo(self.widget, body, self.anchors, (original or {}).get("anchor", default_anchor), self.update_mode)
        previous_port = (original or {}).get("port", "")
        self.port_names = host.reference_ports(original["reference_id"]) if original else []
        if previous_port and previous_port not in self.port_names:
            self.port_names.append(previous_port)
        self.port_names = self.port_names or [""]
        self.port = combo(self.widget, body, self.port_names, previous_port)
        label(self.widget, body, "Offset (µm): number or expression")
        self.offset = line(self.widget, body, (original or {}).get("offset_um", 0))
        layout.addWidget(self.widget)
        self.update_mode()
        self.initial = self.state()

    def update_mode(self, *args):
        if not hasattr(self, "port"):
            return
        self.owner.text = "Reference: " + (self.reference["reference_id"] if self.reference else "not selected")
        self.port.enabled = self.anchors[self.anchor.currentIndex] == "port"

    def set_reference(self, identity, ports):
        self.reference = {**(self.reference or {}), "reference_id": identity}
        previous = self.port_names[self.port.currentIndex]
        self.port_names = list(ports) or [""]
        self.port.clear()
        self.port.addItems(self.port_names)
        self.port.currentIndex = self.port_names.index(previous) if previous in self.port_names else 0
        if not ports and self.anchors[self.anchor.currentIndex] == "port":
            self.anchor.currentIndex = self.anchors.index("center")
        self.update_mode()

    def state(self):
        return [deepcopy(self.reference), self.anchors[self.anchor.currentIndex], self.port_names[self.port.currentIndex], self.offset.text]

    def read(self):
        if self.state() == self.initial and self.original is not None:
            return deepcopy(self.original)
        if self.reference is None:
            raise ValueError("Select a reference object.")
        result = deepcopy(self.reference)
        result["anchor"] = self.anchors[self.anchor.currentIndex]
        if result["anchor"] == "port":
            name = self.port_names[self.port.currentIndex]
            if not name:
                raise ValueError("The selected reference has no port. Select a linked instance.")
            result["port"] = name
        else:
            result.pop("port", None)
        if self.offset.text != str((self.original or {}).get("offset_um", 0)):
            result["offset_um"] = numeric(self.offset.text)
        return result


class CenterField:
    def __init__(self, parent, layout, axis, original, host):
        self.original = deepcopy(original)
        self.widget = pya.QWidget(parent)
        body = pya.QVBoxLayout(self.widget)
        label(self.widget, body, axis.upper() + " center")
        self.modes = ["Free", "Coordinate", "Object anchor"]
        initial = 0 if original is None else 2 if isinstance(original, dict) else 1
        self.mode = combo(self.widget, body, self.modes, self.modes[initial], self.update_mode)
        self.coordinate = line(self.widget, body, original if original is not None and not isinstance(original, dict) else 0)
        self.reference = ReferenceField(self.widget, body, axis, original if isinstance(original, dict) else None, host)
        layout.addWidget(self.widget)
        self.update_mode()
        self.initial = self.state()

    def update_mode(self, *args):
        if not hasattr(self, "reference"):
            return
        self.coordinate.visible = self.mode.currentIndex == 1
        self.reference.widget.visible = self.mode.currentIndex == 2

    def state(self):
        return [self.mode.currentIndex, self.coordinate.text, self.reference.state()]

    def read(self):
        if self.state() == self.initial:
            return deepcopy(self.original)
        if self.mode.currentIndex == 0:
            return None
        if self.mode.currentIndex == 1:
            return numeric(self.coordinate.text)
        return self.reference.read()
