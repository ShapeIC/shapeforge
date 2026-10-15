"""Operation-local graph of parameters, generated geometry, sizes and positions."""

from contextlib import contextmanager
import json
import math

import pya

from shapeforge.parameters import (ParameterValues, validate_definitions, is_measure,
                                   validate_measure, expression_tree, _evaluate)


class Evaluation:
    def __init__(self, session, definitions=None, input_replacements=None, recipe_replacements=None,
                 replacement=None, box_replacement=None, component_replacements=None,
                 via_center_replacement=None, array_replacement=None):
        self.session = session
        definitions = session.parameter_definitions() if definitions is None else definitions
        validate_definitions(definitions)
        self.parameters = ParameterValues(definitions, lookup=self.parameter)
        self.objects = session.placement_objects(raw=True, box_replacement=box_replacement)
        self.inputs, self.recipes = input_replacements or {}, recipe_replacements or {}
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
        self.cache, self.path, self.components, self.offsets = {}, [], {}, {}
        self.measured_ids = set()

    def item(self, identity):
        if identity not in self.objects:
            raise ValueError(f"Missing reference object: {identity}.")
        return self.objects[identity]

    @contextmanager
    def active(self):
        from shapeforge.project_links import project_scope
        previous = self.session._evaluation
        self.session._evaluation = self
        try:
            with project_scope(self.session.project_path):
                yield
        finally:
            self.session._evaluation = previous

    def node(self, key, compute):
        if key in self.cache:
            return self.cache[key]
        if key in self.path:
            cycle = self.path[self.path.index(key):] + [key]
            raise ValueError("Geometry dependency cycle detected: " + " → ".join(": ".join(k) for k in cycle))
        if len(self.path) >= 128:
            raise ValueError("Geometry dependency chain is too deep.")
        self.path.append(key)
        try:
            result = compute()
            self.cache[key] = result
            return result
        finally:
            self.path.pop()

    def parameter(self, name):
        if name not in self.parameters:
            raise ValueError(f"Unknown parameter: {name}")
        definition = self.parameters[name]
        return self.node(("parameter", name), lambda: self.measure(definition) if is_measure(definition)
                         else _evaluate(expression_tree(definition), self.parameter))

    def linear(self, identity):
        item = self.item(identity)
        if item.get("array_member") is not None:
            return self.linear(item["array_member"]["owner_id"])
        t = self.session.object_transform(item)
        return pya.DCplxTrans(t.mag, t.angle, t.is_mirror(), 0, 0)

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
        from shapeforge.components import generate_inputs, input_definition, validate_input_definition
        from shapeforge.project import _geometry_signature
        item = self.item(identity)
        def compute():
            member = item.get("array_member")
            if member is not None:
                component = self.geometry(member["owner_id"])
                if item["type"] == "box":
                    item["local_box"] = self.item(member["owner_id"])["local_box"]
                    return None
                generated = self.components.get(member["owner_id"])
                if generated is not None and hasattr(item["instance"], "cell_index"):
                    self.components[identity] = generated
                item["component"] = component
                item["local_box"] = self.item(member["owner_id"])["local_box"]
                return component
            if item["type"] == "box":
                item["local_box"] = pya.DBox(0, 0, self.size(identity, "x"), self.size(identity, "y"))
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
            if generated is None and definition is not None:
                validate_input_definition(definition, recipe, self.parameters)
                values = definition["values"]
                if definition["version"] == 3:
                    values = self.session.resolve_via_inputs(values, instance.dcplx_trans.mag)
                generated = generate_inputs(recipe, values, self.parameters)
            if generated is not None:
                if generated.component is not old or generated.recipe != self.session.component_recipes.get(component_id):
                    if old is None or _geometry_signature(self.session.layout.cell(instance.cell_index), self.session.layout.dbu) != _geometry_signature(old.kdb_cell, self.session.layout.dbu):
                        raise ValueError(f"Instance {identity} was edited manually; its recipe cannot preserve those edits.")
                    self.components[identity] = generated
                if identity in self.inputs:
                    self.components[identity] = generated
                item["component"] = generated.component
            component = item["component"]
            if identity in self.components:
                # Measure the geometry as it will be copied at the session DBU.
                scratch = pya.Layout()
                scratch.dbu = self.session.layout.dbu
                copied = scratch.create_cell("BOUNDS")
                copied.copy_tree(component.kdb_cell)
                item["local_box"] = copied.dbbox()
            else:
                item["local_box"] = self.session.layout.cell(instance.cell_index).dbbox()
            return component
        return self.node(("geometry", identity), compute)

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
                    raise ValueError("Box extents or center control its position. Remove placement first.")
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
        return self.node(("position", identity), compute)

    def plan(self):
        from shapeforge.arrays import collect, prepare
        from shapeforge.project_ports import validate_position
        from shapeforge.technology import is_via_stack
        from shapeforge.project_ports import is_project_port
        with self.active():
            collect(self.objects, self.parameters, validate_values=False)
            for identity in list(self.objects):
                self.geometry(identity)
            removals = prepare(self.session, self.objects, self.array_replacement)
            if self.measured_ids & removals:
                raise ValueError("Cannot reduce the array: a removed repetition is referenced by a parameter.")
            # Positions already resolved through extent dependencies stay valid:
            # prepare preserves existing members and rejects referenced removals.
            updates = []
            for identity, item in self.objects.items():
                if item["type"] == "box" and item.get("array_member") is None:
                    self.session.validate_box_definition(item["definition"])
                for key, test in (("via_center", is_via_stack), ("port_position", is_project_port)):
                    if item.get(key) is not None:
                        if not test(item.get("recipe")):
                            raise ValueError(f"Invalid {key} generator.")
                        validate_position(item[key], self.parameters)
                transform = self.position(identity)
                if item["type"] == "box" or identity in self.relations or item.get("via_center") or item.get("port_position") or item.get("array") or item.get("array_member"):
                    updates.append((item, transform))
            for name in self.parameters:
                self.parameter(name)
            updates.extend(({"type": "array_remove", "id": identity}, None) for identity in sorted(removals))
            return self.components, updates
