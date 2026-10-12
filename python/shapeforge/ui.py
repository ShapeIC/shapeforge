import pya
from shapeforge.session import Session

_actions = []
_session = None

def place_right_of():
    def execute(session):
        candidates = session.placement_candidates()
        labels = [item["label"] for item in candidates]

        choice = pya.InputDialog.ask_item(
            "ShapeForge — Place Right Of",
            "Reference A: B will be placed to its right.",
            labels,
            0,
        )
        if choice is None:
            return

        index_a = labels.index(choice)
        a = candidates[index_a]
        b = candidates[1 - index_a]

        gap_text = pya.InputDialog.ask_string(
            "ShapeForge — Place Right Of",
            "Edge gap (µm): number or parameter name:",
            "5",
        )
        if gap_text is None:
            return

        gap = session.parse_offset(gap_text)

        dy_text = pya.InputDialog.ask_string(
            "ShapeForge — Place Right Of",
            "Center Y offset (µm); 0 aligns centers:",
            "0",
        )
        if dy_text is None:
            return

        dy = session.parse_offset(dy_text)

        actual_gap, actual_dy = session.place_right_of(a, b, gap, dy)

        pya.MessageBox.info(
            "ShapeForge — Placement",
            f"Edge gap = {actual_gap:.6f} µm\n"
            f"Center Y offset = {actual_dy:.6f} µm",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)

def placement_info():
    def execute(session):
        pya.MessageBox.info(
            "ShapeForge — Placement",
            session.placement_info(),
            pya.MessageBox.Ok,
        )

    run_session_action(execute)


def edit_placement():
    def execute(session):
        _, relation = session.selected_placement_target()

        is_right_of = relation.get("kind", "ports") == "right_of"

        x_label = "Edge gap" if is_right_of else "dx"
        y_label = "Center Y offset" if is_right_of else "dy"

        dx_text = pya.InputDialog.ask_string(
            "ShapeForge — Edit Placement",
            f"{x_label} (µm): number or parameter name:",
            str(relation["dx_um"]),
        )
        if dx_text is None:
            return

        dx = session.parse_offset(dx_text)

        dy_text = pya.InputDialog.ask_string(
            "ShapeForge — Edit Placement",
            f"{y_label} (µm): number or parameter name:",
            str(relation["dy_um"]),
        )
        if dy_text is None:
            return

        dy = session.parse_offset(dy_text)

        actual_dx, actual_dy = session.edit_placement(dx, dy)

        pya.MessageBox.info(
            "ShapeForge — Placement",
            f"Applied port offset:\n"
            f"{x_label} = {actual_dx:.6f} µm\n"
            f"{y_label} = {actual_dy:.6f} µm",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)


def remove_placement():
    run_session_action(lambda session: session.remove_placement())
def run_session_action(callback):
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        callback(_session)
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
def set_parameter():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        parameters = _session.parameters()

        existing = "\n".join(
            f"{name} = {value:g} µm"
            for name, value in sorted(parameters.items())
        )

        name = pya.InputDialog.ask_string(
            "ShapeForge — Parameter",
            "Parameter name:\n\n"
            + (existing or "No parameters defined."),
            "spacing",
        )
        if name is None:
            return

        name = name.strip()

        value = pya.InputDialog.ask_double(
            "ShapeForge — Parameter",
            f"Value of {name} (µm):",
            parameters.get(name, 10.0),
            6,
        )
        if value is None:
            return

        _session.set_parameter(name, value)

    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
def keep_selected_instance_id():
    if _session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        count = _session.keep_selected_instance_id()

        pya.MessageBox.info(
            "ShapeForge",
            f"Selected instance kept its ID.\n"
            f"Assigned new IDs to {count} copies.",
            pya.MessageBox.Ok,
        )
    except Exception as error:
        pya.MessageBox.warning(
            "ShapeForge",
            str(error),
            pya.MessageBox.Ok,
        )
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

        dx_text = pya.InputDialog.ask_string(
            "ShapeForge — Place by Ports",
            "dx (µm): enter a number or parameter name:",
            "spacing",
        )
        if dx_text is None:
            return

        dx = _session.parse_offset(dx_text)

        dy_text = pya.InputDialog.ask_string(
            "ShapeForge — Place by Ports",
            "dy (µm): enter a number or parameter name:",
            "0",
        )
        if dy_text is None:
            return

        dy = _session.parse_offset(dy_text)

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
    
    action = pya.Action()
    action.title = "Keep Selected Instance ID"
    action.on_triggered = keep_selected_instance_id

    menu.insert_item(
        "shapeforge_menu.end",
        "keep_selected_instance_id",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Set Parameter..."
    action.on_triggered = set_parameter

    menu.insert_item(
        "shapeforge_menu.end",
        "set_parameter",
        action,
    )

    _actions.append(action)

    for name, title, callback in [
        ("placement_info", "Placement Info", placement_info),
        ("edit_placement", "Edit Placement...", edit_placement),
        ("remove_placement", "Remove Placement", remove_placement),
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
    action.title = "Place Right Of..."
    action.on_triggered = place_right_of

    menu.insert_item(
        "shapeforge_menu.end",
        "place_right_of",
        action,
    )

    _actions.append(action)
