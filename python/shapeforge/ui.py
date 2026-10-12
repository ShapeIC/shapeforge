import pya
from shapeforge.session import Session

_actions = []
_session = None
def regenerate_placement():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        count = _session.regenerate_placement()

        pya.MessageBox.info(
            "ShapeForge",
            f"Regenerated {count} placement relations.",
            pya.MessageBox.Ok,
        )
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
def place_by_ports():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        candidates = _session.placement_candidates()
        labels = [item["label"] for item in candidates]

        reference = pya.InputDialog.ask_item(
            "ShapeForge — Place by Ports",
            "Reference instance A (stays fixed):",
            labels,
            0,
        )
        if reference is None:
            return

        index_a = labels.index(reference)
        a = candidates[index_a]
        b = candidates[1 - index_a]

        names_a = [port.name for port in a["component"].ports]
        names_b = [port.name for port in b["component"].ports]

        port_a = pya.InputDialog.ask_item(
            "ShapeForge — Place by Ports",
            "Reference port on A:",
            names_a,
            0,
        )
        if port_a is None:
            return

        port_b = pya.InputDialog.ask_item(
            "ShapeForge — Place by Ports",
            f"Target port on B ({b['label']}):",
            names_b,
            0,
        )
        if port_b is None:
            return

        dx = pya.InputDialog.ask_double(
            "ShapeForge — Place by Ports",
            "dx: port B minus port A (µm):",
            5.0,
            3,
        )
        if dx is None:
            return

        dy = pya.InputDialog.ask_double(
            "ShapeForge — Place by Ports",
            "dy: port B minus port A (µm):",
            0.0,
            3,
        )
        if dy is None:
            return

        actual_dx, actual_dy = _session.place_by_ports(
            a, port_a, b, port_b, dx, dy
        )

        pya.MessageBox.info(
            "ShapeForge — Placement",
            f"Applied port offset:\n"
            f"dx = {actual_dx:.6f} µm\n"
            f"dy = {actual_dy:.6f} µm",
            pya.MessageBox.Ok,
        )

    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )

def normalize_instance_ids():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        count = _session.normalize_instance_ids()
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
        return

    pya.MessageBox.info(
        "ShapeForge",
        f"Assigned or replaced {count} instance IDs.",
        pya.MessageBox.Ok,
    )
def show_ports():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        _session.show_ports()
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )


def hide_ports():
    if _session is not None:
        _session.hide_ports()

def new_pcell():
    global _session

    name = pya.InputDialog.ask_string(
        "ShapeForge — New PCell",
        "Cell name:",
        "my_pcell",
    )

    # Cancelar no debe crear un layout.
    if name is None:
        return

    name = name.strip()

    if not name:
        pya.MessageBox.warning(
            "ShapeForge",
            "Please enter a cell name.",
            pya.MessageBox.Ok,
        )
        return

    mw = pya.Application.instance().main_window()

    # El modo 1 abre una vista nueva.
    cellview = mw.create_layout(1)
    layout = cellview.layout()

    # DBU expresado en micrómetros: 0.001 µm = 1 nm.
    layout.dbu = 0.001

    cell = layout.create_cell(name)

    # Mostrar la celda recién creada.
    cellview.cell = cell

    if _session is not None:
        _session.hide_ports()

    _session = Session(cellview, cell)

def hello():
    pya.MessageBox.info(
        "ShapeForge",
        "Hello from ShapeForge!",
        pya.MessageBox.Ok
    )

def session_info():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        _session.validate()
    except RuntimeError as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
        return

    pya.MessageBox.info(
        "ShapeForge — Session",
        f"Working cell: {_session.cell.name}\n"
        f"DBU: {_session.layout.dbu} µm\n"
        f"ShapeForge grid: {_session.grid_um} µm",
        pya.MessageBox.Ok,
    )
def insert_test_instance():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        _session.insert_test_instance()
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            f"Could not insert the test instance:\n{error}",
            pya.MessageBox.Ok,
        )

def selection_info():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        message = _session.selection_info()
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            f"Could not inspect the selection:\n{error}",
            pya.MessageBox.Ok,
        )
        return

    pya.MessageBox.info(
        "ShapeForge — Selection",
        message,
        pya.MessageBox.Ok,
    )

def start():
    mw = pya.Application.instance().main_window()
    menu = mw.menu()

    # Create ShapeForge menu
    menu.insert_menu(
        "help_menu",
        "shapeforge_menu",
        "ShapeForge"
    )

    # Create action
    action = pya.Action()
    action.title = "Hello ShapeForge"
    action.on_triggered = hello

    # Add action
    menu.insert_item(
        "shapeforge_menu.end",
        "hello_action",
        action
    )

    action = pya.Action()
    action.title = "New PCell..."
    action.on_triggered = new_pcell

    menu.insert_item(
        "shapeforge_menu.end",
        "new_pcell",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Session Info"
    action.on_triggered = session_info

    menu.insert_item(
        "shapeforge_menu.end",
        "session_info",
        action,
    )

    _actions.append(action) 

    action = pya.Action()
    action.title = "Insert Test Instance"
    action.on_triggered = insert_test_instance

    menu.insert_item(
        "shapeforge_menu.end",
        "insert_test_instance",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Selection Info"
    action.on_triggered = selection_info

    menu.insert_item(
        "shapeforge_menu.end",
        "selection_info",
        action,
    )

    _actions.append(action)

    for name, title, callback in [
        ("show_ports", "Show Ports", show_ports),
        ("hide_ports", "Hide Ports", hide_ports),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback

        menu.insert_item(
            "shapeforge_menu.end",
            name,
            action,
        )

        _actions.append(action)

    action = pya.Action()
    action.title = "Normalize Instance IDs"
    action.on_triggered = normalize_instance_ids

    menu.insert_item(
        "shapeforge_menu.end",
        "normalize_instance_ids",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Place by Ports..."
    action.on_triggered = place_by_ports

    menu.insert_item(
        "shapeforge_menu.end",
        "place_by_ports",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Regenerate Placement"
    action.on_triggered = regenerate_placement

    menu.insert_item(
        "shapeforge_menu.end",
        "regenerate_placement",
        action,
    )

    _actions.append(action)
