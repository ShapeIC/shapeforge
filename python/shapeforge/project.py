"""Versioned recipes and staged reconstruction of ShapeForge projects."""

import json
import math
import os
from pathlib import Path
import tempfile

import pya

from shapeforge.components import reconstruct_component
from shapeforge.session import Session

FORMAT = "shapeforge"
VERSION = 1


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


def _offset(value, parameters, where):
    if isinstance(value, str):
        if value not in parameters:
            raise ProjectError(f"{where}: unknown parameter '{value}'.")
    else:
        _number(value, where)


def _version(value, where):
    if type(value) is not int or value != VERSION:
        raise ProjectError(f"{where}: unsupported version {value!r}; supported version is {VERSION}.")


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
    else:
        raise ProjectError(f"{where}: unknown provider {provider!r}.")


def validate_document(document):
    """Validate JSON structure and references before invoking any provider."""
    if not isinstance(document, dict):
        raise ProjectError("The project must be a JSON object.")
    _version(document.get("version"), "Project format")
    _object(document, ("format", "version", "project", "environment", "parameters",
                       "components", "instances", "boxes"), "Project")
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
    for name, value in parameters.items():
        if not name.isidentifier():
            raise ProjectError(f"Invalid parameter name: {name!r}.")
        _number(value, f"parameter {name}")
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
        pdk = entry["recipe"].get("pdk")
        if pdk is not None and pdk != environment["pdk"]:
            raise ProjectError("Component PDK differs from the project PDK.")
    objects = {}
    for kind, rows, keys in (
        ("instance", document["instances"], ("id", "component_id", "transform", "placement")),
        ("box", document["boxes"], ("id", "layer", "box_dbu", "definition", "placement")),
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
    dependencies = {key: set() for key in objects}
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
                _offset(relation[key], parameters, f"placement.{key}")
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
        if kind == "box":
            definition = entry["definition"]
            if not isinstance(definition, dict):
                raise ProjectError("Box definition must be an object.")
            if type(definition.get("version")) is not int or definition["version"] not in (1, 2):
                raise ProjectError("Unsupported box definition version; supported versions are 1 and 2.")
            if set(definition) - {"version", "width_um", "height_um", "x_extents", "y_extents", "x_center", "y_center"}:
                raise ProjectError("Unknown box definition fields.")
            for axis, dimension, anchors in (
                ("x", "width_um", ("left", "center", "right", "port")),
                ("y", "height_um", ("bottom", "center", "top", "port")),
            ):
                extents = definition.get(f"{axis}_extents")
                references = []
                if f"{axis}_center" in definition:
                    if definition["version"] != 2:
                        raise ProjectError("Parametric box centers require box definition version 2.")
                    if extents is not None:
                        raise ProjectError(f"{axis.upper()} center and two referenced extents cannot coexist.")
                    if relation is not None:
                        raise ProjectError("Parametric box center cannot coexist with its placement relation.")
                    center = definition[f"{axis}_center"]
                    if isinstance(center, dict):
                        references.append(center)
                    else:
                        _offset(center, parameters, f"{axis}_center")
                if extents is None:
                    if dimension not in definition:
                        raise ProjectError(f"Missing box {dimension}.")
                    _offset(definition[dimension], parameters, dimension)
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
                    _offset(reference.get("offset_um", 0), parameters, "extent.offset_um")
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
        })
    for layer_index in session.layout.layer_indexes():
        info = session.layout.get_info(layer_index)
        for shape in session.cell.shapes(layer_index).each():
            if shape.property("shapeforge.route") is not None:
                unsupported.append("basic routing (not supported by project format v1)")
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
            })
    if unsupported:
        raise ProjectError("Project was not saved because it contains:\n" + "\n".join(sorted(set(unsupported))))
    pdks = {entry["recipe"]["pdk"] for entry in components.values() if "pdk" in entry["recipe"]}
    if len(pdks) > 1:
        raise ProjectError("Project format v1 supports one PDK per project.")
    info = _property(session.cell, "shapeforge.project") or {"name": session.cell.name, "description": ""}
    info["name"] = session.cell.name
    pdk = session.cell.property("shapeforge.pdk") or next(iter(pdks), None)
    document = {
        "format": FORMAT, "version": VERSION, "project": info,
        "environment": {"pdk": pdk, "dbu_um": session.layout.dbu,
                        "grid_um": session.grid_um},
        "parameters": session.parameters(),
        "components": sorted(components.values(), key=lambda entry: entry["id"]),
        "instances": sorted(instances, key=lambda entry: entry["id"]),
        "boxes": sorted(boxes, key=lambda entry: entry["id"]),
    }
    validate_document(document)
    session.placement_plan()  # Missing ports, bad dimensions and incompatible constraints.
    # The snapshot must not expose mutable recipes owned by the provider cache.
    return json.loads(json.dumps(document, allow_nan=False))


def save_project(session, path):
    """Write beside the destination, flush, then atomically replace it."""
    document = snapshot(session)
    serialized = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path = Path(path)
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
        for key in ("shapeforge.parameters", "shapeforge.project", "shapeforge.pdk"):
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
    """Validate, rebuild and regenerate off-screen; no active session is changed."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            document = json.load(handle, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
        validate_document(document)
        layout = pya.Layout()
        layout.dbu = document["environment"]["dbu_um"]
        cell = layout.create_cell(document["project"]["name"])
        session = Session(_DetachedCellView(layout), cell, grid_um=document["environment"]["grid_um"])
        cell.set_property("shapeforge.parameters", json.dumps(document["parameters"], allow_nan=False))
        cell.set_property("shapeforge.project", json.dumps(document["project"], allow_nan=False))
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
        updates, _ = session.placement_plan()
        session.apply_placement_updates(updates)
        return PreparedProject(session, sorted(set(warnings)), path)
    except ProjectError:
        raise
    except Exception as error:
        raise ProjectError(f"Could not open project '{path}': {error}") from error
