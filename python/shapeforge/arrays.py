"""Parametric repetitions of individually addressable instances and boxes."""

import json
from uuid import uuid4

import pya

from shapeforge.parameters import resolve_value

ARRAY = "shapeforge.array"
MEMBER = "shapeforge.array_member"


def validate_definition(definition, parameters):
    if (not isinstance(definition, dict) or set(definition) != {"version", "columns", "rows", "spacing", "dx_um", "dy_um", "members"}
            or type(definition["version"]) is not int or definition["version"] != 1
            or definition["spacing"] not in ("gap", "pitch") or not isinstance(definition["members"], dict)):
        raise ValueError("Invalid ShapeForge array definition.")
    counts = []
    for key in ("columns", "rows"):
        value = resolve_value(definition[key], parameters)
        if value < 1 or not value.is_integer():
            raise ValueError(f"Array {key} must evaluate to a positive integer.")
        counts.append(int(value))
    for index, key in enumerate(("dx_um", "dy_um")):
        value = resolve_value(definition[key], parameters)
        if value < 0 or (definition["spacing"] == "pitch" and counts[index] > 1 and value == 0):
            raise ValueError("Array gaps must be nonnegative and pitches must be positive.")
    identities = set()
    for coordinate, identity in definition["members"].items():
        parts = coordinate.split(",")
        if (len(parts) != 2 or any(not part.isdigit() or str(int(part)) != part for part in parts)
                or coordinate == "0,0" or not isinstance(identity, str) or not identity or identity in identities):
            raise ValueError("Invalid or duplicated array member identity.")
        identities.add(identity)
    return tuple(counts)


def validate_member(member):
    if (not isinstance(member, dict) or set(member) != {"version", "owner_id", "row", "column"}
            or type(member["version"]) is not int or member["version"] != 1
            or not isinstance(member["owner_id"], str) or not member["owner_id"]
            or any(type(member[key]) is not int or member[key] < 0 for key in ("row", "column"))
            or member["row"] == member["column"] == 0):
        raise ValueError("Invalid array member definition.")


def collect(objects, parameters, validate_values=True):
    owners, memberships, reserved = {}, {}, set(objects)
    for identity, item in objects.items():
        definition, member = item.get("array"), item.get("array_member")
        if definition is not None:
            checked = definition if validate_values or not isinstance(definition, dict) else {
                **definition, "columns": 1, "rows": 1, "dx_um": 0, "dy_um": 0}
            validate_definition(checked, parameters)
            if member is not None or (item["type"] == "instance" and item["component"] is None):
                raise ValueError("An array base must be a parametric box or linked instance outside other arrays.")
            from shapeforge.project_ports import is_project_port
            if is_project_port(item.get("recipe")):
                raise ValueError("Project interface ports cannot be repeated as an array.")
            owners[identity] = definition
        if member is not None:
            validate_member(member)
            memberships[identity] = member
    assigned = set()
    for owner, definition in owners.items():
        for coordinate, identity in definition["members"].items():
            if identity == owner or identity in assigned:
                raise ValueError("Array member IDs overlap another array or its base.")
            assigned.add(identity)
            if identity in objects:
                expected = memberships.get(identity)
                if expected is None or expected["owner_id"] != owner or coordinate != f"{expected['row']},{expected['column']}":
                    raise ValueError("Array member identity belongs to another object. Resolve copied IDs explicitly.")
            reserved.add(identity)
    for identity, member in memberships.items():
        owner = member["owner_id"]
        coordinate = f"{member['row']},{member['column']}"
        if owner not in owners or owners[owner]["members"].get(coordinate) != identity:
            raise ValueError(f"Array member {identity} has no valid base or coordinate.")
        item = objects[identity]
        if item["type"] != objects[owner]["type"]:
            raise ValueError("Array repetitions must have the same object type as their base.")
        if item["type"] == "box":
            definition = item.get("definition")
            if (not isinstance(definition, dict) or set(definition) != {"version", "width_um", "height_um"}
                    or definition.get("version") != 1
                    or any(type(definition.get(key)) not in (int, float) for key in ("width_um", "height_um"))):
                raise ValueError("Box repetitions inherit size and position. Edit the base or dissolve the array.")
        if any(item["object"].property(key) is not None for key in ("shapeforge.placement", "shapeforge.via_center", "shapeforge.port_position", "shapeforge.geometry_parameters")):
            raise ValueError("Array members inherit geometry and position; edit the base or dissolve the array.")
    return owners, memberships, reserved


def validate_project(objects, components, parameters):
    class Properties:
        def __init__(self, entry):
            self.entry = entry
        def property(self, name):
            key = name.removeprefix("shapeforge.")
            value = self.entry.get(key)
            return None if value is None else json.dumps(value)
    items = {identity: {"id": identity, "type": kind, "array": entry.get("array"),
                        "array_member": entry.get("array_member"), "component": True if kind == "instance" else None,
                        "recipe": components.get(entry.get("component_id")), "definition": entry.get("definition"), "object": Properties(entry)}
             for identity, (kind, entry) in objects.items()}
    return collect(items, parameters)


def referenced_ids(value):
    if isinstance(value, dict):
        if isinstance(value.get("reference_id"), str):
            yield value["reference_id"]
        for child in value.values():
            yield from referenced_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from referenced_ids(child)


class VirtualInstance:
    """Read-only stand-in while planning new repetitions, before layout edits."""
    def __init__(self, source, member):
        self.dcplx_trans = source["instance"].dcplx_trans
        self.properties = {MEMBER: json.dumps(member)}

    def property(self, key):
        return self.properties.get(key)


class VirtualBox:
    def __init__(self, source, member):
        self.box = source["shape"].box
        self.properties = {MEMBER: json.dumps(member)}

    def property(self, key):
        return self.properties.get(key)


def prepare(session, objects, replacement=None):
    if replacement is not None:
        identity, definition = replacement
        if identity not in objects:
            raise ValueError("The selected array base no longer exists.")
        objects[identity]["array"] = definition
    owners, memberships, reserved = collect(objects, session.parameters())
    removals = set()
    for owner, definition in owners.items():
        columns, rows = validate_definition(definition, session.parameters())
        wanted = {f"{row},{column}" for row in range(rows) for column in range(columns) if row or column}
        definition = {**definition, "members": dict(definition["members"])}
        source = objects[owner]
        source["array"] = definition
        for identity, member in memberships.items():
            if member["owner_id"] == owner and f"{member['row']},{member['column']}" not in wanted:
                removals.add(identity)
        for coordinate in sorted(wanted):
            identity = definition["members"].get(coordinate)
            if identity is None:
                identity = str(uuid4())
                while identity in reserved:
                    identity = str(uuid4())
                reserved.add(identity)
                definition["members"][coordinate] = identity
            row, column = map(int, coordinate.split(","))
            member = {"version": 1, "owner_id": owner, "row": row, "column": column}
            if identity not in objects:
                virtual = VirtualInstance(source, member) if source["type"] == "instance" else VirtualBox(source, member)
                objects[identity] = {"id": identity, "type": source["type"], "object": virtual,
                                     "instance" if source["type"] == "instance" else "shape": virtual,
                                     "component": source["component"], "name": source["name"],
                                     "label": f"Array member ({row}, {column}) | {identity}", "array_member": member}
            item = objects[identity]
            item["component"] = source["component"]
            item["local_box"] = source["local_box"] if "local_box" in source else source["component"].kdb_cell.dbbox()
            if source["type"] == "box":
                item["layer"] = source["layer"]
    for identity, item in objects.items():
        if identity in removals:
            continue
        for key in ("shapeforge.placement", "shapeforge.box", "shapeforge.geometry_parameters", "shapeforge.via_center", "shapeforge.port_position"):
            raw = item["object"].property(key)
            if raw and removals.intersection(referenced_ids(json.loads(raw))):
                raise ValueError("Cannot reduce the array: a removed repetition is referenced by another object.")
    for identity in removals:
        del objects[identity]
    return removals


def member_transform(session, item, source, transform):
    definition, member = source["array"], item["array_member"]
    dx, dy = (session.resolve_offset(definition[key]) for key in ("dx_um", "dy_um"))
    if definition["spacing"] == "gap":
        bounds = session.placement_bbox(source, transform)
        dx, dy = dx + bounds.width(), dy + bounds.height()
    x = session.dbu_to_um(session.snap_um_to_dbu(transform.disp.x + member["column"] * dx))
    y = session.dbu_to_um(session.snap_um_to_dbu(transform.disp.y + member["row"] * dy))
    return pya.DCplxTrans(transform.mag, transform.angle, transform.is_mirror(), x, y)


def locate(session, identity):
    matches = [i for i in session.cell.each_inst() if i.property("shapeforge.instance_id") == identity]
    for layer in session.layout.layer_indexes():
        matches.extend(shape for shape in session.cell.shapes(layer).each() if shape.property("shapeforge.object_id") == identity)
    if len(matches) != 1:
        raise ValueError(f"Missing or duplicate array object ID: {identity}.")
    return matches[0]


def locate_box(session, identity):
    matches = [(shape, layer) for layer in session.layout.layer_indexes()
               for shape in session.cell.shapes(layer).each() if shape.property("shapeforge.object_id") == identity]
    if len(matches) != 1 or not matches[0][0].is_box():
        raise ValueError(f"Missing or duplicate array box ID: {identity}.")
    return matches[0]


def selected_owner(session):
    session.validate()
    selection = list(session.cellview.view().object_selection)
    if len(selection) != 1 or selection[0].cv_index != session.cellview.index():
        raise ValueError("Select one direct parametric box or linked instance.")
    item = session.selected_placement_object(selection[0], session.placement_objects())
    if item["type"] == "instance" and item["component"] is None:
        raise ValueError("Select a parametric box or linked component instance to repeat.")
    member = item.get("array_member")
    identity = member["owner_id"] if member else item["id"]
    return identity, locate(session, identity)


def configure(session, definition):
    if not session.layout.is_editable():
        raise ValueError("Open KLayout in editor mode.")
    identity, source = selected_owner(session)
    previous = source.property(ARRAY)
    definition = {**definition, "version": 1, "members": json.loads(previous)["members"] if previous else {}}
    validate_definition(definition, session.parameters())
    components = session.primitive_update_plan()
    updates, _ = session.placement_plan(component_replacements=components, array_replacement=(identity, definition))
    view = session.cellview.view()
    session.hide_ports()
    view.cancel()
    view.transaction("ShapeForge: configure array")
    try:
        session.apply_regeneration_plan(components, updates)
    finally:
        view.commit()
    view.add_missing_layers()


def protected_ids(instance):
    result = set()
    raw = instance.property(ARRAY)
    if raw:
        result.add(instance.property("shapeforge.instance_id") or instance.property("shapeforge.object_id"))
        result.update(json.loads(raw)["members"].values())
    raw = instance.property(MEMBER)
    if raw:
        result.add(instance.property("shapeforge.instance_id") or instance.property("shapeforge.object_id"))
        result.add(json.loads(raw)["owner_id"])
    return result


def dissolve(session):
    if not session.layout.is_editable():
        raise ValueError("Open KLayout in editor mode.")
    identity, source = selected_owner(session)
    if source.property(ARRAY) is None:
        raise ValueError("The selection does not belong to an array.")
    objects = session.placement_objects()
    members = [key for key, item in objects.items() if (item.get("array_member") or {}).get("owner_id") == identity]
    geometry = source.property("shapeforge.geometry_parameters")
    box_definition = source.property("shapeforge.box")
    view = session.cellview.view()
    view.cancel()
    view.transaction("ShapeForge: dissolve array")
    try:
        source.delete_property(ARRAY)
        for key in members:
            member = locate(session, key)
            member.delete_property(MEMBER)
            if geometry is not None:
                member.set_property("shapeforge.geometry_parameters", geometry)
            if box_definition is not None:
                base = json.loads(box_definition)
                independent = {"version": 1}
                for axis, field in (("x", "width_um"), ("y", "height_um")):
                    size = member.box.width() if axis == "x" else member.box.height()
                    independent[field] = session.dbu_to_um(size) if base.get(f"{axis}_extents") else base[field]
                from shapeforge.dimensions import box_version
                independent["version"] = box_version(independent)
                member.set_property("shapeforge.box", json.dumps(independent, allow_nan=False))
    finally:
        view.commit()


def apply_metadata(session, updates):
    for item, _ in updates:
        if item["type"] == "array_remove":
            locate(session, item["id"]).delete()
    for item, _ in updates:
        if item.get("array") is not None:
            locate(session, item["id"]).set_property(ARRAY, json.dumps(item["array"], allow_nan=False))
    for item, transform in updates:
        member = item.get("array_member")
        if member is None:
            continue
        if item["type"] == "box":
            _, layer = locate_box(session, member["owner_id"])
            matches = [(shape, index) for index in session.layout.layer_indexes() for shape in session.cell.shapes(index).each()
                       if shape.property("shapeforge.object_id") == item["id"]]
            if matches and matches[0][1] != layer:
                old, _ = matches[0]
                properties, box = old.prop_id, old.box
                old.delete()
                shape = session.cell.shapes(layer).insert(box)
                shape.prop_id = properties
            elif matches:
                shape = matches[0][0]
            else:
                shape = session.cell.shapes(layer).insert(pya.Box(0, 0, session.um_to_dbu(item["local_box"].width()),
                                                                  session.um_to_dbu(item["local_box"].height())))
            shape.set_property("shapeforge.object_id", item["id"])
            shape.set_property("shapeforge.box", json.dumps(item["definition"], allow_nan=False))
            shape.set_property(MEMBER, json.dumps(member, allow_nan=False))
            continue
        source = locate(session, member["owner_id"])
        matches = [i for i in session.cell.each_inst() if i.property("shapeforge.instance_id") == item["id"]]
        instance = matches[0] if matches else session.cell.insert(pya.DCellInstArray(source.cell_index, transform))
        instance.cell_index = source.cell_index
        instance.set_property("shapeforge.instance_id", item["id"])
        instance.set_property("shapeforge.component_id", session.component_id_for_instance(source))
        instance.set_property(MEMBER, json.dumps(member, allow_nan=False))
