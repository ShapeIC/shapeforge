"""Operation-local graph of parameters, generated geometry, sizes and positions."""

from contextlib import contextmanager
import json
import math
from time import perf_counter

import pya
from shapeforge import runtime
from shapeforge.incremental import Memo, Components, Updates, EvaluationError

from shapeforge.parameters import (ParameterValues, validate_definitions, is_measure,
                                   validate_measure, expression_tree, _evaluate)


class Evaluation:
    def __init__(self, session, definitions=None, input_replacements=None, recipe_replacements=None,
                 replacement=None, box_replacement=None, component_replacements=None,
                 via_center_replacement=None, array_replacement=None, refresh_sources=False, full=False):
        self.session = session
        definitions = session.parameter_definitions() if definitions is None else definitions
        validate_definitions(definitions)
        from shapeforge.instance_parameters import instances
        self.namespaces = {name: i.property("shapeforge.instance_id") for name, i in instances(session, definitions).items()}
        self.parameters = ParameterValues(definitions, lookup=self.parameter)
        self.objects = session.placement_objects(raw=True, box_replacement=box_replacement)
        self.inputs, self.recipes = input_replacements or {}, recipe_replacements or {}
        self.refresh_sources = refresh_sources or bool(runtime.current() and runtime.current().refresh)
        self.full = full
        # Candidate position/box/array changes can affect measurements and hence
        # invalidate components computed before the candidate was supplied.
        candidate = any(value is not None for value in (replacement, box_replacement, via_center_replacement, array_replacement))
        self.forced = {} if candidate else component_replacements or {}
        self.array_replacement = array_replacement
        if array_replacement is not None:
            identity, definition = array_replacement
            self.item(identity)["array"] = definition
        if via_center_replacement is not None:
            identity, position = via_center_replacement
            self.item(identity)["via_center"] = position
        self.relations = {identity: json.loads(raw) for identity, item in self.objects.items()
                          if (raw := item["object"].property("shapeforge.placement")) is not None}
        if replacement is not None:
            identity, relation = replacement
            self.item(identity)
            self.relations[identity] = relation
        self.components, self.offsets = Components(), {}
        self.components.evaluation = self
        self.source_state = None
        self._signatures = {}
        self.geometry_aux = dict(getattr(session, "_geometry_aux", {}))
        environment = (session.layout.dbu, session.grid_um, session.pdk)
        previous = (getattr(session, "_nodes", {}) if not full and not self.refresh_sources
                    and getattr(session, "_node_environment", None) == environment else {})
        self.environment = environment
        self.memo = Memo(previous, self.intrinsic)
        self.cache, self.path = self.memo.values, self.memo.path
        self.measured_ids = set()

    def intrinsic(self, key):
        kind, identity, *rest = key
        if kind == "parameter":
            return json.dumps(self.parameters.get(identity), sort_keys=True)
        item = self.objects.get(identity)
        if item is None:
            return "missing"
        if kind == "internal parameter":
            return tuple(sorted(name for name, value in self.namespaces.items() if value == identity))
        if kind == "linear":
            if item.get("array_member"):
                return item["array_member"]["owner_id"]
            transform = self.session.object_transform(item)
            return transform.mag, transform.angle, transform.is_mirror()
        if kind == "box size":
            axis = rest[0]
            definition = item["definition"]
            return json.dumps([definition.get(f"{axis}_extents"),
                               definition.get("width_um" if axis == "x" else "height_um")], sort_keys=True)
        if kind == "box validation":
            return json.dumps(item["definition"], sort_keys=True)
        if kind == "position validation":
            return json.dumps([item.get("via_center"), item.get("port_position"),
                               (item.get("recipe") or {}).get("generator")], sort_keys=True)
        if kind == "geometry":
            if item.get("array_member") is not None:
                return json.dumps(item["array_member"], sort_keys=True)
            if item["type"] == "box":
                return json.dumps([item.get("array_member"), {k: v for k, v in item["definition"].items() if k not in ("version", "x_center", "y_center")}], sort_keys=True)
            instance = item["instance"]
            if not hasattr(instance, "cell_index"):
                return json.dumps(item.get("array_member"), sort_keys=True)
            return (self.session.component_id_for_instance(instance), instance.cell_index,
                    self.session.layout.cell(instance.cell_index).dbbox().to_s(),
                    json.dumps(self.inputs.get(identity), sort_keys=True),
                    instance.property("shapeforge.geometry_parameters"),
                    json.dumps(self.recipes.get(identity), sort_keys=True),
                    instance.property("shapeforge.internal_parameters"),
                    json.dumps(item.get("array_member"), sort_keys=True), self.source_version(item.get("recipe")))
        if kind == "position":
            return (self.session.object_transform(item).to_s(), json.dumps([
                item.get("definition"), self.relations.get(identity), item.get("via_center"), item.get("port_position"),
                item.get("array"), item.get("array_member")], sort_keys=True))
        return item["type"]

    def source_version(self, recipe):
        if not recipe or self.source_state is None:
            return None
        entries = self.source_state.entries
        provider = recipe["provider"]
        if provider == "shapeforge-project":
            from pathlib import Path
            value = entries.get(("project_tree", str(Path(recipe["source"]).resolve())))
            return value[1] if value else recipe["dependency_digest"]
        if provider == "external-python":
            value = entries.get(("external_sources", recipe["library"], recipe["pdk"]))
            return value["digest"] if value else recipe["implementation_digest"]
        if provider in ("shapeic-cellkit", "shapeic-cellkit-technology"):
            if provider == "shapeic-cellkit":
                value = entries.get(("primitive", recipe["pdk"], recipe["primitive"]))
                return value.implementation_digest if value else recipe["implementation_digest"]
            catalog = entries.get(("catalog", recipe["pdk"]))
            value = entries.get(("technology", str(catalog.root), recipe["pdk"])) if catalog else None
            return value[1] if value else recipe["implementation_digest"]
        return None

    def item(self, identity):
        if identity not in self.objects:
            raise EvaluationError(f"Missing reference object: {identity}.")
        return self.objects[identity]

    @contextmanager
    def active(self):
        from shapeforge.project_links import project_scope
        previous = self.session._evaluation
        self.session._evaluation = self
        try:
            with runtime.scope(self.session, refresh=self.refresh_sources) as sources:
                self.source_state = sources
                with project_scope(self.session.project_path):
                    yield
        finally:
            self.session._evaluation = previous

    def node(self, key, compute):
        return self.memo.call(key, compute)

    def parameter(self, name):
        if "." in name:
            alias, parameter = name.split(".")
            if alias not in self.namespaces:
                raise ValueError(f"Unknown linked instance: {alias}")
            identity = self.namespaces[alias]
            def compute():
                self.measured_ids.add(identity)
                self.geometry(identity)
                generated = self.components.get(identity)
                from shapeforge.instance_parameters import outputs
                values = (generated.internal_parameters if generated is not None and generated.internal_parameters is not None
                          else outputs(self.item(identity)["instance"]))
                if parameter not in values:
                    raise ValueError(f"Instance {alias} has no internal parameter '{parameter}'.")
                return values[parameter]
            return self.node(("internal parameter", identity, parameter), compute)
        if name not in self.parameters:
            raise ValueError(f"Unknown parameter: {name}")
        definition = self.parameters[name]
        return self.node(("parameter", name), lambda: self.measure(definition) if is_measure(definition)
                         else _evaluate(expression_tree(definition), self.parameter))

    def linear(self, identity):
        def compute():
            item = self.item(identity)
            if item.get("array_member") is not None:
                return self.linear(item["array_member"]["owner_id"])
            t = self.session.object_transform(item)
            return pya.DCplxTrans(t.mag, t.angle, t.is_mirror(), 0, 0)
        return self.node(("linear", identity), compute)

    def measure(self, definition):
        validate_measure(definition)
        identity, quantity = definition["reference_id"], definition["quantity"]
        self.measured_ids.add(identity)
        item = self.item(identity)
        def compute():
            if quantity == "port_width":
                if item["type"] != "instance":
                    raise ValueError("Port width requires a linked instance.")
                component = self.geometry(identity)
                if component is None:
                    raise ValueError("Port width requires a linked component.")
                ports = {port.name: port for port in component.ports}
                if definition["port"] not in ports:
                    raise ValueError(f"Instance {identity} has no port '{definition['port']}'.")
                value = float(ports[definition["port"]].width) * abs(self.linear(identity).mag)
            elif item["type"] == "box":
                value = self.size(identity, "x" if quantity == "width" else "y")
            else:
                self.geometry(identity)
                bounds = self.session.placement_bbox(item, self.linear(identity))
                value = bounds.width() if quantity == "width" else bounds.height()
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Measurement {identity} / {quantity} must be positive and finite.")
            return value
        return self.node(("measurement", identity, quantity, definition.get("port", "")), compute)

    def geometry(self, identity):
        from shapeforge.components import generate_inputs, input_definition, validate_input_definition, effective_inputs
        item = self.item(identity)
        def compute():
            member = item.get("array_member")
            if member is not None:
                component = self.geometry(member["owner_id"])
                if item["type"] == "box":
                    item["local_box"] = self.item(member["owner_id"])["local_box"]
                    self.geometry_aux[identity] = (None, item["local_box"].dup(), None)
                    return None
                generated = self.components.get(member["owner_id"])
                if generated is not None and hasattr(item["instance"], "cell_index"):
                    self.components[identity] = generated
                item["component"] = component
                item["local_box"] = self.item(member["owner_id"])["local_box"]
                self.geometry_aux[identity] = (component, item["local_box"].dup(), generated)
                return component
            if item["type"] == "box":
                item["local_box"] = pya.DBox(0, 0, self.size(identity, "x"), self.size(identity, "y"))
                self.geometry_aux[identity] = (None, item["local_box"].dup(), None)
                return None
            instance = item["instance"]
            component_id = self.session.component_id_for_instance(instance)
            old = self.session.components.get(component_id)
            recipe = self.recipes.get(identity, self.session.component_recipes.get(component_id))
            raw = instance.property("shapeforge.geometry_parameters")
            definition = json.loads(raw) if raw else None
            if identity in self.inputs:
                definition = input_definition(self.inputs[identity], recipe["provider"]) or {
                    "version": 1 if recipe["provider"] == "shapeic-cellkit" else 2,
                    "values": self.inputs[identity]}
            generated = self.forced.get(identity)
            if definition is None and self.refresh_sources and recipe and recipe["provider"] != "shapeforge":
                definition = {"version": 4 if recipe["provider"] == "shapeforge-project" else
                              1 if recipe["provider"] == "shapeic-cellkit" else 2,
                              "values": recipe.get("overrides", recipe.get("geometry", recipe.get("arguments")))}
            if generated is None and definition is not None:
                validate_input_definition(definition, recipe, self.parameters)
                values = definition["values"]
                if definition["version"] == 3:
                    values = self.session.resolve_via_inputs(values, self.linear(identity).mag)
                effective, previous = effective_inputs(recipe, values, self.parameters)
                stamp_key = "dependency_digest" if recipe["provider"] == "shapeforge-project" else "implementation_digest"
                unchanged_source = self.source_version(recipe) == recipe.get(stamp_key)
                if not self.refresh_sources and identity not in self.recipes and effective == previous and unchanged_source:
                    runtime.count("generation_reused_loaded")
                else:
                    runtime.count("provider_requests")
                    with runtime.phase("generation"):
                        generated = generate_inputs(recipe, values, self.parameters)
            if generated is not None:
                item["component"] = generated.component
            component = item["component"]
            if generated is not None and generated.component is not old:
                # Measure the geometry as it will be copied at the session DBU.
                bounds = self.session._bounds
                key = (id(component), self.session.layout.dbu)
                if key not in bounds:
                    scratch = pya.Layout()
                    scratch.dbu = self.session.layout.dbu
                    copied = scratch.create_cell("BOUNDS")
                    runtime.count("bounds_copies")
                    copied.copy_tree(component.kdb_cell)
                    bounds[key] = (component, copied.dbbox())
                item["local_box"] = bounds[key][1]
            else:
                item["local_box"] = self.session.layout.cell(instance.cell_index).dbbox()
            self.geometry_aux[identity] = (component, item["local_box"].dup(), generated)
            return component
        result = self.node(("geometry", identity), compute)
        component, bounds, generated = self.geometry_aux[identity]
        item["component"], item["local_box"] = component, bounds.dup()
        if item.get("array_member") is not None:
            owner = item["array_member"]["owner_id"]
            self.geometry(owner)
            generated = self.components.get(owner)
        if generated is not None and item["type"] == "instance" and hasattr(item["instance"], "cell_index"):
            from shapeforge.instance_parameters import outputs
            instance = item["instance"]
            component_id = self.session.component_id_for_instance(instance)
            if (generated.component is not self.session.components.get(component_id)
                    or generated.recipe != self.session.component_recipes.get(component_id)
                    or generated.internal_parameters is not None and generated.internal_parameters != outputs(instance)):
                self.check_original(identity)
                self.components[identity] = generated
        return result

    def signature(self, cell):
        from shapeforge.project import _geometry_signature
        key = (cell.layout(), cell.cell_index(), self.session.layout.dbu)
        if key not in self._signatures:
            runtime.count("geometry_signatures")
            self._signatures[key] = _geometry_signature(cell, self.session.layout.dbu)
        return self._signatures[key]

    def check_original(self, identity):
        item = self.item(identity)
        instance = item["instance"]
        old = self.session.components.get(self.session.component_id_for_instance(instance))
        if old is None or self.signature(self.session.layout.cell(instance.cell_index)) != self.signature(old.kdb_cell):
            raise ValueError(f"Instance {identity} was edited manually; its recipe cannot preserve those edits.")

    def coordinate(self, reference, axis):
        identity = reference["reference_id"]
        transform = self.position(identity)
        return self.session.extent_coordinate(reference, axis, self.objects, {identity: transform})

    def size(self, identity, axis):
        item = self.item(identity)
        def compute():
            if item.get("array_member") is not None:
                return self.size(item["array_member"]["owner_id"], axis)
            definition = item["definition"]
            extents = definition.get(f"{axis}_extents")
            if extents:
                lower = self.coordinate(extents["min"], axis)
                upper = self.coordinate(extents["max"], axis)
                if upper <= lower:
                    raise ValueError(f"Box {identity}: {axis.upper()} maximum must exceed minimum.")
                value = self.session.snap_um_to_dbu(upper) - self.session.snap_um_to_dbu(lower)
            else:
                value = self.session.snap_um_to_dbu(self.session.resolve_dimension(
                    definition["width_um" if axis == "x" else "height_um"]))
            if value <= 0:
                raise ValueError(f"Box {identity}: dimensions collapse after grid snapping.")
            return self.session.dbu_to_um(value)
        return self.node(("box size", identity, axis), compute)

    def position(self, identity):
        from shapeforge.arrays import member_transform
        item = self.item(identity)
        def compute():
            self.geometry(identity)
            transform = self.session.object_transform(item)
            relation = self.relations.get(identity)
            member = item.get("array_member")
            if member is not None:
                if relation is not None:
                    raise ValueError("Array controls this repetition's position.")
                owner = member["owner_id"]
                transform = member_transform(self.session, item, self.item(owner), self.position(owner))
            elif item["type"] == "box":
                definition = item["definition"]
                governed = any(definition.get(k) is not None for k in ("x_extents", "y_extents", "x_center", "y_center"))
                if governed and relation is not None:
                    raise EvaluationError("Box extents or center control its position. Remove its placement relation first.")
                coords = [transform.disp.x, transform.disp.y]
                for index, axis in enumerate(("x", "y")):
                    extents = definition.get(f"{axis}_extents")
                    if extents:
                        coords[index] = self.session.dbu_to_um(self.session.snap_um_to_dbu(self.coordinate(extents["min"], axis)))
                    elif f"{axis}_center" in definition:
                        center = definition[f"{axis}_center"]
                        coordinate = self.coordinate(center, axis) if isinstance(center, dict) else self.session.resolve_offset(center)
                        coords[index] = self.session.dbu_to_um(self.session.snap_um_to_dbu(coordinate - self.size(identity, axis) / 2))
                transform = pya.DCplxTrans(1, 0, False, *coords)
            position = item.get("via_center") or item.get("port_position")
            if position and any(f"{axis}_center" in position for axis in ("x", "y")):
                if relation is not None:
                    raise ValueError("Center coordinates control this object's position. Remove placement first.")
                coords = [transform.disp.x, transform.disp.y]
                bounds = self.session.placement_bbox(item, self.linear(identity)) if item.get("via_center") else None
                for index, axis in enumerate(("x", "y")):
                    if f"{axis}_center" in position:
                        value = position[f"{axis}_center"]
                        coordinate = self.coordinate(value, axis) if isinstance(value, dict) else self.session.resolve_offset(value)
                        offset = ((bounds.left + bounds.right) / 2 if axis == "x" else (bounds.bottom + bounds.top) / 2) if bounds else 0
                        coords[index] = self.session.dbu_to_um(self.session.snap_um_to_dbu(coordinate - offset))
                transform = pya.DCplxTrans(transform.mag, transform.angle, transform.is_mirror(), *coords)
            if relation is not None:
                reference = relation["reference_id"]
                transform, dx, dy = self.session.relation_transform(self.item(reference), item, relation,
                    transform_a=self.position(reference), transform_b=transform)
                self.offsets[identity] = (dx, dy)
            return transform
        result = self.node(("position", identity), compute)
        if identity not in self.offsets and identity in self.relations:
            # Offsets describe the requested relation, not a native wrapper.
            self.offsets[identity] = (self.session.resolve_offset(self.relations[identity]["dx_um"]),
                                      self.session.resolve_offset(self.relations[identity]["dy_um"]))
        return result

    def plan(self):
        started = perf_counter()
        from shapeforge.arrays import collect, prepare
        from shapeforge.project_ports import validate_position
        from shapeforge.technology import is_via_stack
        from shapeforge.project_ports import is_project_port
        with self.active():
            runtime.count("plans" if self.source_state.owner is self.session else "child_plans")
            collect(self.objects, self.parameters, validate_values=False)
            for identity in list(self.objects):
                self.geometry(identity)
            removals = prepare(self.session, self.objects, self.array_replacement)
            if self.measured_ids & removals:
                raise ValueError("Cannot reduce the array: a removed repetition is referenced by a parameter.")
            # Positions already resolved through extent dependencies stay valid:
            # prepare preserves existing members and rejects referenced removals.
            updates = Updates()
            updates.evaluation = self
            for identity, item in self.objects.items():
                if item["type"] == "box" and item.get("array_member") is None:
                    self.node(("box validation", identity), lambda item=item: self.session.validate_box_definition(item["definition"]))
                def validate(item=item):
                    for key, test in (("via_center", is_via_stack), ("port_position", is_project_port)):
                        if item.get(key) is not None:
                            if not test(item.get("recipe")):
                                raise ValueError(f"Invalid {key} generator.")
                            validate_position(item[key], self.parameters)
                if item.get("via_center") is not None or item.get("port_position") is not None:
                    self.node(("position validation", identity), validate)
                transform = self.position(identity)
                if item["type"] == "box" or identity in self.relations or item.get("via_center") or item.get("port_position") or item.get("array") or item.get("array_member"):
                    updates.append((item, transform))
            for name in self.parameters:
                self.parameter(name)
            self.measured_ids.update(key[1] for key in self.memo.observed
                                     if key[0] in ("measurement", "internal parameter"))
            if self.measured_ids & removals:
                raise ValueError("Cannot reduce the array: a removed repetition is referenced by a parameter.")
            updates.extend(({"type": "array_remove", "id": identity}, None) for identity in sorted(removals))
            from shapeforge.changes import changed_updates
            updates[:] = changed_updates(self.session, updates, self.components)
            self.session._nodes = {key: entry for key, entry in self.memo.entries.items() if key in self.memo.observed}
            self.session._node_environment = self.environment
            self.session._geometry_aux = {identity: value for identity, value in self.geometry_aux.items() if identity in self.objects}
            self.source_state.seconds["evaluation"] = self.source_state.seconds.get("evaluation", 0) + perf_counter() - started
            self.session.performance = {"counts": dict(self.source_state.counts), "seconds": dict(self.source_state.seconds)}
            return self.components, updates
