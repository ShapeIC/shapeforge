"""Project interface list; row selection never changes the layout selection."""

import pya
from shapeforge.project_ports import rows, remove, select


class PortsEditor:
    def __init__(self, parent, session_getter, refresh):
        self.session_getter, self.changed = session_getter, refresh
        self.signature, self.entries, self.selected_id = None, [], None
        self.widget = pya.QWidget(parent)
        layout = pya.QVBoxLayout(self.widget)
        self.status = pya.QLabel(self.widget)
        self.status.wordWrap = True
        layout.addWidget(self.status)
        self.table = pya.QTableWidget(0, 5, self.widget)
        self.table.setHorizontalHeaderLabels(["Port", "X (µm)", "Y (µm)", "Orientation", "Status"])
        self.table.editTriggers = pya.QAbstractItemView.NoEditTriggers
        self.table.selectionBehavior = pya.QAbstractItemView.SelectRows
        self.table.selectionMode = pya.QAbstractItemView.SingleSelection
        self.table.currentCellChanged = self.select_row
        self.table.cellClicked = lambda row, column: self.select_row(row, column, -1, -1)
        layout.addWidget(self.table)
        self.buttons = {}
        for name, label in (("add", "Add Project Port…"), ("edit", "Edit Project Port…"),
                            ("select", "Select in Layout"), ("delete", "Delete Project Port")):
            button = pya.QPushButton(label, self.widget)
            button.clicked = lambda *args, action=name: self.action(action)
            layout.addWidget(button)
            self.buttons[name] = button
        self.editable, self.supported = False, False

    def refresh(self):
        session = self.session_getter()
        entries = []
        try:
            if session is None:
                raise ValueError("Create or open a ShapeForge project.")
            session.validate()
            entries = rows(session)
            self.editable = session.layout.is_editable()
            self.supported = session.pdk == "ihp-sg13g2"
            status = f"{session.cell.name} — project interface ports" if self.supported else "Project ports currently support IHP only."
        except Exception as error:
            status, self.editable, self.supported = str(error), False, False
        signature = (id(session), status, entries, self.editable, self.supported)
        if signature == self.signature:
            return
        if self.signature is None or self.signature[0] != id(session):
            self.selected_id = None
        self.signature, self.entries = signature, entries
        self.status.text = status
        self.table.blockSignals(True)
        try:
            self.table.rowCount = len(entries)
            for index, row in enumerate(entries):
                status = "Copied ID: resolve in Selection" if row["duplicate_id"] else "Duplicate name: rename" if row["duplicate_name"] else "Array unsupported" if row["array"] else ""
                for column, value in enumerate((row["name"], f"{row['center'][0]:g}", f"{row['center'][1]:g}", f"{row['orientation']:g}°", status)):
                    self.table.setItem(index, column, pya.QTableWidgetItem(value))
            self.table.resizeColumnsToContents()
            matches = [index for index, row in enumerate(entries) if row["id"] == self.selected_id]
            if len(matches) == 1:
                self.table.setCurrentCell(matches[0], 0)
            else:
                self.selected_id = None
                self.table.clearSelection()
        finally:
            self.table.blockSignals(False)
        self.enable_buttons()

    def select_row(self, row, column, previous_row, previous_column):
        self.selected_id = self.entries[row]["id"] if 0 <= row < len(self.entries) else None
        self.enable_buttons()

    def enable_buttons(self):
        candidates = [row for row in self.entries if row["id"] == self.selected_id]
        valid = len(candidates) == 1 and not candidates[0]["duplicate_id"] and not candidates[0]["array"]
        self.buttons["add"].enabled = self.editable and self.supported
        self.buttons["select"].enabled = valid
        for name in ("edit", "delete"):
            self.buttons[name].enabled = valid and self.editable and self.supported

    def action(self, action):
        self.refresh()
        if not self.buttons[action].enabled:
            return
        try:
            from shapeforge.ui import project_port_dialog
            session = self.session_getter()
            if action == "add": project_port_dialog(session)
            elif action == "edit": project_port_dialog(session, self.selected_id)
            elif action == "delete": remove(session, self.selected_id)
            elif action == "select": select(session, self.selected_id)
        except Exception as error:
            pya.MessageBox.warning("ShapeForge — Ports", str(error), pya.MessageBox.Ok)
        finally:
            self.changed()
