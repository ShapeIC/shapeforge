"""Project interface pins backed by ordinary gdsfactory Components and Ports."""

import json
from uuid import uuid4

import pya

from shapeforge.parameters import resolve_value
from shapeforge.technology import generate_technology

POSITION_PROPERTY = "shapeforge.port_position"


def is_project_port(recipe):
    return bool(recipe and recipe.get("provider") == "shapeic-cellkit-technology" and recipe.get("generator") == "project_port")


def validate_position(position, parameters):
    if position is None:
        return
    if (not isinstance(position, dict) or set(position) - {"version", "x_center", "y_center"}
            or type(position.get("version")) is not int or position["version"] != 1):
        raise ValueError("Invalid center position version or fields.")
    for axis, anchors in (("x", ("left", "center", "right", "port")), ("y", ("bottom", "center", "top", "port"))):
        if f"{axis}_center" not in position:
            continue
        value = position[f"{axis}_center"]
        if isinstance(value, dict):
            if (not {"reference_id", "anchor"} <= set(value)
                    or set(value) - {"reference_id", "anchor", "offset_um", "port"}
                    or not isinstance(value["reference_id"], str) or not value["reference_id"]
                    or value["anchor"] not in anchors):
                raise ValueError(f"Invalid {axis.upper()} center reference.")
            if value["anchor"] == "port" and (not isinstance(value.get("port"), str) or not value["port"]):
                raise ValueError("A port reference needs a port name.")
            resolve_value(value.get("offset_um", 0), parameters)
        else:
            resolve_value(value, parameters)


def rows(session):
    session.validate()
    result = []
    for instance in session.cell.each_inst():
        recipe = session.component_recipes.get(session.component_id_for_instance(instance))
        if not is_project_port(recipe):
            continue
        object_id = instance.property("shapeforge.instance_id")
        source = session.components.get(session.component_id_for_instance(instance))
        ports = session.transformed_ports(instance, source) if source is not None else []
        actual = ports[0] if ports else {}
        raw = instance.property(POSITION_PROPERTY)
        result.append({"id": object_id, "name": recipe["arguments"]["pinName"], "arguments": dict(recipe["arguments"]),
                       "values": json.loads(instance.property("shapeforge.geometry_parameters"))["values"]
                       if instance.property("shapeforge.geometry_parameters") else dict(recipe["arguments"]),
                       "position": json.loads(raw) if raw else None,
                       "center": actual.get("center", (instance.dcplx_trans.disp.x, instance.dcplx_trans.disp.y)),
                       "orientation": actual.get("orientation", instance.dcplx_trans.angle),
                       "width": actual.get("width"), "array": instance.size() != 1 or instance.is_regular_array()})
    for row in result:
        row["duplicate_id"] = not row["id"] or sum(other["id"] == row["id"] for other in result) > 1
        row["duplicate_name"] = sum(other["name"] == row["name"] for other in result) > 1
    return result


def validate_names(session):
    for row in rows(session):
        if row["duplicate_name"]:
            raise ValueError(f"Duplicate project port name: {row['name']}. Rename the copied port explicitly.")


def locate(session, object_id):
    session.validate()
    matches = [instance for instance in session.cell.each_inst() if instance.property("shapeforge.instance_id") == object_id]
    if not object_id or len(matches) != 1:
        raise ValueError("Duplicate or missing port ID. Use Keep Selected Instance ID first.")
    instance = matches[0]
    if instance.size() != 1 or instance.is_regular_array():
        raise ValueError("Project port arrays are not supported.")
    recipe = session.component_recipes.get(session.component_id_for_instance(instance))
    if not is_project_port(recipe):
        raise ValueError("The selected object is not a project port.")
    return instance, recipe


def _editable(session):
    session.validate()
    if not session.layout.is_editable():
        raise ValueError("Open KLayout in editor mode.")


def _stage(session):
    from shapeforge.project import _DetachedCellView
    from shapeforge.session import Session
    layout = pya.Layout()
    layout.dbu = session.layout.dbu
    cell = layout.create_cell(session.cell.name)
    cell.copy_tree(session.cell)
    for key, value in session.cell.properties().items():
        cell.set_property(key, value)
    staged = Session(_DetachedCellView(layout), cell, session.grid_um)
    staged.components = dict(session.components)
    staged.component_recipes = dict(session.component_recipes)
    return staged


def _install(session, instance, generated, component_id):
    cell = session.layout.create_cell(f"SF_PORT_{component_id[:8]}")
    cell.copy_tree(generated.component.kdb_cell)
    cell.set_property("shapeforge.component_id", component_id)
    cell.set_property("shapeforge.component_recipe", json.dumps(generated.recipe, allow_nan=False))
    instance.cell_index = cell.cell_index()
    instance.set_property("shapeforge.component_id", component_id)
    session.components[component_id] = generated.component
    session.component_recipes[component_id] = generated.recipe


def _definition(session, instance, values, position):
    session.set_geometry_definition(instance, values, "shapeic-cellkit-technology")
    if position is None:
        instance.delete_property(POSITION_PROPERTY)
    else:
        instance.set_property(POSITION_PROPERTY, json.dumps(position, allow_nan=False))


def _rename_reference(value, object_id, old_name, new_name):
    if isinstance(value, dict):
        if value.get("reference_id") == object_id:
            for key in ("port", "reference_port"):
                if value.get(key) == old_name:
                    value[key] = new_name
        for child in value.values():
            _rename_reference(child, object_id, old_name, new_name)
    elif isinstance(value, list):
        for child in value:
            _rename_reference(child, object_id, old_name, new_name)


def _rewrite_references(session, object_id, old_name, new_name):
    changes = []
    objects = [(i, i.property("shapeforge.instance_id")) for i in session.cell.each_inst()]
    for layer in session.layout.layer_indexes():
        objects += [(s, s.property("shapeforge.object_id")) for s in session.cell.shapes(layer).each()]
    for obj, identity in objects:
        for key in ("shapeforge.placement", "shapeforge.box", POSITION_PROPERTY, "shapeforge.via_center"):
            raw = obj.property(key)
            if raw is None:
                continue
            value = json.loads(raw)
            _rename_reference(value, object_id, old_name, new_name)
            if key == "shapeforge.placement" and identity == object_id and value.get("target_port") == old_name:
                value["target_port"] = new_name
            text = json.dumps(value, allow_nan=False)
            if value != json.loads(raw):
                obj.set_property(key, text)
                changes.append((identity, key, text))
    return changes


def create(session, values, position=None, orientation=0):
    _editable(session)
    return _change(session, None, values, position, orientation)


def edit(session, object_id, values, position=None, orientation=None):
    _editable(session)
    locate(session, object_id)
    return _change(session, object_id, values, position, orientation)


def _change(session, object_id, values, position, orientation):
    validate_position(position, session.parameters())
    if orientation is not None and (type(orientation) not in (int, float) or orientation not in (0, 90, 180, 270)):
        raise ValueError("Choose port orientation 0, 90, 180 or 270 degrees.")
    generated = generate_technology("project_port", values, session.pdk, session.parameters())
    values = {**generated.recipe["arguments"], **values}
    name = generated.recipe["arguments"]["pinName"]
    if any(row["name"] == name and row["id"] != object_id for row in rows(session)):
        raise ValueError(f"Duplicate project port name: {name}.")
    staged = _stage(session)
    new = object_id is None
    object_id = object_id or str(uuid4())
    component_id = str(uuid4())
    reference_changes = []
    if new:
        cell = staged.layout.create_cell("PORT_PENDING")
        instance = staged.cell.insert(pya.CellInstArray(cell.cell_index(), pya.Trans()))
        instance.set_property("shapeforge.instance_id", object_id)
    else:
        instance, old_recipe = locate(staged, object_id)
        from shapeforge.project import _geometry_signature
        source_id = session.component_id_for_instance(locate(session, object_id)[0])
        current = locate(session, object_id)[0]
        if _geometry_signature(session.layout.cell(current.cell_index), session.layout.dbu) != _geometry_signature(session.components[source_id].kdb_cell, session.layout.dbu):
            raise ValueError("This pin was edited manually; its recipe cannot preserve those edits.")
        if old_recipe["arguments"]["pinName"] != name:
            reference_changes = _rewrite_references(staged, object_id, old_recipe["arguments"]["pinName"], name)
    _install(staged, instance, generated, component_id)
    _definition(staged, instance, values, position)
    transform = instance.dcplx_trans
    if orientation is not None:
        transform = pya.DCplxTrans(transform.mag, orientation, transform.is_mirror(), transform.disp.x, transform.disp.y)
        instance.dcplx_trans = transform
    validate_names(staged)
    components, updates = staged.regeneration_plan()
    view = session.cellview.view()
    session.hide_ports()
    view.cancel()
    view.transaction("ShapeForge: add project port" if new else "ShapeForge: edit project port")
    try:
        if new:
            cell = session.layout.create_cell("SF_PORT_PENDING")
            live = session.cell.insert(pya.CellInstArray(cell.cell_index(), pya.Trans()))
            live.set_property("shapeforge.instance_id", object_id)
            session.cell.set_property("shapeforge.pdk", generated.recipe["pdk"])
        else:
            live, _ = locate(session, object_id)
        _install(session, live, generated, component_id)
        _definition(session, live, values, position)
        live.dcplx_trans = transform
        for identity, key, text in reference_changes:
            for candidate in session.cell.each_inst():
                if candidate.property("shapeforge.instance_id") == identity:
                    candidate.set_property(key, text)
            for layer in session.layout.layer_indexes():
                for candidate in session.cell.shapes(layer).each():
                    if candidate.property("shapeforge.object_id") == identity:
                        candidate.set_property(key, text)
        # Translate staged box handles to live shapes; instances are already
        # reacquired by stable ID by the existing planner's application method.
        live_objects = session.placement_objects()
        updates = [({**item, "shape": live_objects[item["id"]]["shape"]} if item["type"] == "box" else item, trans)
                   for item, trans in updates]
        session.apply_regeneration_plan(components, updates)
    finally:
        view.commit()
    view.add_missing_layers()
    return object_id


def usages(session, object_id):
    result = []
    for identity, item in session.placement_objects().items():
        if identity == object_id:
            continue
        raw = item["object"].property("shapeforge.placement")
        if raw and json.loads(raw).get("reference_id") == object_id:
            result.append(f"{identity}: placement")
        definition = item.get("definition") if item["type"] == "box" else (item.get("via_center") or item.get("port_position"))
        if definition and any(ref["reference_id"] == object_id for ref in session.box_position_references(definition)):
            result.append(f"{identity}: extents or center")
    return result


def remove(session, object_id):
    _editable(session)
    instance, _ = locate(session, object_id)
    used = usages(session, object_id)
    if used:
        raise ValueError("Cannot delete a referenced project port:\n" + "\n".join(used))
    view = session.cellview.view()
    session.hide_ports()
    view.cancel()
    view.transaction("ShapeForge: delete project port")
    try:
        instance.delete()
    finally:
        view.commit()


def select(session, object_id):
    instance, _ = locate(session, object_id)
    path = pya.ObjectInstPath()
    path.cv_index = session.cellview.index()
    path.top = session.cell.cell_index()
    path.append_path(pya.InstElement(instance))
    session.cellview.view().object_selection = [path]
