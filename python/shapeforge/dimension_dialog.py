"""Dimension entry with an explicit switch to graphical port selection."""

import pya

from shapeforge.dimensions import is_port_width

PORT_REFERENCE = object()


def dimension_dialog(session, title, label, previous, magnification=1):
    window = pya.Application.instance().main_window()
    dialog = pya.QDialog(window)
    dialog.windowTitle = title
    layout = pya.QVBoxLayout(dialog)
    layout.addWidget(pya.QLabel(label + " (µm): number or expression", dialog))
    summary = pya.QLabel(dialog)
    summary.wordWrap = True
    summary.textFormat = pya.Qt.PlainText
    layout.addWidget(summary)
    field = pya.QLineEdit(dialog)
    layout.addWidget(field)
    linked = is_port_width(previous)
    if linked:
        field.text = str(session.resolve_dimension(previous, magnification=magnification))
        owner = next(i for i in session.cell.each_inst() if i.property("shapeforge.instance_id") == previous["reference_id"])
        name = session.layout.cell(owner.cell_index).name
        summary.text = f"Port width: {name} / {previous['port']} — {session.resolve_dimension(previous):g} µm in layout"
    else:
        field.text = str(previous)
    field.enabled = not linked
    result = [None]
    mode = ["reference" if linked else "value"]

    def reference(*args):
        result[0] = PORT_REFERENCE
        dialog.accept()

    def use_value(*args):
        mode[0] = "value"
        field.enabled = True
        summary.text = "Enter a number or expression to replace the port binding."
        field.setFocus()

    def accept(*args):
        try:
            value = previous if mode[0] == "reference" else session.parse_offset(field.text)
            if session.resolve_dimension(value, magnification=magnification) <= 0:
                raise ValueError("Dimension must be positive.")
            result[0] = value
            dialog.accept()
        except Exception as error:
            summary.text = str(error)

    for text, callback in (("Reference to Port…", reference), ("Use Value / Expression", use_value),
                           ("OK", accept), ("Cancel", lambda *a: dialog.reject())):
        button = pya.QPushButton(text, dialog)
        button.autoDefault = False
        button.default = text == "OK"
        button.clicked = callback
        layout.addWidget(button)
    try:
        return result[0] if dialog.exec_() else None
    finally:
        dialog.deleteLater()
