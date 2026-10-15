"""Versioned recipes and staged reconstruction of ShapeForge projects."""

import json
import math
import os
from pathlib import Path
import tempfile

import pya

from shapeforge.components import reconstruct_component, validate_input_definition
from shapeforge.session import Session
from shapeforge.parameters import resolve_parameters, resolve_value

FORMAT = "shapeforge"
VERSION = 12


class ProjectError(ValueError):
    pass


def _object(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProjectError(f"{where}: expected fields {', '.join(sorted(keys))}.")
    return value


def _string(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{where}: expected a non-empty string.")


def _number(value, where, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ProjectError(f"{where}: expected a finite number.")
    if positive and value <= 0:
        raise ProjectError(f"{where}: must be positive.")


def _offset(value, parameters, where, version=2):
    if version == 1 and isinstance(value, str) and not value.isidentifier():
        raise ProjectError(f"{where}: arithmetic expressions require project version 2.")
    try:
        resolve_value(value, parameters)
    except ValueError as error:
        message = str(error)
        raise ProjectError(f"{where}: {message[:1].lower() + message[1:]}") from error


def _version(value, where):
    if type(value) is not int or value != 1:
        raise ProjectError(f"{where}: unsupported version {value!r}; supported version is 1.")


def _recipe(recipe, where):
    if not isinstance(recipe, dict):
        raise ProjectError(f"{where}: expected a component recipe.")
    provider = recipe.get("provider")
    if provider == "shapeforge":
        _object(recipe, ("provider", "generator", "version"), where)
        _version(recipe["version"], where)
        if recipe["generator"] != "test_block":
            raise ProjectError(f"{where}: unknown ShapeForge generator.")
    elif provider == "shapeic-cellkit":
        _object(recipe, ("provider", "pdk", "primitive", "geometry", "options", "versions",
                         "implementation_digest", "technology_digest", "technology_revision",
                         "layout_policy"), where)
        for key in ("pdk", "primitive", "implementation_digest", "technology_digest",
                    "technology_revision", "layout_policy"):
            _string(recipe[key], f"{where}.{key}")
        if not isinstance(recipe["geometry"], dict):
            raise ProjectError(f"{where}: geometry must contain dataclass field values.")
        for key, value in recipe["geometry"].items():
            _string(key, f"{where}.geometry field")
            _number(value, f"{where}.geometry.{key}", positive=True)
        if recipe["options"] != {}:
            raise ProjectError(f"{where}: additional CellKit render options are unsupported.")
        if not isinstance(recipe["versions"], dict):
            raise ProjectError(f"{where}: versions must be an object.")
        for package, value in recipe["versions"].items():
            _string(package, f"{where}.versions package")
            _string(value, f"{where}.versions.{package}")
    elif provider == "shapeic-cellkit-technology":
        _object(recipe, ("provider", "pdk", "generator", "arguments", "parameter_types", "versions",
                         "implementation_digest", "technology_digest", "technology_revision", "layout_policy"), where)
        for key in ("pdk", "generator", "implementation_digest", "technology_digest", "technology_revision", "layout_policy"):
            _string(recipe[key], f"{where}.{key}")
        from shapeforge.technology import parameter_specs, resolve_inputs
        try:
            expected = {name: spec["type"] for name, spec in parameter_specs(recipe["generator"], recipe["pdk"]).items()}
            if recipe["parameter_types"] != expected:
                raise ValueError("Technology parameter types differ from the known contract.")
            if not isinstance(recipe["arguments"], dict) or any(isinstance(v, dict) for v in recipe["arguments"].values()):
                raise ValueError("Technology recipes require effective argument literals.")
            resolve_inputs(recipe["generator"], recipe["arguments"], {}, recipe["pdk"])
        except ValueError as error:
            raise ProjectError(f"{where}: {error}") from error
        if not isinstance(recipe["versions"], dict):
            raise ProjectError(f"{where}: versions must be an object.")
        for package, value in recipe["versions"].items():
            _string(package, "versions package")
            _string(value, "versions value")
    elif provider == "external-python":
        _object(recipe, ("provider", "library", "file", "function", "pdk", "arguments",
                         "parameter_types", "implementation_digest", "versions"), where)
        for key in ("library", "file", "function", "pdk", "implementation_digest"):
            _string(recipe[key], f"{where}.{key}")
        if not recipe["library"].isidentifier() or not recipe["function"].isidentifier() or recipe["function"].startswith("_"):
            raise ProjectError(f"{where}: invalid external library or function identifier.")
        file = Path(recipe["file"])
        if file.is_absolute() or ".." in file.parts or file.suffix != ".py":
            raise ProjectError(f"{where}: external source must be a relative Python file.")
        if not isinstance(recipe["parameter_types"], dict):
            raise ProjectError(f"{where}: parameter_types must be an object.")
        for name, kind in recipe["parameter_types"].items():
            _string(name, "external argument name")
            if not name.isidentifier() or kind not in ("float", "int", "bool", "str"):
                raise ProjectError(f"{where}: invalid external argument type.")
        from shapeforge.external import resolve_arguments
        try:
            if any(isinstance(v, dict) for v in recipe["arguments"].values()):
                raise ValueError("Saved recipes must contain effective literals.")
            resolve_arguments(recipe["arguments"], recipe["parameter_types"], {})
        except (ValueError, AttributeError) as error:
            raise ProjectError(f"{where}: {error}") from error
        if not isinstance(recipe["versions"], dict):
            raise ProjectError(f"{where}: versions must be an object.")
        for package, value in recipe["versions"].items():
            _string(package, "versions package")
            _string(value, "versions value")
    elif provider == "shapeforge-project":
        _object(recipe, ("provider", "source", "pdk", "public_parameters", "overrides", "arguments",
                         "source_digest", "dependency_digest", "versions"), where)
        for key in ("source", "source_digest", "dependency_digest"):
            _string(recipe[key], f"{where}.{key}")
        if recipe["pdk"] is not None:
            _string(recipe["pdk"], f"{where}.pdk")
        from shapeforge.project_links import validate_public, resolve_overrides
        validate_public(recipe["public_parameters"], recipe["arguments"])
        if set(recipe["arguments"]) != set(recipe["public_parameters"]):
            raise ProjectError("Linked project arguments differ from its saved interface.")
        for values in (recipe["overrides"], recipe["arguments"]):
            resolve_overrides(values, {}, recipe["public_parameters"])
            for value in values.values():
                _number(value, "linked project effective value")
        if not isinstance(recipe["versions"], dict) or any(not isinstance(v, str) for v in recipe["versions"].values()):
            raise ProjectError("Invalid linked project dependency versions.")
    else:
        raise ProjectError(f"{where}: unknown provider {provider!r}.")


def validate_document(document):
    """Validate JSON structure and references before invoking any provider."""
    if not isinstance(document, dict):
        raise ProjectError("The project must be a JSON object.")
    if type(document.get("version")) is not int or document["version"] not in range(1, 13):
        raise ProjectError(f"Project format: unsupported version {document.get('version')!r}; supported versions are 1 through 12.")
    _object(document, ("format", "version", "project", "environment", "parameters",
                       "components", "instances", "boxes") +
            (("public_parameters",) if document["version"] >= 11 else ()), "Project")
    if document["format"] != FORMAT:
        raise ProjectError("This file is not a ShapeForge project.")
    info = _object(document["project"], ("name", "description"), "project")
    _string(info["name"], "project.name")
    if not isinstance(info["description"], str):
        raise ProjectError("project.description must be a string.")
    environment = _object(document["environment"], ("pdk", "dbu_um", "grid_um"), "environment")
    if environment["pdk"] is not None:
        _string(environment["pdk"], "environment.pdk")
    for key in ("dbu_um", "grid_um"):
        _number(environment[key], f"environment.{key}", positive=True)
    grid = environment["grid_um"] / environment["dbu_um"]
    if not math.isfinite(grid) or grid < 1 or not math.isclose(grid, round(grid), rel_tol=0, abs_tol=1e-9):
        raise ProjectError("The grid must be an integer multiple of DBU.")
    parameters = document["parameters"]
    if not isinstance(parameters, dict):
        raise ProjectError("parameters must be an object.")
    try:
        if document["version"] == 1 and any(isinstance(value, str) for value in parameters.values()):
            raise ValueError("Parameter formulas require project version 2.")
        from shapeforge.parameters import is_measure, structural_parameters
        if document["version"] < 12 and any(is_measure(v) for v in parameters.values()):
            raise ValueError("Geometry measurement parameters require project version 12.")
        parameters = structural_parameters(parameters)
        resolve_parameters(parameters)
    except ValueError as error:
        raise ProjectError(str(error)) from error
    from shapeforge.project_links import validate_public
    try:
        validate_public(document.get("public_parameters", []), parameters)
    except ValueError as error:
        raise ProjectError(str(error)) from error
    for key in ("components", "instances", "boxes"):
        if not isinstance(document[key], list):
            raise ProjectError(f"{key} must be an array.")
    components = {}
    for entry in document["components"]:
        _object(entry, ("id", "recipe"), "component")
        _string(entry["id"], "component.id")
        if entry["id"] in components:
            raise ProjectError(f"Duplicate component ID: {entry['id']}.")
        components[entry["id"]] = entry["recipe"]
        _recipe(entry["recipe"], f"component {entry['id']}")
        if entry["recipe"]["provider"] == "external-python" and document["version"] < 4:
            raise ProjectError("External Python recipes require project version 4.")
        if entry["recipe"]["provider"] == "shapeic-cellkit-technology" and document["version"] < 5:
            raise ProjectError("CellKit technology recipes require project version 5.")
        if (entry["recipe"]["provider"] == "shapeic-cellkit-technology"
                and entry["recipe"]["generator"] in ("via_stack", "project_port") and document["version"] < 6):
            raise ProjectError("Via stacks and project ports require project version 6.")
        if entry["recipe"]["provider"] == "shapeforge-project" and document["version"] < 11:
            raise ProjectError("Linked projects require project version 11.")
        pdk = entry["recipe"].get("pdk")
        if pdk is not None and pdk != environment["pdk"]:
            raise ProjectError("Component PDK differs from the project PDK.")
    objects = {}
    for kind, rows, keys in (
        ("instance", document["instances"], ("id", "component_id", "transform", "placement") +
         (("geometry_parameters",) if document["version"] >= 3 else ()) +
         (("port_position",) if document["version"] >= 6 else ()) +
         (("via_center",) if document["version"] >= 7 else ()) +
         (("array", "array_member") if document["version"] >= 9 else ())),
        ("box", document["boxes"], ("id", "layer", "box_dbu", "definition", "placement") +
         (("array", "array_member") if document["version"] >= 10 else ())),
    ):
        for entry in rows:
            _object(entry, keys, kind)
            _string(entry["id"], f"{kind}.id")
            if entry["id"] in objects:
                raise ProjectError(f"Duplicate object ID: {entry['id']}. Resolve copied IDs explicitly.")
            objects[entry["id"]] = (kind, entry)
            if kind == "instance":
                _string(entry["component_id"], "instance.component_id")
                if entry["component_id"] not in components:
                    raise ProjectError(f"Missing component: {entry['component_id']}.")
                geometry_parameters = entry.get("geometry_parameters")
                if geometry_parameters is not None:
                    _object(geometry_parameters, ("version", "values"), "geometry_parameters")
                    if geometry_parameters["version"] == 3 and document["version"] < 8:
                        raise ProjectError("Port width input bindings require project version 8.")
                    try:
                        validate_input_definition(geometry_parameters, components[entry["component_id"]], parameters)
                    except Exception as error:
                        raise ProjectError(f"Instance {entry['id']} geometry: {error}") from error
                from shapeforge.project_ports import is_project_port, validate_position
                recipe = components[entry["component_id"]]
                if recipe["provider"] == "shapeforge-project" and entry.get("array_member") is None:
                    if geometry_parameters is None or geometry_parameters["version"] != 4:
                        raise ProjectError("Linked project bases require version 4 override bindings, including empty overrides.")
                position = entry.get("port_position")
                if position is not None:
                    if not is_project_port(recipe):
                        raise ProjectError("Only project ports can have port_position.")
                    try:
                        validate_position(position, parameters)
                    except ValueError as error:
                        raise ProjectError(str(error)) from error
                from shapeforge.technology import is_via_stack
                via_center = entry.get("via_center")
                if via_center is not None:
                    if not is_via_stack(recipe):
                        raise ProjectError("Only via stacks can have via_center.")
                    try:
                        validate_position(via_center, parameters)
                    except ValueError as error:
                        raise ProjectError(str(error)) from error
                if is_project_port(recipe) and geometry_parameters is not None:
                    if geometry_parameters["values"]["pinName"] != recipe["arguments"]["pinName"]:
                        raise ProjectError("Project port name bindings must match the effective recipe; use Edit Project Port to rename.")
                transform = _object(entry["transform"], ("mag", "angle", "mirror", "x_um", "y_um"), "transform")
                for key in ("mag", "angle", "x_um", "y_um"):
                    _number(transform[key], f"transform.{key}", positive=key == "mag")
                if type(transform["mirror"]) is not bool:
                    raise ProjectError("transform.mirror must be a boolean.")
            else:
                layer = _object(entry["layer"], ("layer", "datatype", "name"), "box.layer")
                for key in ("layer", "datatype"):
                    if type(layer[key]) is not int or not 0 <= layer[key] <= 65535:
                        raise ProjectError(f"box.layer.{key} must be an integer in 0..65535.")
                if not isinstance(layer["name"], str):
                    raise ProjectError("box.layer.name must be a string.")
                box = entry["box_dbu"]
                if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                    raise ProjectError("box_dbu must contain four integer DBU coordinates.")
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ProjectError("Box dimensions must be positive.")
    from shapeforge.parameters import is_measure
    for name, value in parameters.items():
        if is_measure(value):
            identity = value["reference_id"]
            if identity not in objects:
                raise ProjectError(f"Parameter {name}: missing reference object {identity}.")
            if value["quantity"] == "port_width" and objects[identity][0] != "instance":
                raise ProjectError(f"Parameter {name}: port width requires a linked instance.")
    from shapeforge.project_ports import is_project_port
    from shapeforge.arrays import validate_project
    try:
        array_owners, array_members, _ = validate_project(objects, components, parameters)
    except ValueError as error:
        raise ProjectError(str(error)) from error
    names = [components[entry["component_id"]]["arguments"]["pinName"] for entry in document["instances"]
             if is_project_port(components[entry["component_id"]])]
    if len(names) != len(set(names)):
        raise ProjectError("Duplicate project port names. Rename copied ports explicitly.")
    from shapeforge.dimensions import validate_port_width, width_references
    def check_width_reference(reference):
        if document["version"] < 8:
            raise ProjectError("Port width dimensions require project version 8.")
        try:
            validate_port_width(reference)
        except ValueError as error:
            raise ProjectError(str(error)) from error
        if reference["reference_id"] not in objects or objects[reference["reference_id"]][0] != "instance":
            raise ProjectError(f"Port width reference requires a linked instance: {reference['reference_id']}.")

    dimension_dependencies = {key: set() for key, (kind, _) in objects.items() if kind == "instance"}
    for identity, member in array_members.items():
        if objects[identity][0] == "instance":
            dimension_dependencies[identity].add(member["owner_id"])
    for identity, (kind, entry) in objects.items():
        if kind == "instance" and entry.get("geometry_parameters"):
            for reference in width_references(entry["geometry_parameters"]["values"]):
                check_width_reference(reference)
                dimension_dependencies[identity].add(reference["reference_id"])
    dimension_visiting, dimension_done = set(), set()
    def visit_dimension(identity):
        if identity in dimension_visiting:
            raise ProjectError(f"Dimension generation cycle detected at {identity}.")
        if identity in dimension_done:
            return
        dimension_visiting.add(identity)
        for source in sorted(dimension_dependencies[identity]):
            visit_dimension(source)
        dimension_visiting.remove(identity)
        dimension_done.add(identity)
    for identity in dimension_dependencies:
        visit_dimension(identity)

    dependencies = {key: set() for key in objects}
    for identity, member in array_members.items():
        dependencies[identity].add(member["owner_id"])
    for object_id, (kind, entry) in objects.items():
        relation = entry["placement"]
        if relation is not None:
            if not isinstance(relation, dict):
                raise ProjectError("placement must be an object or null.")
            relation_kind = relation.get("kind", "ports")
            keys = {"version", "reference_id", "dx_um", "dy_um"}
            # Older edge relations also retain unused null port fields.
            for key in ("reference_port", "target_port"):
                if key in relation:
                    keys.add(key)
            if "kind" in relation:
                keys.add("kind")
            if relation_kind == "ports":
                keys.update(("reference_port", "target_port"))
            elif relation_kind == "edges":
                keys.update(("direction", "alignment"))
            elif relation_kind != "right_of":
                raise ProjectError(f"Unknown placement kind: {relation_kind!r}.")
            _object(relation, keys, f"placement {object_id}")
            _version(relation["version"], "placement")
            _string(relation["reference_id"], "placement.reference_id")
            if relation["reference_id"] not in objects:
                raise ProjectError(f"Missing reference object: {relation['reference_id']}.")
            dependencies[object_id].add(relation["reference_id"])
            for key in ("dx_um", "dy_um"):
                _offset(relation[key], parameters, f"placement.{key}", document["version"])
            if relation_kind == "ports":
                for key in ("reference_port", "target_port"):
                    _string(relation[key], f"placement.{key}")
                if kind != "instance" or objects.get(relation["reference_id"], (None,))[0] != "instance":
                    raise ProjectError("Port placement requires two linked instances.")
            if relation_kind == "edges":
                direction = relation["direction"]
                if direction not in ("right", "left", "above", "below"):
                    raise ProjectError("Invalid placement direction.")
                allowed = ("bottom", "center", "top") if direction in ("right", "left") else ("left", "center", "right")
                if relation["alignment"] not in allowed:
                    raise ProjectError("Invalid placement alignment.")
        position = (entry.get("via_center") or entry.get("port_position")) if kind == "instance" else None
        if position is not None:
            if any(f"{axis}_center" in position for axis in ("x", "y")) and relation is not None:
                raise ProjectError("Instance centers and placement cannot govern the same object.")
            for axis in ("x", "y"):
                value = position.get(f"{axis}_center")
                if isinstance(value, dict):
                    reference = value["reference_id"]
                    if reference not in objects:
                        raise ProjectError(f"Missing reference object: {reference}.")
                    if value["anchor"] == "port" and objects[reference][0] != "instance":
                        raise ProjectError("Port coordinates require linked instances.")
                    dependencies[object_id].add(reference)
        if kind == "box":
            definition = entry["definition"]
            if not isinstance(definition, dict):
                raise ProjectError("Box definition must be an object.")
            if type(definition.get("version")) is not int or definition["version"] not in (1, 2, 3):
                raise ProjectError("Unsupported box definition version; supported versions are 1, 2 and 3.")
            if definition["version"] == 3 and document["version"] < 8:
                raise ProjectError("Box definition version 3 requires project version 8.")
            if set(definition) - {"version", "width_um", "height_um", "x_extents", "y_extents", "x_center", "y_center"}:
                raise ProjectError("Unknown box definition fields.")
            for axis, dimension, anchors in (
                ("x", "width_um", ("left", "center", "right", "port")),
                ("y", "height_um", ("bottom", "center", "top", "port")),
            ):
                extents = definition.get(f"{axis}_extents")
                references = []
                if f"{axis}_center" in definition:
                    if definition["version"] not in (2, 3):
                        raise ProjectError("Parametric box centers require box definition version 2.")
                    if extents is not None:
                        raise ProjectError(f"{axis.upper()} center and two referenced extents cannot coexist.")
                    if relation is not None:
                        raise ProjectError("Parametric box center cannot coexist with its placement relation.")
                    center = definition[f"{axis}_center"]
                    if isinstance(center, dict):
                        references.append(center)
                    else:
                        _offset(center, parameters, f"{axis}_center", document["version"])
                if extents is None:
                    if dimension not in definition:
                        raise ProjectError(f"Missing box {dimension}.")
                    if isinstance(definition[dimension], dict):
                        if definition["version"] != 3:
                            raise ProjectError("Port width dimensions require box definition version 3.")
                        check_width_reference(definition[dimension])
                    else:
                        _offset(definition[dimension], parameters, dimension, document["version"])
                else:
                    # Only the active axis definition is evaluated.
                    if relation is not None:
                        raise ProjectError("Referenced box extents cannot coexist with its placement relation.")
                    _object(extents, ("min", "max"), f"{axis}_extents")
                    references.extend(extents.values())
                for reference in references:
                    if not isinstance(reference, dict):
                        raise ProjectError("Extent reference must be an object.")
                    required = {"reference_id", "anchor"}
                    if not required <= set(reference) or set(reference) - required - {"offset_um", "port"}:
                        raise ProjectError("Invalid box extent reference fields.")
                    _string(reference["reference_id"], "extent.reference_id")
                    if reference["reference_id"] not in objects:
                        raise ProjectError(f"Missing reference object: {reference['reference_id']}.")
                    dependencies[object_id].add(reference["reference_id"])
                    if reference["anchor"] not in anchors:
                        raise ProjectError(f"Invalid {axis} extent anchor.")
                    _offset(reference.get("offset_um", 0), parameters, "extent.offset_um", document["version"])
                    if reference["anchor"] == "port":
                        _string(reference.get("port"), "extent.port")
                        if objects.get(reference["reference_id"], (None,))[0] != "instance":
                            raise ProjectError("Port extents require a linked instance.")
    visiting, done = set(), set()
    def visit(key):
        if key not in objects:
            raise ProjectError(f"Missing reference object: {key}.")
        if key in visiting:
            raise ProjectError(f"Placement cycle detected at {key}.")
        if key in done:
            return
        visiting.add(key)
        for reference in sorted(dependencies[key]):
            visit(reference)
        visiting.remove(key)
        done.add(key)
    for key in objects:
        visit(key)
    return document


def _property(obj, key):
    value = obj.property(key)
    return None if value is None else json.loads(value)


def _geometry_signature(cell, dbu):
    """Compare generated geometry, including paths/text and child-cell edits."""
    layout = pya.Layout()
    layout.dbu = dbu
    copy = layout.create_cell("COMPARE")
    copy.copy_tree(cell)
    copy.flatten(-1)
    return sorted((layout.get_info(layer).layer, layout.get_info(layer).datatype,
                   shape.to_s().split(" props=")[0])
                  for layer in layout.layer_indexes()
                  for shape in copy.shapes(layer).each())


def snapshot(session):
    session.validate()
    components, instances, boxes, unsupported = {}, [], [], []
    for instance in session.cell.each_inst():
        instance_id = instance.property("shapeforge.instance_id")
        component_id = session.component_id_for_instance(instance)
        if (not instance_id or component_id not in session.components
                or instance.is_regular_array() or instance.size() != 1):
            unsupported.append("unregistered instance or instance array")
            continue
        source = session.components[component_id]
        cell = session.layout.cell(instance.cell_index)
        recipe = session.component_recipes.get(component_id)
        if recipe is None:
            recipe = _property(cell, "shapeforge.component_recipe")
        if recipe is None:
            raise ProjectError(f"Component {cell.name} has no recorded generation recipe. Reinsert it with this version of ShapeForge.")
        if _geometry_signature(cell, session.layout.dbu) != _geometry_signature(source.kdb_cell, session.layout.dbu):
            raise ProjectError(f"Component {cell.name} was edited after generation. Its recipe cannot reproduce those edits.")
        components[component_id] = {"id": component_id, "recipe": recipe}
        transform = instance.dcplx_trans
        instances.append({
            "id": instance_id, "component_id": component_id,
            "transform": {"mag": transform.mag, "angle": transform.angle,
                          "mirror": transform.is_mirror(), "x_um": transform.disp.x,
                          "y_um": transform.disp.y},
            "placement": _property(instance, "shapeforge.placement"),
            "geometry_parameters": _property(instance, "shapeforge.geometry_parameters"),
            "port_position": _property(instance, "shapeforge.port_position"),
            "via_center": _property(instance, "shapeforge.via_center"),
            "array": _property(instance, "shapeforge.array"),
            "array_member": _property(instance, "shapeforge.array_member"),
        })
    for layer_index in session.layout.layer_indexes():
        info = session.layout.get_info(layer_index)
        for shape in session.cell.shapes(layer_index).each():
            if shape.property("shapeforge.route") is not None:
                unsupported.append("basic routing (not supported by the project format)")
                continue
            definition = _property(shape, "shapeforge.box")
            object_id = shape.property("shapeforge.object_id")
            if definition is None or not object_id or not shape.is_box():
                unsupported.append("unregistered geometry (register drawn boxes with Make Box Parametric)")
                continue
            box = shape.box
            boxes.append({
                "id": object_id,
                "layer": {"layer": info.layer, "datatype": info.datatype, "name": info.name},
                "box_dbu": [box.left, box.bottom, box.right, box.top],
                "definition": definition, "placement": _property(shape, "shapeforge.placement"),
                "array": _property(shape, "shapeforge.array"),
                "array_member": _property(shape, "shapeforge.array_member"),
            })
    if unsupported:
        raise ProjectError("Project was not saved because it contains:\n" + "\n".join(sorted(set(unsupported))))
    pdks = {entry["recipe"]["pdk"] for entry in components.values() if "pdk" in entry["recipe"]}
    if len(pdks) > 1:
        raise ProjectError("The project format supports one PDK per project.")
    info = _property(session.cell, "shapeforge.project") or {"name": session.cell.name, "description": ""}
    info["name"] = session.cell.name
    pdk = session.cell.property("shapeforge.pdk") or next(iter(pdks), None)
    document = {
        "format": FORMAT, "version": VERSION, "project": info,
        "environment": {"pdk": pdk, "dbu_um": session.layout.dbu,
                        "grid_um": session.grid_um},
        "parameters": session.parameters(),
        "public_parameters": session.public_parameters(),
        "components": sorted(components.values(), key=lambda entry: entry["id"]),
        "instances": sorted(instances, key=lambda entry: entry["id"]),
        "boxes": sorted(boxes, key=lambda entry: entry["id"]),
    }
    validate_document(document)
    session.regeneration_plan()  # Validate future component geometry and dependent placement without mutation.
    # The snapshot must not expose mutable recipes owned by the provider cache.
    return json.loads(json.dumps(document, allow_nan=False))


def save_project(session, path):
    """Write beside the destination, flush, then atomically replace it."""
    document = snapshot(session)
    path = Path(path).resolve()
    from shapeforge.project_links import PROVIDER, read_tree
    for entry in document["components"]:
        if entry["recipe"]["provider"] == PROVIDER:
            # Validate the destination ancestry too: Save As must not overwrite
            # a dependency and thereby turn a valid graph into a recursive one.
            read_tree(entry["recipe"]["source"], (path,))
            entry["recipe"]["source"] = os.path.relpath(entry["recipe"]["source"], path.parent)
    serialized = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise ProjectError(f"Could not save project '{path}': {error}") from error
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    session.project_path = str(path)
    return document


class _DetachedCellView:
    """Use the existing Session planner on a layout that has no GUI view yet."""
    def __init__(self, layout):
        self._layout = layout

    def layout(self):
        return self._layout

    def is_valid(self):
        return True


class PreparedProject:
    def __init__(self, session, warnings, path):
        self.session = session
        self.warnings = warnings
        self.path = str(path)

    def attach(self, cellview):
        """Copy a fully regenerated project into an empty editor layout."""
        layout = cellview.layout()
        layout.dbu = self.session.layout.dbu
        cell = layout.create_cell(self.session.cell.name)
        cell.copy_tree(self.session.cell)
        for key in ("shapeforge.parameters", "shapeforge.public_parameters", "shapeforge.project", "shapeforge.pdk"):
            value = self.session.cell.property(key)
            if value is not None:
                cell.set_property(key, value)
        session = Session(cellview, cell, grid_um=self.session.grid_um)
        session.components = dict(self.session.components)
        session.component_recipes = dict(self.session.component_recipes)
        session.project_path = self.path
        cellview.cell = cell
        return session


def _reject_constant(value):
    raise ProjectError(f"Invalid JSON numeric constant: {value}.")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectError(f"Duplicate JSON field: {key}.")
        result[key] = value
    return result


def prepare_project(path):
    """Validate the include graph before invoking providers; stage off-screen."""
    from shapeforge.project_links import read_tree
    document, _ = read_tree(path)
    return prepare_document(document, Path(path).resolve())


def prepare_document(document, path):
    from shapeforge.project_links import project_scope
    with project_scope(path):
        return _prepare_document(document, path)


def _prepare_document(document, path):
    try:
        validate_document(document)
        layout = pya.Layout()
        layout.dbu = document["environment"]["dbu_um"]
        cell = layout.create_cell(document["project"]["name"])
        session = Session(_DetachedCellView(layout), cell, grid_um=document["environment"]["grid_um"])
        cell.set_property("shapeforge.parameters", json.dumps(document["parameters"], allow_nan=False))
        cell.set_property("shapeforge.project", json.dumps(document["project"], allow_nan=False))
        cell.set_property("shapeforge.public_parameters", json.dumps(document.get("public_parameters", [])))
        session.project_path = str(path)
        if document["environment"]["pdk"] is not None:
            cell.set_property("shapeforge.pdk", document["environment"]["pdk"])
        warnings, cells = [], {}
        for index, entry in enumerate(document["components"]):
            generated, changes = reconstruct_component(entry["recipe"])
            warnings.extend(changes)
            component_id = entry["id"]
            session.components[component_id] = generated.component
            # Future saves describe what was actually rebuilt in this environment.
            session.component_recipes[component_id] = generated.recipe
            key = id(generated.component)
            if key not in cells:
                target = layout.create_cell(f"SF_COMPONENT_{index}")
                target.copy_tree(generated.component.kdb_cell)
                target.set_property("shapeforge.component_recipe", json.dumps(generated.recipe, allow_nan=False))
                cells[key] = target
        for entry in document["instances"]:
            component_id = entry["component_id"]
            target = cells[id(session.components[component_id])]
            t = entry["transform"]
            transform = pya.DCplxTrans(t["mag"], t["angle"], t["mirror"], t["x_um"], t["y_um"])
            instance = cell.insert(pya.DCellInstArray(target.cell_index(), transform))
            instance.set_property("shapeforge.instance_id", entry["id"])
            instance.set_property("shapeforge.component_id", component_id)
            if entry.get("geometry_parameters") is not None:
                instance.set_property("shapeforge.geometry_parameters", json.dumps(entry["geometry_parameters"], allow_nan=False))
            if entry.get("port_position") is not None:
                instance.set_property("shapeforge.port_position", json.dumps(entry["port_position"], allow_nan=False))
            if entry.get("via_center") is not None:
                instance.set_property("shapeforge.via_center", json.dumps(entry["via_center"], allow_nan=False))
            for key in ("array", "array_member"):
                if entry.get(key) is not None:
                    instance.set_property("shapeforge." + key, json.dumps(entry[key], allow_nan=False))
            if entry["placement"] is not None:
                instance.set_property("shapeforge.placement", json.dumps(entry["placement"], allow_nan=False))
        for entry in document["boxes"]:
            info = entry["layer"]
            layer = layout.layer(pya.LayerInfo(info["layer"], info["datatype"], info["name"]))
            shape = cell.shapes(layer).insert(pya.Box(*entry["box_dbu"]))
            shape.set_property("shapeforge.object_id", entry["id"])
            shape.set_property("shapeforge.box", json.dumps(entry["definition"], allow_nan=False))
            if entry["placement"] is not None:
                shape.set_property("shapeforge.placement", json.dumps(entry["placement"], allow_nan=False))
            for key in ("array", "array_member"):
                if entry.get(key) is not None:
                    shape.set_property("shapeforge." + key, json.dumps(entry[key], allow_nan=False))
        components, updates = session.regeneration_plan()
        session.apply_regeneration_plan(components, updates)
        return PreparedProject(session, sorted(set(warnings)), path)
    except ProjectError:
        raise
    except Exception as error:
        raise ProjectError(f"Could not open project '{path}': {error}") from error
