"""Embeddable parameter editor using KLayout's Qt bindings."""

import pya


class ParameterEditor:
    def __init__(self, parent, session_getter):
        self.session_getter = session_getter
        self.signature = None
        self.names = []
        self.on_change = None
        self.widget = content = pya.QWidget(parent)
        layout = pya.QVBoxLayout(content)
        self.status = pya.QLabel("Create or open a ShapeForge project.", content)
        self.status.wordWrap = True
        layout.addWidget(self.status)
        self.table = pya.QTableWidget(0, 3, content)
        self.table.setHorizontalHeaderLabels(["Parameter", "Definition", "Value (µm)"])
        self.table.editTriggers = pya.QAbstractItemView.NoEditTriggers
        self.table.selectionBehavior = pya.QAbstractItemView.SelectRows
        self.table.selectionMode = pya.QAbstractItemView.SingleSelection
        self.table.currentCellChanged = self.select_row
        self.table.cellClicked = lambda row, column: self.select_row(row, column, -1, -1)
        layout.addWidget(self.table)
        layout.addWidget(pya.QLabel("Name", content))
        self.name = pya.QLineEdit(content)
        layout.addWidget(self.name)
        layout.addWidget(pya.QLabel("Number or expression: +, -, *, /, floor(expression), parentheses", content))
        self.definition = pya.QLineEdit(content)
        layout.addWidget(self.definition)
        self.buttons = []
        row = pya.QHBoxLayout()
        for title, callback in (("New", self.new), ("Add / Update", self.save), ("Delete", self.delete)):
            button = pya.QPushButton(title, content)
            button.clicked = callback
            row.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(row)
        regenerate = pya.QPushButton("Regenerate Placement", content)
        regenerate.clicked = self.regenerate
        layout.addWidget(regenerate)
        self.buttons.append(regenerate)
        self.refresh()

    def refresh(self, *args):
        if self.widget._destroyed():
            return
        session = self.session_getter()
        definitions, values = {}, {}
        try:
            if session is None:
                raise ValueError("Create or open a ShapeForge project.")
            session.validate()
            definitions = session.parameters()
            values = session.resolved_parameters()
            status = f"{session.cell.name} — parameters in µm"
            available = session.layout.is_editable()
            if not available:
                status += " — open KLayout in editor mode"
        except Exception as error:
            status, available = str(error), False
        signature = (id(session), status, definitions, values, available)
        if signature == self.signature:
            return
        previous_name = self.name.text if self.signature and self.signature[0] == id(session) else ""
        self.signature = signature
        self.status.text = status
        for widget in [self.name, self.definition, self.table] + self.buttons:
            widget.enabled = available
        self.table.blockSignals(True)
        try:
            self.names = sorted(definitions)
            self.table.rowCount = len(self.names)
            for row, name in enumerate(self.names):
                for column, text in enumerate((name, str(definitions[name]), f"{values.get(name, float('nan')):g}")):
                    self.table.setItem(row, column, pya.QTableWidgetItem(text))
            self.table.resizeColumnsToContents()
            if previous_name in self.names:
                self.table.setCurrentCell(self.names.index(previous_name), 0)
            else:
                self.table.clearSelection()
                self.name.text = ""
                self.definition.text = ""
        finally:
            self.table.blockSignals(False)
        if previous_name in self.names:
            self.select_row(self.names.index(previous_name), 0, -1, -1)

    def select_row(self, row, column, previous_row, previous_column):
        if 0 <= row < len(self.names):
            name = self.names[row]
            try:
                session = self.session_getter()
                session.validate()
                definition = session.parameters()[name]
            except (AttributeError, KeyError, RuntimeError):
                self.refresh()
                return
            self.name.text = name
            self.definition.text = str(definition)

    def new(self, *args):
        self.table.clearSelection()
        self.name.text = ""
        self.definition.text = ""
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

    def save(self, *args):
        self.action(lambda session: session.set_parameter(self.name.text, self.definition.text))

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
