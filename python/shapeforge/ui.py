import json
import pya
from shapeforge.session import Session

_actions = []
_session = None

def make_box_parametric():
    def execute(session):
        shape = session.selected_box()
        raw = shape.property("shapeforge.box")

        if raw is None:
            definition = {
                "width_um": session.dbu_to_um(shape.box.width()),
                "height_um": session.dbu_to_um(shape.box.height()),
            }
        else:
            definition = json.loads(raw)

        width_text = pya.InputDialog.ask_string(
            "ShapeForge — Parametric Box",
            "Width (µm): number or parameter name:",
            str(definition["width_um"]),
        )
        if width_text is None:
            return

        width = session.parse_offset(width_text)

        height_text = pya.InputDialog.ask_string(
            "ShapeForge — Parametric Box",
            "Height (µm): number or parameter name:",
            str(definition["height_um"]),
        )
        if height_text is None:
            return

        height = session.parse_offset(height_text)

        actual_width, actual_height = session.make_box_parametric(
            width, height
        )

        pya.MessageBox.info(
            "ShapeForge — Parametric Box",
            f"Width: {actual_width:.6f} µm\n"
            f"Height: {actual_height:.6f} µm",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)


def regenerate_boxes():
    def execute(session):
        count = session.regenerate_boxes()

        pya.MessageBox.info(
            "ShapeForge",
            f"Updated {count} objects (box dimensions and placement).",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)
def route_ports():
    def execute(session):
        candidates = session.placement_candidates()
        labels = [item["label"] for item in candidates]

        choice = pya.InputDialog.ask_item(
            "ShapeForge — Route Ports",
            "Start instance:",
            labels,
            0,
        )
        if choice is None:
            return

        index_a = labels.index(choice)
        a = candidates[index_a]
        b = candidates[1 - index_a]

        names_a = [port.name for port in a["component"].ports]
        names_b = [port.name for port in b["component"].ports]

        port_a = pya.InputDialog.ask_item(
            "ShapeForge — Route Ports",
            "Start port:",
            names_a,
            0,
        )
        if port_a is None:
            return

        port_b = pya.InputDialog.ask_item(
            "ShapeForge — Route Ports",
            "End port:",
            names_b,
            0,
        )
        if port_b is None:
            return

        width = pya.InputDialog.ask_double(
            "ShapeForge — Route Ports",
            "Route width (µm):",
            1.0,
            3,
        )
        if width is None:
            return

        route_id = session.route_ports(
            a, port_a, b, port_b, width
        )

        pya.MessageBox.info(
            "ShapeForge",
            f"Created route:\n{route_id}",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)
def place_by_edges():
    def execute(session):
        candidates = session.placement_candidates(allow_boxes=True)
        labels = [item["label"] for item in candidates]

        choice = pya.InputDialog.ask_item(
            "ShapeForge — Place by Edges",
            "Reference object A:",
            labels,
            0,
        )
        if choice is None:
            return

        index_a = labels.index(choice)
        a = candidates[index_a]
        b = candidates[1 - index_a]

        direction = pya.InputDialog.ask_item(
            "ShapeForge — Place by Edges",
            "Position of B relative to A:",
            ["right", "left", "above", "below"],
            0,
        )
        if direction is None:
            return

        alignments = (
            ["bottom", "center", "top"]
            if direction in ("right", "left")
            else ["left", "center", "right"]
        )

        alignment = pya.InputDialog.ask_item(
            "ShapeForge — Place by Edges",
            "Align bounding boxes:",
            alignments,
            1,
        )
        if alignment is None:
            return

        gap_text = pya.InputDialog.ask_string(
            "ShapeForge — Place by Edges",
            "Edge gap (µm): number or parameter name:",
            "5",
        )
        if gap_text is None:
            return

        gap = session.parse_offset(gap_text)

        axis = "Y" if direction in ("right", "left") else "X"

        offset_text = pya.InputDialog.ask_string(
            "ShapeForge — Place by Edges",
            f"Alignment offset along {axis} (µm):",
            "0",
        )
        if offset_text is None:
            return

        offset = session.parse_offset(offset_text)

        actual_gap, actual_offset = session.place_by_edges(
            a, b, gap, offset, direction, alignment
        )

        pya.MessageBox.info(
            "ShapeForge — Placement",
            f"Direction: {direction}\n"
            f"Alignment: {alignment}\n"
            f"Gap: {actual_gap:.6f} µm\n"
            f"Offset along {axis}: {actual_offset:.6f} µm",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)
def place_right_of():
    def execute(session):
        candidates = session.placement_candidates(allow_boxes=True)
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

        is_edges = relation.get("kind", "ports") in ("right_of", "edges")

        x_label = "Edge gap" if is_edges else "dx"
        y_label = "Perpendicular offset" if is_edges else "dy"

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
            f"Applied placement:\n"
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
            f"Updated {count} objects (box dimensions and placement).",
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
            f"Applied placement:\n"
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

def keep_selected_box_id():
    def execute(session):
        count = session.keep_selected_box_id()
        pya.MessageBox.info(
            "ShapeForge", f"Assigned new IDs to {count} box copies.",
            pya.MessageBox.Ok)
    run_session_action(execute)



def define_box_extents():
    def execute(session):
        _, box_id, definition, objects = session.selected_box_definition()
        references = [item for key, item in sorted(objects.items()) if key != box_id]
        labels = [item["label"] for item in references]

        def choose_reference(axis, end, existing):
            if not references:
                raise RuntimeError("Insert another managed object to use as a reference.")
            reference_ids = [item["id"] for item in references]
            default = (reference_ids.index(existing["reference_id"])
                       if existing and existing.get("reference_id") in reference_ids else 0)
            choice = pya.InputDialog.ask_item(
                "ShapeForge — Box Extents", f"{axis.upper()} {end}: reference object:",
                labels, default)
            if choice is None:
                return None
            item = references[labels.index(choice)]
            anchors = (["left", "center", "right"] if axis == "x"
                       else ["bottom", "center", "top"])
            options = list(anchors)
            port_options = {}
            if item["type"] == "instance" and item["component"] is not None:
                for port in item["component"].ports:
                    label = f"Port: {port.name}"
                    options.append(label)
                    port_options[label] = port.name
            previous = None
            if existing:
                previous = (f"Port: {existing.get('port')}" if existing.get("anchor") == "port"
                            else existing.get("anchor"))
            default = (options.index(previous) if previous in options
                       else (0 if end == "min" else 2))
            anchor = pya.InputDialog.ask_item(
                "ShapeForge — Box Extents", f"{axis.upper()} {end}: edge or port:",
                options, default)
            if anchor is None:
                return None
            text = pya.InputDialog.ask_string(
                "ShapeForge — Box Extents", f"{axis.upper()} {end}: offset (µm), number or parameter:",
                str(existing.get("offset_um", 0) if existing else 0))
            if text is None:
                return None
            ref = {"reference_id": item["id"], "anchor": anchor,
                   "offset_um": session.parse_offset(text)}
            if anchor in port_options:
                ref["anchor"] = "port"
                ref["port"] = port_options[anchor]
            return ref

        for axis, dimension in (("x", "width_um"), ("y", "height_um")):
            key = f"{axis}_extents"
            existing = definition.get(key)
            mode = pya.InputDialog.ask_item(
                "ShapeForge — Box Extents", f"{axis.upper()} axis definition:",
                ["Dimension", "Two references"], 1 if existing else 0)
            if mode is None:
                return
            if mode == "Dimension":
                text = pya.InputDialog.ask_string(
                    "ShapeForge — Box Extents", f"{dimension} (µm), number or parameter:",
                    str(definition.get(dimension, 1)))
                if text is None:
                    return
                definition[dimension] = session.parse_offset(text)
                definition.pop(key, None)
            else:
                lower = choose_reference(axis, "min", existing.get("min") if existing else None)
                if lower is None:
                    return
                upper = choose_reference(axis, "max", existing.get("max") if existing else None)
                if upper is None:
                    return
                definition[key] = {"min": lower, "max": upper}
        session.define_box_extents(definition)
        pya.MessageBox.info("ShapeForge — Box", "Box extents saved and regenerated.", pya.MessageBox.Ok)
    run_session_action(execute)


def box_info():
    def execute(session):
        pya.MessageBox.info("ShapeForge — Box", session.box_info(), pya.MessageBox.Ok)
    run_session_action(execute)

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

    action = pya.Action()
    action.title = "Place by Edges..."
    action.on_triggered = place_by_edges

    menu.insert_item(
        "shapeforge_menu.end",
        "place_by_edges",
        action,
    )

    _actions.append(action)
    
    action = pya.Action()
    action.title = "Route Ports..."
    action.on_triggered = route_ports

    menu.insert_item(
        "shapeforge_menu.end",
        "route_ports",
        action,
    )

    _actions.append(action)

    for name, title, callback in [
        (
            "make_box_parametric",
            "Make Box Parametric...",
            make_box_parametric,
        ),
        (
            "regenerate_boxes",
            "Regenerate Boxes",
            regenerate_boxes,
        ),
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
    action.title = "Keep Selected Box ID"
    action.on_triggered = keep_selected_box_id
    menu.insert_item("shapeforge_menu.end", "keep_selected_box_id", action)
    _actions.append(action)

    for name, title, callback in [
        ("define_box_extents", "Define Box Extents...", define_box_extents),
        ("box_info", "Box Info", box_info),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback
        menu.insert_item("shapeforge_menu.end", name, action)
        _actions.append(action)


