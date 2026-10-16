import json
import math
import inspect
from dataclasses import MISSING
from typing import get_type_hints
import pya
from shapeforge.session import Session
from shapeforge.components import available_primitives, cellkit_catalog, geometry_fields
from shapeforge.project import prepare_project, save_project as write_project
from shapeforge.technology import available_technology_pcells, parameter_specs as technology_specs
from shapeforge.dimension_dialog import dimension_dialog, PORT_REFERENCE

_actions = []
_session = None
_parameter_panel = None
_active_flow = None


def load_external_libraries():
    from shapeforge.external import CONFIG_KEY, configure_libraries
    window = pya.Application.instance().main_window()
    raw = window.get_config(CONFIG_KEY)
    if raw:
        configure_libraries(json.loads(raw))


def external_libraries():
    from shapeforge.external import CONFIG_KEY, libraries, register_library
    def execute(session):
        name = pya.InputDialog.ask_string("ShapeForge — External Libraries", "Library name (Python identifier):", "external")
        if name is None:
            return
        root = pya.FileDialog.ask_existing_dir("ShapeForge — Trusted Python Library", "")
        if not root:
            return
        register_library(name.strip(), root, session.pdk)
        pya.Application.instance().main_window().set_config(CONFIG_KEY, json.dumps(libraries()))
    run_session_action(execute)


def ask_external_arguments(recipe, previous, session):
    from shapeforge.external import generator_specs
    specs = generator_specs(recipe)  # Validate every annotation before prompting.
    return ask_component_arguments(recipe["function"], specs, previous, session)


def ask_dimension(session, title, label, previous, target_id=None, magnification=1):
    value = dimension_dialog(session, title, label, previous, magnification)
    if value is not PORT_REFERENCE:
        return value
    objects = session.placement_objects()
    candidates = [item for identity, item in objects.items() if identity != target_id
                  and item["type"] == "instance" and item["component"] is not None and len(item["component"].ports)]
    if not candidates:
        raise ValueError("No other linked instance with ports is available.")
    identity = yield from pick_reference(session, candidates, "Select the owner of the dimension's reference port.",
                                         [target_id] if target_id is not None else [])
    if identity is None:
        return
    item = session.placement_objects()[identity]
    ports = session.transformed_ports(item["instance"], item["component"])
    labels = [f"{port['name']} — width {port['width']:g} µm" for port in ports]
    choice = pya.InputDialog.ask_item(title, "Reference port (width in layout):", labels, 0)
    if choice is None:
        return
    value = {"kind": "port_width", "reference_id": identity, "port": ports[labels.index(choice)]["name"]}
    session.resolve_dimension(value, magnification=magnification)
    return value


def ask_via_arguments(session, specs, previous, target_id=None, magnification=1):
    values = {}
    for name, spec in specs.items():
        if name in ("width", "height"):
            value = yield from ask_dimension(session, "ShapeForge — via_stack", name,
                                             previous.get(name, spec["default"]), target_id, magnification)
            if value is None:
                return
            if isinstance(value, str):
                value = {"expression": value}
            values[name] = value
        else:
            answer = ask_component_arguments("via_stack", {name: spec}, previous, session)
            if answer is None:
                return
            values.update(answer)
    from shapeforge.technology import resolve_inputs
    resolve_inputs("via_stack", session.resolve_via_inputs(values, magnification), session.parameters(), session.pdk)
    return values


def ask_component_arguments(generator, specs, previous, session):
    import inspect
    from shapeforge.external import resolve_arguments
    types = {name: spec["type"] for name, spec in specs.items()}
    values = {}
    for name, spec in specs.items():
        kind = spec["type"]
        default = previous.get(name, spec["default"])
        if "choices" in spec:
            choices = spec["choices"]
            labels = [spec.get("choice_labels", {}).get(choice, choice) for choice in choices]
            answer = pya.InputDialog.ask_item("ShapeForge — " + generator, name + ":", labels,
                                            choices.index(default) if default in choices else 0)
            if answer is None:
                return
            values[name] = choices[labels.index(answer)]
            continue
        if kind == "bool":
            answer = pya.InputDialog.ask_item("ShapeForge — " + generator, name + " (bool):",
                                            ["false", "true"], 1 if default is True else 0)
            if answer is None:
                return
            values[name] = answer == "true"
            continue
        text = "" if default is inspect.Parameter.empty else (
            default["expression"] if isinstance(default, dict) else str(default))
        hint = "literal text" if kind == "str" else f"{spec.get('unit', 'native units')}; number or project expression"
        while True:
            answer = pya.InputDialog.ask_string("ShapeForge — " + generator, f"{name} ({kind}; {hint}):", text)
            if answer is None:
                return
            try:
                if kind == "str":
                    value = answer
                else:
                    try:
                        value = int(answer.strip()) if kind == "int" else float(answer.strip())
                    except ValueError:
                        value = {"expression": answer.strip()}
                resolve_arguments({name: value}, {name: kind}, session.parameters())
                values[name] = value
                break
            except (ValueError, OverflowError) as error:
                pya.MessageBox.warning("ShapeForge", str(error), pya.MessageBox.Ok)
                text = answer
    resolve_arguments(values, types, session.parameters())
    return values


def insert_external_pcell():
    from shapeforge.external import available_generators, libraries
    def execute(session):
        names = sorted(name for name, info in libraries().items() if info["pdk"] == session.pdk)
        if not names:
            raise ValueError("No external libraries registered for this PDK. Use External Libraries… first.")
        library = pya.InputDialog.ask_item("ShapeForge — External PCell", "Library:", names, 0)
        if library is None:
            return
        generators = available_generators(library)
        if not generators:
            raise ValueError("This library has no public Python generator functions.")
        file = pya.InputDialog.ask_item("ShapeForge — External PCell", "Python file:", sorted(generators), 0)
        if file is None:
            return
        function = pya.InputDialog.ask_item("ShapeForge — External PCell", "Function:", generators[file], 0)
        if function is None:
            return
        reference = {"library": library, "file": file, "function": function, "pdk": session.pdk}
        values = ask_external_arguments(reference, {}, session)
        if values is not None:
            session.insert_external_pcell(reference, values)
    run_session_action(execute)


def show_parameters():
    global _parameter_panel
    if _parameter_panel is None or _parameter_panel.dock._destroyed():
        from shapeforge.panel import ShapeForgePanel
        _parameter_panel = ShapeForgePanel(pya.Application.instance().main_window(), lambda: _session)
    _parameter_panel.show()


def ask_project_port_position(session, object_id, previous, title="ShapeForge — Project Port Position"):
    objects = session.placement_objects()
    position = {"version": 1}
    for axis in ("x", "y"):
        key = f"{axis}_center"
        existing = (previous or {}).get(key)
        modes = ["Free (move in layout)", "Coordinate", "Object anchor"]
        default = 0 if existing is None else (2 if isinstance(existing, dict) else 1)
        mode = pya.InputDialog.ask_item(title, f"{axis.upper()} center:", modes, default)
        if mode is None:
            return
        if mode == modes[0]:
            continue
        if mode == "Coordinate":
            text = pya.InputDialog.ask_string(title, f"{axis.upper()} (µm), number or expression:",
                                             str(existing) if existing is not None and not isinstance(existing, dict) else "0")
            if text is None:
                return
            position[key] = session.parse_offset(text)
        else:
            if not any(identity != object_id for identity in objects):
                raise ValueError("No other registered reference object is available.")
            reference = yield from choose_box_reference(session, objects, object_id, axis, "center",
                                                       existing if isinstance(existing, dict) else None, title=title)
            if reference is None:
                return
            position[key] = reference
    return position


def project_port_dialog(session, object_id=None):
    return run_session_action(lambda current: _project_port_dialog(current, object_id), session=session)


def _project_port_dialog(session, object_id=None):
    from shapeforge.project_ports import rows, create, edit
    specs = technology_specs("project_port", session.pdk)
    previous, position, angle = {}, {"version": 1}, 0
    if object_id is not None:
        matches = [row for row in rows(session) if row["id"] == object_id]
        if len(matches) != 1 or matches[0]["duplicate_id"]:
            raise ValueError("Resolve the copied port ID with Keep Selected Instance ID first.")
        previous, position, angle = matches[0]["values"], matches[0]["position"], matches[0]["orientation"]
    else:
        names = {row["name"] for row in rows(session)}
        index = 1
        while f"port{index}" in names:
            index += 1
        previous["pinName"] = f"port{index}"
    values = ask_component_arguments("Project Port", specs, previous, session)
    if values is None:
        return
    choices = ["0", "90", "180", "270"]
    keep = f"Keep current ({angle:g}°)"
    if str(int(angle)) not in choices or angle != int(angle):
        choices.append(keep)
    choice = pya.InputDialog.ask_item("ShapeForge — Project Port", "Orientation (degrees):", choices,
                                    choices.index(str(int(angle))) if str(int(angle)) in choices and angle == int(angle) else len(choices) - 1)
    if choice is None:
        return
    orientation = None if choice == keep else int(choice)
    position = yield from ask_project_port_position(session, object_id, position)
    if position is None:
        return
    if object_id is None:
        return create(session, values, position, orientation or 0)
    return edit(session, object_id, values, position, orientation)


def add_project_port():
    run_session_action(lambda session: _project_port_dialog(session))


def selected_project_port_id(session):
    from shapeforge.project_ports import locate
    instance, _ = session.selected_primitive_instance()
    identity = instance.property("shapeforge.instance_id")
    locate(session, identity)
    return identity


def edit_project_port():
    run_session_action(lambda session: _project_port_dialog(session, selected_project_port_id(session)))


def delete_project_port():
    from shapeforge.project_ports import remove
    run_session_action(lambda session: remove(session, selected_project_port_id(session)))


def define_via_center():
    def execute(session):
        from shapeforge.technology import is_via_stack
        instance, recipe = session.selected_primitive_instance()
        if not is_via_stack(recipe):
            raise ValueError("Select a registered via_stack instance.")
        identity = instance.property("shapeforge.instance_id")
        raw = instance.property("shapeforge.via_center")
        position = yield from ask_project_port_position(session, identity, json.loads(raw) if raw else None,
                                                        title="ShapeForge — Via Center")
        if position is None:
            return
        current, _ = session.selected_primitive_instance()
        if current.property("shapeforge.instance_id") != identity:
            raise ValueError("The selected via stack changed. Retry the action.")
        session.define_via_center(position)
        pya.MessageBox.info("ShapeForge — Via Center", "Via center saved and regenerated.", pya.MessageBox.Ok)
    run_session_action(execute)


def array_dialog(session, editing=False):
    from shapeforge.arrays import ARRAY, selected_owner, configure, validate_definition
    identity, source = selected_owner(session)
    raw = source.property(ARRAY)
    if editing and raw is None:
        raise ValueError("Select an array base or one of its repetitions.")
    if not editing and raw is not None:
        raise ValueError("This instance already belongs to an array. Use Edit Array.")
    definition = json.loads(raw) if raw else {"version": 1, "columns": 2, "rows": 1, "spacing": "gap", "dx_um": 5, "dy_um": 5, "members": {}}
    for key, label in (("columns", "Columns (includes the base):"), ("rows", "Rows (includes the base):")):
        text = pya.InputDialog.ask_string("ShapeForge — Array", label + " number or expression", str(definition[key]))
        if text is None:
            return
        definition[key] = session.parse_offset(text)
    choices = ["Edge gap", "Origin pitch"]
    choice = pya.InputDialog.ask_item("ShapeForge — Array", "Spacing in layout X/Y:", choices, 0 if definition["spacing"] == "gap" else 1)
    if choice is None:
        return
    definition["spacing"] = "gap" if choice == choices[0] else "pitch"
    for key, label in (("dx_um", "X spacing (µm):"), ("dy_um", "Y spacing (µm):")):
        text = pya.InputDialog.ask_string("ShapeForge — Array", label + " number or expression", str(definition[key]))
        if text is None:
            return
        definition[key] = session.parse_offset(text)
    validate_definition(definition, session.parameters())
    current, _ = selected_owner(session)
    if current != identity:
        raise ValueError("The selected array base changed. Retry the action.")
    configure(session, definition)


def create_array():
    run_session_action(lambda session: array_dialog(session))


def edit_array():
    run_session_action(lambda session: array_dialog(session, editing=True))


def dissolve_array():
    from shapeforge.arrays import dissolve
    run_session_action(dissolve)


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

        identity = shape.property("shapeforge.object_id")
        width = yield from ask_dimension(session, "ShapeForge — Parametric Box", "Width", definition["width_um"], identity)
        if width is None:
            return
        height = yield from ask_dimension(session, "ShapeForge — Parametric Box", "Height", definition["height_um"], identity)
        if height is None:
            return
        if session.selected_box() != shape:
            raise ValueError("The selected box changed. Retry the action.")

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
        identity = yield from pick_reference(session, candidates, "Select the route's start instance.",
                                             [item["id"] for item in candidates])
        if identity is None:
            return
        objects = session.placement_objects()
        a = objects[identity]
        b = objects[next(item["id"] for item in candidates if item["id"] != identity)]

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

        a, b = current_placement_pair(session, a, b, False)
        route_id = session.route_ports(
            a, port_a, b, port_b, width
        )

        pya.MessageBox.info(
            "ShapeForge",
            f"Created route:\n{route_id}",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)
def choose_placement_pair(session, allow_boxes, target_only=False):
    if target_only:
        session.validate()
        selection = list(session.cellview.view().object_selection)
        if len(selection) != 1 or selection[0].cv_index != session.cellview.index():
            raise RuntimeError("Select exactly one placement target in the working cell.")
        objects = session.placement_objects()
        target = session.selected_placement_object(selection[0], objects)
        candidates = [item for key, item in objects.items() if key != target["id"]]
        if not allow_boxes:
            if target["type"] != "instance" or target["component"] is None or not len(target["component"].ports):
                raise RuntimeError("Port placement requires a linked target instance with ports.")
            candidates = [item for item in candidates if item["type"] == "instance"
                          and item["component"] is not None and len(item["component"].ports)]
        if not candidates:
            raise RuntimeError("No other registered reference object is available.")
    else:
        candidates = session.placement_candidates(allow_boxes=allow_boxes)
    required = [target["id"]] if target_only else [item["id"] for item in candidates]
    identity = yield from pick_reference(session, candidates, "Select reference A (stays fixed).", required)
    if identity is None:
        return
    objects = session.placement_objects()
    target_id = target["id"] if target_only else next(item["id"] for item in candidates if item["id"] != identity)
    return objects[identity], objects[target_id]


def current_placement_pair(session, reference, target, target_only):
    objects = session.placement_objects()
    if target_only:
        selection = list(session.cellview.view().object_selection)
        if len(selection) != 1 or selection[0].cv_index != session.cellview.index():
            raise RuntimeError("The placement target selection changed. Retry the action.")
        current = session.selected_placement_object(selection[0], objects)
        if current["id"] != target["id"]:
            raise RuntimeError("The placement target selection changed. Retry the action.")
    if reference["id"] not in objects or target["id"] not in objects:
        raise RuntimeError("A placement object was removed. Retry the action.")
    return objects[reference["id"]], objects[target["id"]]


def panel_place_by_edges():
    place_by_edges(target_only=True)


def panel_place_by_ports():
    place_by_ports(target_only=True)


def place_by_edges(target_only=False):
    def execute(session):
        pair = yield from choose_placement_pair(session, allow_boxes=True, target_only=target_only)
        if pair is None:
            return
        a, b = pair

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

        a, b = current_placement_pair(session, a, b, target_only)
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
        identity = yield from pick_reference(session, candidates, "Select reference A: B will be placed to its right.",
                                             [item["id"] for item in candidates])
        if identity is None:
            return
        objects = session.placement_objects()
        a = objects[identity]
        b = objects[next(item["id"] for item in candidates if item["id"] != identity)]

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

        a, b = current_placement_pair(session, a, b, False)
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
def link_parameter_dimension(name, completed=None):
    def execute(session):
        name_value = name.strip()
        if not name_value:
            raise ValueError("Enter a parameter name before linking a dimension.")
        labels = ["Width", "Height", "Port Width"]
        choice = pya.InputDialog.ask_item("ShapeForge — Link Parameter", "Dimension (layout axes, µm):", labels, 0)
        if choice is None:
            return
        quantity = {"Width": "width", "Height": "height", "Port Width": "port_width"}[choice]
        objects = session.placement_objects(raw=True)
        candidates = [item for item in objects.values() if quantity != "port_width" or
                      item["type"] == "instance" and item["component"] is not None and len(item["component"].ports)]
        candidates = [item for item in candidates if item["type"] != "instance" or
                      not item["instance"].is_regular_array() and item["instance"].size() == 1]
        if not candidates:
            raise ValueError("No registered objects are available for this measurement.")
        identity = yield from pick_reference(session, candidates, "Select the object to measure.")
        if identity is None:
            return
        item = session.placement_objects(raw=True).get(identity)
        if item is None:
            raise ValueError("The measurement reference was removed. Retry the action.")
        definition = {"version": 1, "kind": "geometry_measure", "reference_id": identity, "quantity": quantity}
        if quantity == "port_width":
            names = [port.name for port in item["component"].ports]
            port = pya.InputDialog.ask_item("ShapeForge — Link Parameter", "Port:", names, 0)
            if port is None:
                return
            definition["port"] = port
        # Never silently remove a public flag when replacing an existing parameter.
        session.set_parameter(name_value, definition)
        if completed is not None:
            completed()
    return run_session_action(execute)


def pick_reference(session, candidates, prompt, required_ids=()):
    from shapeforge.reference_picker import ReferenceRequest
    identity = yield ReferenceRequest(tuple(item["id"] for item in candidates), prompt, tuple(required_ids))
    return identity


class _DialogFlow:
    """Advance ordinary dialogs until a graphical selection is requested."""

    def __init__(self, session, dialogs):
        self.session, self.dialogs = session, dialogs
        self.picker = None
        self.result = None
        self.previous_actions = []

    def advance(self, identity=None):
        global _active_flow
        try:
            request = self.dialogs.send(identity)
        except StopIteration as completed:
            self.result = completed.value
            self.finish()
            return
        except Exception as error:
            self.finish()
            pya.MessageBox.warning("ShapeForge", str(error), pya.MessageBox.Ok)
            return
        try:
            from shapeforge.reference_picker import ReferencePicker
            _active_flow = self
            window = pya.Application.instance().main_window()
            if not self.previous_actions:
                self.previous_actions = [(action, action.enabled) for action in _actions]
                for action, _ in self.previous_actions:
                    action.enabled = False
            if _parameter_panel is not None and not _parameter_panel.dock._destroyed():
                _parameter_panel.set_reference_picking(True)
            self.picker = ReferencePicker(self.session, request, self.selected, lambda: _session, window)
            self.picker.start()
        except Exception as error:
            self.finish()
            pya.MessageBox.warning("ShapeForge", str(error), pya.MessageBox.Ok)

    def selected(self, identity):
        self.picker = None
        if identity is None or _session is not self.session:
            self.finish()
            return
        self.advance(identity)

    def finish(self):
        global _active_flow
        if self.picker is not None:
            self.picker.finish(None, notify=False)
            self.picker = None
        self.dialogs.close()
        if _active_flow is self:
            _active_flow = None
        for action, enabled in self.previous_actions:
            if not action._destroyed():
                action.enabled = enabled
        if _parameter_panel is not None and not _parameter_panel.dock._destroyed():
            _parameter_panel.set_reference_picking(False)


def run_session_action(callback, session=None):
    if _active_flow is not None:
        return
    session = _session if session is None else session
    if session is None:
        pya.MessageBox.warning(
            "ShapeForge",
            "Create a PCell first.",
            pya.MessageBox.Ok,
        )
        return

    try:
        result = callback(session)
        if inspect.isgenerator(result):
            flow = _DialogFlow(session, result)
            flow.advance()
            return flow.result
        return result
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
            f"Definition of {name} (µm), number or expression (+, -, *, /, floor):",
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
def place_by_ports(target_only=False):
    def execute(session):
        pair = yield from choose_placement_pair(session, allow_boxes=False, target_only=target_only)
        if pair is None:
            return
        a, b = pair

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

        dx = session.parse_offset(dx_text)

        dy_text = pya.InputDialog.ask_string(
            "ShapeForge — Place by Ports",
            "dy (µm): enter a number or parameter name:",
            "0",
        )
        if dy_text is None:
            return

        dy = session.parse_offset(dy_text)

        a, b = current_placement_pair(session, a, b, target_only)
        actual_dx, actual_dy = session.place_by_ports(
            a, port_a, b, port_b, dx, dy
        )

        pya.MessageBox.info(
            "ShapeForge — Placement",
            f"Applied placement:\n"
            f"dx = {actual_dx:.6f} µm\n"
            f"dy = {actual_dy:.6f} µm",
            pya.MessageBox.Ok,
        )

    run_session_action(execute)

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
def ask_primitive_geometry(primitive, previous, limits, session=None):
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
                if field.name not in previous:
                    default = limits.required_nf
                hint = f"; required: {limits.required_nf}"
            elif field.name == "nf" and limits.nf_multiple_of is not None:
                hint = f"; multiple of {limits.nf_multiple_of}"
            elif field.name == "finger_width_m" and limits.max_finger_width_m is not None:
                hint = f"; maximum: {limits.max_finger_width_m / scale:g} µm"
        text = "" if default is MISSING else (default if isinstance(default, str) else f"{default / scale:g}")
        while True:
            answer = pya.InputDialog.ask_string(
                f"ShapeForge — {primitive}", f"{label} (number or project expression{hint}):", text)
            if answer is None:
                return
            try:
                number = int(answer.strip()) if kind is int else float(answer.strip())
                if not math.isfinite(number) or number <= 0:
                    raise ValueError()
                values[field.name] = number if kind is int else number * scale
                break
            except (ValueError, OverflowError):
                try:
                    if session is None:
                        raise ValueError("Enter a number.")
                    number = session.resolve_offset(answer.strip())
                    if number <= 0 or (kind is int and not number.is_integer()):
                        raise ValueError(f"{label} must evaluate to a positive {'integer' if kind is int else 'number'}.")
                    values[field.name] = answer.strip()
                    break
                except ValueError as error:
                    pya.MessageBox.warning("ShapeForge", str(error), pya.MessageBox.Ok)
                    text = answer
    return values


def insert_primitive():
    def execute(session):
        session.validate()
        catalog = cellkit_catalog(session.pdk)
        primitives = available_primitives(catalog)
        technology = available_technology_pcells(catalog)
        names = sorted(primitives) + sorted(technology)
        if not names:
            raise RuntimeError(f"No primitive PCell providers are available for PDK {session.pdk}.")
        primitive = pya.InputDialog.ask_item(
            "ShapeForge — Insert Primitive", f"Primitive ({session.pdk}):", names, 0)
        if primitive is None:
            return
        if primitive in technology:
            generator = technology[primitive]
            previous = next((recipe["arguments"] for recipe in reversed(list(session.component_recipes.values()))
                             if recipe.get("provider") == "shapeic-cellkit-technology"
                             and recipe.get("generator") == generator and recipe.get("pdk") == session.pdk), {})
            if generator == "via_stack":
                values = yield from ask_via_arguments(session, technology_specs(generator, session.pdk), previous)
            else:
                values = ask_component_arguments(generator, technology_specs(generator, session.pdk), previous, session)
            if values is not None:
                session.insert_technology_pcell(generator, values)
            return
        previous = next((recipe["geometry"] for recipe in reversed(list(session.component_recipes.values()))
                         if recipe.get("provider") == "shapeic-cellkit"
                         and recipe.get("primitive") == primitive and recipe.get("pdk") == session.pdk), {})
        values = ask_primitive_geometry(primitive, previous, primitives[primitive].geometry_limits, session)
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

def ask_project_arguments(source, previous, session, editing=False):
    from shapeforge.project_links import interface, project_scope
    with project_scope(session.project_path):
        fields = interface(source)
    names = {name for name, definition, value in fields}
    values = dict(previous)
    title = "ShapeForge — Project Parameters"
    if editing and not fields and not values:
        pya.MessageBox.info(title,
            "The linked project has no external parameters in its saved file:\n"
            f"{source}\n\n"
            "Open the child project separately. In Parameters, select a parameter, "
            "check External and click Add / Update. Then use Save Project and retry "
            "Edit Primitive Parameters on this instance.\n\n"
            "This editor reads the saved child file; unsaved changes in another view "
            "are not used.", pya.MessageBox.Ok)
        return None
    for name in sorted(set(values) - names):
        answer = pya.InputDialog.ask_item(title,
            f"{name} is no longer external. Remove its override?",
            ["Cancel", "Remove override"], 0)
        if answer != "Remove override":
            return None
        del values[name]
    for name, definition, default in fields:
        modes = ["Use child default", "Override (number or parent expression)"]
        answer = pya.InputDialog.ask_item(title,
            f"{name}: child default {definition} (currently {default:g} µm)",
            modes, 1 if name in values else 0)
        if answer is None:
            return None
        if answer == modes[0]:
            values.pop(name, None)
            continue
        value = pya.InputDialog.ask_string(title, f"{name} (µm or parent expression):", str(values.get(name, default)))
        if value is None:
            return None
        values[name] = session.parse_offset(value)
    return values


def insert_project():
    def execute(session):
        path = pya.FileDialog.ask_open_file_name("ShapeForge — Insert Project", "", "ShapeForge projects (*.shapeforge.json);;JSON (*.json)")
        if not path:
            return
        values = ask_project_arguments(path, {}, session)
        if values is not None:
            session.insert_project(path, values)
    run_session_action(execute)


def relink_project():
    def execute(session):
        instance, recipe = session.selected_primitive_instance()
        if recipe["provider"] != "shapeforge-project":
            raise ValueError("Select a linked ShapeForge project instance.")
        path = pya.FileDialog.ask_open_file_name("ShapeForge — Relink Project", recipe["source"], "ShapeForge projects (*.shapeforge.json);;JSON (*.json)")
        if not path:
            return
        raw = instance.property("shapeforge.geometry_parameters")
        previous = json.loads(raw)["values"] if raw else recipe["overrides"]
        values = ask_project_arguments(path, previous, session)
        if values is not None:
            session.edit_primitive_parameters(values, source=path)
    run_session_action(execute)


def edit_primitive_parameters():
    def execute(session):
        instance, recipe = session.selected_primitive_instance()
        raw = instance.property("shapeforge.geometry_parameters")
        previous = recipe.get("geometry", recipe.get("arguments")) if raw is None else json.loads(raw)["values"]
        if recipe["provider"] == "shapeforge-project":
            values = ask_project_arguments(recipe["source"], previous, session, editing=True)
        elif recipe["provider"] == "external-python":
            values = ask_external_arguments(recipe, previous, session)
        elif recipe["provider"] == "shapeic-cellkit-technology":
            if recipe["generator"] == "via_stack":
                values = yield from ask_via_arguments(session, technology_specs("via_stack", recipe["pdk"]), previous,
                                                     instance.property("shapeforge.instance_id"), instance.dcplx_trans.mag)
            else:
                values = ask_component_arguments(recipe["generator"], technology_specs(recipe["generator"], recipe["pdk"]), previous, session)
        else:
            descriptor = cellkit_catalog(recipe["pdk"]).primitive(recipe["primitive"])
            values = ask_primitive_geometry(recipe["primitive"], previous, descriptor.geometry_limits, session)
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



def choose_box_reference(session, objects, box_id, axis, end, existing=None, title=None):
    title = title or ("ShapeForge — Box Center" if end == "center" else "ShapeForge — Box Extents")
    references = [item for key, item in sorted(objects.items()) if key != box_id]
    if not references:
        raise RuntimeError("Insert another managed object to use as a reference.")
    identity = yield from pick_reference(session, references, f"{axis.upper()} {end}: select the reference object.",
                                         [box_id] if box_id is not None else [])
    if identity is None:
        return None
    item = session.placement_objects()[identity]
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
        return (yield from choose_box_reference(session, objects, box_id, axis, "center",
                                    existing if isinstance(existing, dict) else None))
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
                value = yield from ask_dimension(
                    session, "ShapeForge — Box Center",
                    f"{axis.upper()} center replaces this axis's two extents. {dimension}",
                    session.dbu_to_um(size), box_id)
                if value is None:
                    return
                definition[dimension] = value
                definition.pop(f"{axis}_extents")
            center = yield from choose_box_center(session, objects, box_id, axis, definition.get(key))
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
                value = yield from ask_dimension(session, "ShapeForge — Box Extents", dimension,
                                                  definition.get(dimension, 1), box_id)
                if value is None:
                    return
                definition[dimension] = value
                definition.pop(key, None)
                if mode == "Dimension + center":
                    center = yield from choose_box_center(session, objects, box_id, axis,
                                               definition.get(f"{axis}_center"))
                    if center is None:
                        return
                    definition[f"{axis}_center"] = center
                else:
                    definition.pop(f"{axis}_center", None)
            else:
                lower = yield from choose_box_reference(session, objects, box_id, axis, "min", existing.get("min") if existing else None)
                if lower is None:
                    return
                upper = yield from choose_box_reference(session, objects, box_id, axis, "max", existing.get("max") if existing else None)
                if upper is None:
                    return
                definition[key] = {"min": lower, "max": upper}
                from shapeforge.dimensions import is_port_width
                if is_port_width(definition.get(dimension)):
                    definition.pop(dimension)
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
    from shapeforge.reference_clicks import ensure_factory
    ensure_factory()
    try:
        load_external_libraries()
    except Exception as error:
        pya.MessageBox.warning("ShapeForge — External Libraries", str(error), pya.MessageBox.Ok)
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
    action.title = "ShapeForge Panel"
    action.on_triggered = show_parameters
    menu.insert_item("shapeforge_menu.end", "parameters_panel", action)
    _actions.append(action)

    for name, title, callback in [
        ("save_project", "Save Project...", save_project),
        ("open_project", "Open Project...", open_project),
        ("insert_project", "Insert ShapeForge Project...", insert_project),
        ("relink_project", "Relink Project...", relink_project),
        ("add_project_port", "Add Project Port...", add_project_port),
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

    for name, title, callback in [
        ("external_libraries", "External Libraries...", external_libraries),
        ("insert_external_pcell", "Insert External PCell...", insert_external_pcell),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback
        menu.insert_item("shapeforge_menu.end", name, action)
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
        ("define_via_center", "Define Via Center...", define_via_center),
        ("create_array", "Create Array...", create_array),
        ("edit_array", "Edit Array...", edit_array),
        ("dissolve_array", "Dissolve Array", dissolve_array),
        ("box_info", "Box Info", box_info),
    ]:
        action = pya.Action()
        action.title = title
        action.on_triggered = callback
        menu.insert_item("shapeforge_menu.end", name, action)
        _actions.append(action)


