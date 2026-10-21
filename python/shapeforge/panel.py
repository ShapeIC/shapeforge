"""One ShapeForge dock containing the parameter editor and selection actions."""

import pya
from shapeforge.parameter_panel import ParameterEditor
from shapeforge.selection import selection_context
from shapeforge.ports_panel import PortsEditor

ACTION_TITLES = {
    "rename_instance": "Rename Instance…",
    "select_instance_box": "Select Instance / Box…",
    "create_array": "Create Array…", "edit_array": "Edit Array…", "dissolve_array": "Dissolve Array",
    "edit_project_port": "Edit Project Port…", "delete_project_port": "Delete Project Port",
    "make_box_parametric": "Make Box Parametric…",
    "define_box_extents": "Define Box Extents…",
    "define_box_center": "Define Box Center…",
    "define_via_center": "Define Via Center…",
    "box_info": "Box Info",
    "relink_project": "Relink Project…",
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
        self.reference_picking = False
        self._state_token = None
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
        button = pya.QPushButton(ACTION_TITLES["select_instance_box"], content)
        button.clicked = lambda *args: self.execute("select_instance_box")
        self.buttons["select_instance_box"] = button
        layout.addWidget(button)
        from shapeforge.selection_inspector import SelectionInspector
        self.inspector = SelectionInspector(self, content, layout)
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
        if self.dock._destroyed() or self.reference_picking:
            return
        from shapeforge.view_state import token
        try:
            session = self.session_getter()
            active = self.main_window.current_view() if hasattr(self.main_window, "current_view") else None
            state = (token(session), active)
            if state == self._state_token:
                return
            self._state_token = state
        except Exception:
            self._state_token = None
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
        self.inspector.refresh(session, context)
        if context == self.signature:
            return
        self.signature = context
        if context["key"] is not None and context["key"] != self.selection_key:
            self.tabs.currentIndex = 1
        self.selection_key = context["key"]
        self.summary.text = "\n".join(context["summary"].splitlines()[:3])
        if self.inspector.current is not None:
            self.summary.text += "\n" + self.inspector.current["name"]
        for name, button in self.buttons.items():
            present = name in context["actions"]
            reason = context["actions"].get(name)
            button.visible = present
            button.enabled = present and reason is None
            button.toolTip = reason or ACTION_TITLES[name]

    def execute(self, action):
        if self.reference_picking:
            return
        self.refresh()  # Resolve the current selection, not a stale native object.
        if action not in self.context["actions"] or self.context["actions"][action] is not None:
            return
        try:
            self.callbacks[action]()
        finally:
            self.refresh()

    def set_reference_picking(self, picking):
        if picking and not self.reference_picking:
            self._picking_tab = self.tabs.currentIndex
        self.reference_picking = picking
        self.tabs.enabled = not picking
        if not picking:
            self.refresh()
            if hasattr(self, "_picking_tab"):
                self.tabs.currentIndex = self._picking_tab
                del self._picking_tab

    def show(self):
        self.refresh()
        self.dock.show()
        self.dock.raise_()
