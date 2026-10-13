import json
import math
from dataclasses import MISSING
from typing import get_type_hints
import pya
from shapeforge.session import Session
from shapeforge.components import available_primitives, cellkit_catalog, geometry_fields
from shapeforge.project import prepare_project, save_project as write_project

_actions = []
_session = None
_parameter_panel = None


def show_parameters():
    global _parameter_panel
    if _parameter_panel is None or _parameter_panel.dock._destroyed():
        from shapeforge.parameter_panel import ParameterPanel
        _parameter_panel = ParameterPanel(pya.Application.instance().main_window(), lambda: _session)
    _parameter_panel.show()


def save_project():
    def execute(session):
        session.validate()
        path = pya.FileDialog.ask_save_file_name(
            "ShapeForge — Save Project", session.project_path or f"{session.cell.name}.shapeforge.json",
            "ShapeForge projects (*.shapeforge.json)")
        if not path:
            return
        if not path.endswith(".shapeforge.json"):
            path += ".shapeforge.json"
        write_project(session, path)
        pya.MessageBox.info("ShapeForge", f"Project saved:\n{path}", pya.MessageBox.Ok)
    run_session_action(execute)


def open_project():
    global _session
    path = pya.FileDialog.ask_open_file_name(
        "ShapeForge — Open Project", "", "ShapeForge projects (*.shapeforge.json)")
    if not path:
        return
    mw = pya.Application.instance().main_window()
    previous_view = mw.current_view_index
    cellview = None
    try:
        prepared = prepare_project(path)
        cellview = mw.create_layout(1)
        if not cellview.layout().is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        session = prepared.attach(cellview)
        view = cellview.view()
        view.add_missing_layers()
        view.max_hier()
        view.zoom_fit()
        if _session is not None:
            _session.hide_ports()
    except Exception as error:
        if cellview is not None:
            mw.close_current_view()
        if previous_view >= 0:
            mw.current_view_index = previous_view
        pya.MessageBox.warning("ShapeForge", str(error), pya.MessageBox.Ok)
        return
    _session = session
    if prepared.warnings:
        pya.MessageBox.warning(
            "ShapeForge — Project compatibility",
            "Project opened with the installed dependencies:\n" + "\n".join(prepared.warnings),
            pya.MessageBox.Ok)

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
            f"{name} = {value} µm"
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

        value = pya.InputDialog.ask_string(
            "ShapeForge — Parameter",
            f"Definition of {name} (µm), number or expression (+, -, *):",
            str(parameters.get(name, 10.0)),
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
def ask_primitive_geometry(primitive, previous, limits):
    from shapeic_cellkit import PrimitiveGeometry
    types = get_type_hints(PrimitiveGeometry)
    values = {}
    for field in geometry_fields():
        kind = types[field.name]
        if kind not in (int, float):
            raise ValueError(f"Unsupported CellKit geometry field type: {field.name} ({kind}).")
        scale = 1e-6 if field.name.endswith("_m") else 1
        label = field.name[:-2].replace("_", " ") + " (µm)" if scale != 1 else field.name
        default = previous.get(field.name, field.default)
        if default is MISSING and field.default_factory is not MISSING:
            default = field.default_factory()
        hint = ""
        if limits is not None:
            if field.name == "nf" and limits.required_nf is not None:
                default = limits.required_nf
                hint = f"; required: {limits.required_nf}"
            elif field.name == "nf" and limits.nf_multiple_of is not None:
                hint = f"; multiple of {limits.nf_multiple_of}"
            elif field.name == "finger_width_m" and limits.max_finger_width_m is not None:
                hint = f"; maximum: {limits.max_finger_width_m / scale:g} µm"
        text = "" if default is MISSING else f"{default / scale:g}"
        while True:
            answer = pya.InputDialog.ask_string(
                f"ShapeForge — {primitive}", f"{label} (positive {'integer' if kind is int else 'number'}{hint}):", text)
            if answer is None:
                return
            try:
                number = int(answer.strip()) if kind is int else float(answer.strip())
                if not math.isfinite(number) or number <= 0:
                    raise ValueError()
                values[field.name] = number if kind is int else number * scale
                break
            except (ValueError, OverflowError):
                pya.MessageBox.warning("ShapeForge", f"Enter a positive {'integer' if kind is int else 'finite number'} for {label}.", pya.MessageBox.Ok)
                text = answer
    return values


def insert_primitive():
    def execute(session):
        session.validate()
        catalog = cellkit_catalog(session.pdk)
        primitives = available_primitives(catalog)
        names = sorted(primitives)
        if not names:
            raise RuntimeError(f"No primitive PCell providers are available for PDK {session.pdk}.")
        primitive = pya.InputDialog.ask_item(
            "ShapeForge — Insert Primitive", f"Primitive ({session.pdk}):", names, 0)
        if primitive is None:
            return
        previous = next((recipe["geometry"] for recipe in reversed(list(session.component_recipes.values()))
                         if recipe.get("provider") == "shapeic-cellkit"
                         and recipe.get("primitive") == primitive and recipe.get("pdk") == session.pdk), {})
        values = ask_primitive_geometry(primitive, previous, primitives[primitive].geometry_limits)
        if values is None:
            return
        session.insert_primitive(primitive, values, pdk=session.pdk)
    run_session_action(execute)


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

def edit_primitive_parameters():
    def execute(session):
        _, recipe = session.selected_primitive_instance()
        descriptor = cellkit_catalog(recipe["pdk"]).primitive(recipe["primitive"])
        values = ask_primitive_geometry(recipe["primitive"], recipe["geometry"], descriptor.geometry_limits)
        if values is not None:
            session.edit_primitive_parameters(values)
    run_session_action(execute)

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



def choose_box_reference(session, objects, box_id, axis, end, existing=None):
    title = "ShapeForge — Box Center" if end == "center" else "ShapeForge — Box Extents"
    references = [item for key, item in sorted(objects.items()) if key != box_id]
    labels = [item["label"] for item in references]
    if not references:
        raise RuntimeError("Insert another managed object to use as a reference.")
    reference_ids = [item["id"] for item in references]
    default = (reference_ids.index(existing["reference_id"])
               if existing and existing.get("reference_id") in reference_ids else 0)
    choice = pya.InputDialog.ask_item(
        title, f"{axis.upper()} {end}: reference object:",
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
               else ({"min": 0, "max": 2}.get(end, 1)))
    anchor = pya.InputDialog.ask_item(
        title, f"{axis.upper()} {end}: edge or port:",
        options, default)
    if anchor is None:
        return None
    text = pya.InputDialog.ask_string(
        title, f"{axis.upper()} {end}: offset (µm), number or parameter:",
        str(existing.get("offset_um", 0) if existing else 0))
    if text is None:
        return None
    ref = {"reference_id": item["id"], "anchor": anchor,
           "offset_um": session.parse_offset(text)}
    if anchor in port_options:
        ref["anchor"] = "port"
        ref["port"] = port_options[anchor]
    return ref


def choose_box_center(session, objects, box_id, axis, existing=None):
    mode = pya.InputDialog.ask_item(
        "ShapeForge — Box Center", f"{axis.upper()} center definition:",
        ["Coordinate", "Reference"], 1 if isinstance(existing, dict) else 0)
    if mode is None:
        return None
    if mode == "Reference":
        return choose_box_reference(session, objects, box_id, axis, "center",
                                    existing if isinstance(existing, dict) else None)
    text = pya.InputDialog.ask_string(
        "ShapeForge — Box Center", f"{axis.upper()} center (µm), number or parameter:",
        str(existing if existing is not None and not isinstance(existing, dict) else 0))
    return None if text is None else session.parse_offset(text)


def define_box_center():
    def execute(session):
        shape, box_id, definition, objects = session.selected_box_definition()
        for axis, dimension in (("x", "width_um"), ("y", "height_um")):
            key = f"{axis}_center"
            mode = pya.InputDialog.ask_item(
                "ShapeForge — Box Center", f"{axis.upper()} center:",
                ["Automatic", "Parametric"], 1 if key in definition else 0)
            if mode is None:
                return
            if mode == "Automatic":
                definition.pop(key, None)
                continue
            if definition.get(f"{axis}_extents") is not None:
                box = shape.box
                size = box.width() if axis == "x" else box.height()
                text = pya.InputDialog.ask_string(
                    "ShapeForge — Box Center",
                    f"{axis.upper()} center replaces this axis's two extents. {dimension} (µm), number or parameter:",
                    str(session.dbu_to_um(size)))
                if text is None:
                    return
                definition[dimension] = session.parse_offset(text)
                definition.pop(f"{axis}_extents")
            center = choose_box_center(session, objects, box_id, axis, definition.get(key))
            if center is None:
                return
            definition[key] = center
        session.define_box_extents(definition)
        pya.MessageBox.info("ShapeForge — Box", "Box center saved and regenerated.", pya.MessageBox.Ok)
    run_session_action(execute)


def define_box_extents():
    def execute(session):
        _, box_id, definition, objects = session.selected_box_definition()
        for axis, dimension in (("x", "width_um"), ("y", "height_um")):
            key = f"{axis}_extents"
            existing = definition.get(key)
            mode = pya.InputDialog.ask_item(
                "ShapeForge — Box Extents", f"{axis.upper()} axis definition:",
                ["Dimension", "Dimension + center", "Two references"],
                2 if existing else (1 if f"{axis}_center" in definition else 0))
            if mode is None:
                return
            if mode in ("Dimension", "Dimension + center"):
                text = pya.InputDialog.ask_string(
                    "ShapeForge — Box Extents", f"{dimension} (µm), number or parameter:",
                    str(definition.get(dimension, 1)))
                if text is None:
                    return
                definition[dimension] = session.parse_offset(text)
                definition.pop(key, None)
                if mode == "Dimension + center":
                    center = choose_box_center(session, objects, box_id, axis,
                                               definition.get(f"{axis}_center"))
                    if center is None:
                        return
                    definition[f"{axis}_center"] = center
                else:
                    definition.pop(f"{axis}_center", None)
            else:
                lower = choose_box_reference(session, objects, box_id, axis, "min", existing.get("min") if existing else None)
                if lower is None:
                    return
                upper = choose_box_reference(session, objects, box_id, axis, "max", existing.get("max") if existing else None)
                if upper is None:
                    return
                definition[key] = {"min": lower, "max": upper}
                definition.pop(f"{axis}_center", None)
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
    action.title = "Parameters Panel"
    action.on_triggered = show_parameters
    menu.insert_item("shapeforge_menu.end", "parameters_panel", action)
    _actions.append(action)

    for name, title, callback in [
        ("save_project", "Save Project...", save_project),
        ("open_project", "Open Project...", open_project),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback
        menu.insert_item("shapeforge_menu.end", name, action)
        _actions.append(action)

    show_parameters()

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
    action.title = "Insert Primitive..."
    action.on_triggered = insert_primitive

    menu.insert_item(
        "shapeforge_menu.end",
        "insert_primitive",
        action,
    )

    _actions.append(action)

    action = pya.Action()
    action.title = "Edit Primitive Parameters..."
    action.on_triggered = edit_primitive_parameters
    menu.insert_item("shapeforge_menu.end", "edit_primitive_parameters", action)
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
        ("define_box_center", "Define Box Center...", define_box_center),
        ("box_info", "Box Info", box_info),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback
        menu.insert_item("shapeforge_menu.end", name, action)
        _actions.append(action)


