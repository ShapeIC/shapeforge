"""Embeddable parameter editor using KLayout's Qt bindings."""

import pya
from shapeforge.parameters import is_measure, derived_parameters, describe_definition


class ParameterEditor:
    def __init__(self, parent, session_getter):
        self.session_getter = session_getter
        self.signature = None
        self.names = []
        self.on_change = None
        self.linked_definition = None
        self.imported = {}
        self.available = False
        self.widget = content = pya.QWidget(parent)
        layout = pya.QVBoxLayout(content)
        self.status = pya.QLabel("Create or open a ShapeForge project.", content)
        self.status.wordWrap = True
        layout.addWidget(self.status)
        self.table = pya.QTableWidget(0, 4, content)
        self.table.setHorizontalHeaderLabels(["Parameter", "Definition", "Value (µm)", "External"])
        self.table.editTriggers = pya.QAbstractItemView.NoEditTriggers
        self.table.selectionBehavior = pya.QAbstractItemView.SelectRows
        self.table.selectionMode = pya.QAbstractItemView.SingleSelection
        self.table.currentCellChanged = self.select_row
        self.table.cellClicked = lambda row, column: self.select_row(row, column, -1, -1)
        layout.addWidget(self.table)
        layout.addWidget(pya.QLabel("Name", content))
        self.name = pya.QLineEdit(content)
        layout.addWidget(self.name)
        layout.addWidget(pya.QLabel("Number, expression or instance.parameter: +, -, *, /, floor(expression), parentheses", content))
        self.definition = pya.QLineEdit(content)
        layout.addWidget(self.definition)
        self.external = pya.QCheckBox("External (ask when inserting this project)", content)
        self.external.checked = False
        layout.addWidget(self.external)
        self.buttons = []
        row = pya.QHBoxLayout()
        for title, callback in (("New", self.new), ("Add / Update", self.save), ("Delete", self.delete)):
            button = pya.QPushButton(title, content)
            button.clicked = callback
            row.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(row)
        link = pya.QPushButton("Link to Dimension…", content)
        link.clicked = self.link_dimension
        layout.addWidget(link)
        self.buttons.append(link)
        regenerate = pya.QPushButton("Regenerate Placement", content)
        regenerate.clicked = self.regenerate
        layout.addWidget(regenerate)
        self.buttons.append(regenerate)
        self.refresh()

    def refresh(self, *args):
        if self.widget._destroyed():
            return
        session = self.session_getter()
        definitions, values, public, imported = {}, {}, [], {}
        try:
            if session is None:
                raise ValueError("Create or open a ShapeForge project.")
            session.validate()
            definitions = session.parameters()
            from shapeforge.instance_parameters import rows
            imported = rows(session)
            values = session.resolved_parameters()
            public = session.public_parameters()
            status = f"{session.cell.name} — parameters in µm"
            available = session.layout.is_editable()
            if not available:
                status += " — open KLayout in editor mode"
        except Exception as error:
            status, available = str(error), False
            if session is not None:
                try:
                    session.validate()
                    available = session.layout.is_editable()
                except Exception:
                    pass
        self.available = available
        self.imported = imported
        signature = (id(session), status, definitions, values, public, available, imported)
        if signature == self.signature:
            return
        previous_name = self.name.text if self.signature and self.signature[0] == id(session) else ""
        self.signature = signature
        self.status.text = status
        for widget in [self.name, self.definition, self.external, self.table] + self.buttons:
            widget.enabled = available
        self.table.blockSignals(True)
        try:
            self.names = sorted(definitions) + sorted(imported)
            self.table.rowCount = len(self.names)
            for row, name in enumerate(self.names):
                definition = f"Read only · {name.split('.')[0]}" if name in imported else describe_definition(definitions[name])
                value = imported.get(name, values.get(name))
                for column, text in enumerate((name, definition, f"{value:g}" if value is not None else "—", "—" if name in imported else "yes" if name in public else "no")):
                    self.table.setItem(row, column, pya.QTableWidgetItem(text))
            self.table.resizeColumnsToContents()
            if previous_name in self.names:
                self.table.setCurrentCell(self.names.index(previous_name), 0)
            else:
                self.table.clearSelection()
                self.name.text = ""
                self.definition.text = ""
                self.external.checked = False
        finally:
            self.table.blockSignals(False)
        if previous_name in self.names:
            self.select_row(self.names.index(previous_name), 0, -1, -1)
        else:
            self.set_read_only(False)

    def set_read_only(self, imported):
        self.name.readOnly = self.definition.readOnly = imported
        for index in (1, 2, 3):  # Add/Update, Delete, Link to Dimension.
            self.buttons[index].enabled = self.available and not imported
        self.external.enabled = self.available and not imported

    def select_row(self, row, column, previous_row, previous_column):
        if 0 <= row < len(self.names):
            name = self.names[row]
            if name in self.imported:
                self.name.text = name
                self.definition.text = f"Read only · {name.split('.')[0]} · {self.imported[name]:g}"
                self.linked_definition = None
                self.external.checked = False
                self.set_read_only(True)
                return
            try:
                session = self.session_getter()
                session.validate()
                definition = session.parameters()[name]
            except (AttributeError, KeyError, RuntimeError):
                self.refresh()
                return
            self.name.text = name
            self.set_read_only(False)
            self.linked_definition = dict(definition) if is_measure(definition) else None
            self.definition.text = describe_definition(definition)
            derived = name in derived_parameters(session.parameters())
            self.external.enabled = session.layout.is_editable() and not derived
            self.external.toolTip = "Parameters derived from geometry or child instances are internal." if derived else "Ask when inserting this project."
            self.external.checked = name in session.public_parameters()

    def new(self, *args):
        self.set_read_only(False)
        self.table.clearSelection()
        self.name.text = ""
        self.definition.text = ""
        self.linked_definition = None
        self.external.enabled = self.name.enabled
        self.external.checked = False
        self.name.setFocus()

    def action(self, callback):
        try:
            session = self.session_getter()
            if session is None:
                raise ValueError("Create or open a ShapeForge project.")
            session.validate()
            callback(session)
            self.refresh()
            if self.on_change is not None:
                self.on_change()
        except Exception as error:
            pya.MessageBox.warning("ShapeForge — Parameters", str(error), pya.MessageBox.Ok)

    def link_dimension(self, *args):
        from shapeforge import ui
        name = self.name.text
        def completed():
            self.refresh()
            if self.on_change is not None:
                self.on_change()
        ui.link_parameter_dimension(name, completed)

    def save(self, *args):
        value = self.linked_definition if self.linked_definition is not None and self.definition.text == describe_definition(self.linked_definition) else self.definition.text
        self.action(lambda session: session.set_parameter(self.name.text, value, external=self.external.checked))

    def delete(self, *args):
        self.action(lambda session: session.delete_parameter(self.name.text.strip()))

    def regenerate(self, *args):
        self.action(lambda session: session.regenerate_placement())

class ParameterPanel(ParameterEditor):
    """Compatibility wrapper; ShapeForgePanel embeds ParameterEditor directly."""
    def __init__(self, main_window, session_getter):
        self.dock = pya.QDockWidget("ShapeForge Parameters", main_window)
        self.dock.objectName = "shapeforge_parameters"
        super().__init__(self.dock, session_getter)
        self.dock.widget = self.widget
        main_window.addDockWidget(pya.Qt.RightDockWidgetArea, self.dock)
        self.timer = pya.QTimer(self.dock)
        self.timer.timeout = self.refresh
        self.timer.start(500)

    def show(self):
        self.refresh()
        self.dock.show()
        self.dock.raise_()
