"""One ShapeForge dock containing the parameter editor and selection actions."""

import pya
from shapeforge.parameter_panel import ParameterEditor
from shapeforge.selection import selection_context
from shapeforge.ports_panel import PortsEditor

ACTION_TITLES = {
    "edit_project_port": "Edit Project Port…", "delete_project_port": "Delete Project Port",
    "make_box_parametric": "Make Box Parametric…",
    "define_box_extents": "Define Box Extents…",
    "define_box_center": "Define Box Center…",
    "box_info": "Box Info",
    "edit_primitive_parameters": "Edit Primitive Parameters…",
    "selection_info": "Selection Info",
    "show_ports": "Show Ports", "hide_ports": "Hide Ports",
    "panel_place_by_edges": "Place by Edges…", "panel_place_by_ports": "Place by Ports…",
    "placement_info": "Placement Info", "edit_placement": "Edit Placement…",
    "remove_placement": "Remove Placement", "regenerate_placement": "Regenerate Placement",
    "keep_selected_box_id": "Keep Selected Box ID", "keep_selected_instance_id": "Keep Selected Instance ID",
}


class ShapeForgePanel:
    def __init__(self, main_window, session_getter, callbacks=None):
        self.main_window, self.session_getter = main_window, session_getter
        if callbacks is None:
            from shapeforge import ui
            callbacks = {name: getattr(ui, name) for name in ACTION_TITLES}
        self.callbacks = callbacks
        self.signature, self.selection_key, self.context = None, None, None
        self.dock = pya.QDockWidget("ShapeForge", main_window)
        self.dock.objectName = "shapeforge_parameters"  # Keep the saved dock position.
        self.tabs = pya.QTabWidget(self.dock)
        self.parameters = ParameterEditor(self.tabs, session_getter)
        self.parameters.on_change = self.refresh
        self.tabs.addTab(self.parameters.widget, "Parameters")
        self.selection = pya.QScrollArea(self.tabs)
        self.selection.widgetResizable = True
        content = pya.QWidget(self.selection)
        layout = pya.QVBoxLayout(content)
        self.summary = pya.QLabel(content)
        self.summary.wordWrap = True
        self.summary.textFormat = pya.Qt.PlainText
        layout.addWidget(self.summary)
        self.buttons = {}
        for name, title in ACTION_TITLES.items():
            button = pya.QPushButton(title, content)
            button.clicked = lambda *args, action=name: self.execute(action)
            layout.addWidget(button)
            self.buttons[name] = button
        layout.addStretch(1)
        self.selection.widget = content
        self.tabs.addTab(self.selection, "Selection")
        self.ports = PortsEditor(self.tabs, session_getter, self.refresh)
        self.tabs.addTab(self.ports.widget, "Ports")
        self.dock.widget = self.tabs
        main_window.addDockWidget(pya.Qt.RightDockWidgetArea, self.dock)
        self.timer = pya.QTimer(self.dock)
        self.timer.timeout = self.refresh
        self.timer.start(500)
        self.refresh()

    def refresh(self, *args):
        if self.dock._destroyed():
            return
        self.parameters.refresh()
        self.ports.refresh()
        session = self.session_getter()
        context = selection_context(session)
        if session is not None and hasattr(self.main_window, "current_view"):
            try:
                if self.main_window.current_view() != session.cellview.view():
                    context = {"key": None, "kind": None, "summary": "Activate the ShapeForge working layout.", "actions": {}}
            except Exception:
                pass  # Closed sessions are already represented by their context.
        self.context = context
        if context == self.signature:
            return
        self.signature = context
        if context["key"] is not None and context["key"] != self.selection_key:
            self.tabs.currentIndex = 1
        self.selection_key = context["key"]
        self.summary.text = context["summary"]
        for name, button in self.buttons.items():
            present = name in context["actions"]
            reason = context["actions"].get(name)
            button.visible = present
            button.enabled = present and reason is None
            button.toolTip = reason or ACTION_TITLES[name]

    def execute(self, action):
        self.refresh()  # Resolve the current selection, not a stale native object.
        if action not in self.context["actions"] or self.context["actions"][action] is not None:
            return
        try:
            self.callbacks[action]()
        finally:
            self.refresh()

    def show(self):
        self.refresh()
        self.dock.show()
        self.dock.raise_()
