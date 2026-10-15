import pya
import math
import os
from pathlib import Path
from uuid import uuid4
from shapeforge.components import (generate_cellkit, resolve_geometry, generate_inputs,
                                  input_definition, validate_input_definition, input_expressions)
from shapeforge.parameters import parameter_definition, references, resolve_parameters, resolve_value

import json

class Session:
    def __init__(self, cellview, cell, grid_um=0.005):
        self.cellview = cellview
        self.layout = cellview.layout()
        self.cell = cell

        if not math.isfinite(grid_um) or grid_um <= 0:
            raise ValueError("The grid must be a positive finite value.")

        grid_dbu = grid_um / self.layout.dbu
        self.grid_dbu = round(grid_dbu)

        if self.grid_dbu < 1 or not math.isclose(
            grid_dbu,
            self.grid_dbu,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("The grid must be an integer multiple of DBU.")

        self.components = {}
        self.component_recipes = {}
        self._evaluation = None
        self.project_path = None
        self.port_markers = []

    def validate(self):
        if not self.cellview.is_valid():
            raise RuntimeError(
                "The session layout is closed. Create a new PCell."
            )

        if self.cell._destroyed():
            raise RuntimeError(
                "The working cell was deleted. Create a new PCell."
            )    
    def insert_test_instance(self):
        return self.insert_primitive("simplediffpair", {
            "length_m": 0.4e-6, "finger_width_m": 5.0e-6, "nf": 4,
        })

    @property
    def pdk(self):
        self.validate()
        return self.cell.property("shapeforge.pdk") or os.environ.get("SHAPEFORGE_PDK", "ihp-sg13g2")

    def insert_primitive(self, primitive, geometry, pdk=None):
        """Generate a catalog primitive and insert it with its effective recipe."""
        self.validate()

        layout = self.layout
        view = self.cellview.view()

        if not layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        pdk = pdk or self.pdk
        recorded_pdk = self.cell.property("shapeforge.pdk")
        if recorded_pdk is not None and recorded_pdk != pdk:
            raise ValueError(f"This project uses PDK {recorded_pdk}; cannot insert a {pdk} primitive.")
        generated = generate_cellkit(primitive, resolve_geometry(geometry, self.parameters()), pdk=pdk)
        return self.insert_generated(generated, geometry)

    def insert_external_pcell(self, reference, arguments):
        from shapeforge.external import generate_external
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        reference = {**reference, "pdk": self.pdk}
        generated = generate_external(reference, arguments, self.parameters())
        # Fill optional defaults so the persisted binding covers every argument.
        arguments = {**generated.recipe["arguments"], **arguments}
        return self.insert_generated(generated, arguments)

    def insert_technology_pcell(self, generator, arguments):
        if generator == "project_port":
            raise ValueError("Use Add Project Port to define the project interface.")
        from shapeforge.technology import generate_technology
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        if generator == "via_stack":
            concrete = self.resolve_via_inputs(arguments)
        else:
            concrete = arguments
        generated = generate_technology(generator, concrete, self.pdk, self.parameters())
        return self.insert_generated(generated, {**generated.recipe["arguments"], **arguments})

    def insert_generated(self, generated, geometry):
        self.validate()
        layout, view = self.layout, self.cellview.view()
        if not layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        recorded = self.cell.property("shapeforge.pdk")
        if recorded is not None and recorded != generated.recipe["pdk"]:
            raise ValueError("Component PDK differs from the project PDK.")
        primitive = generated.recipe.get("primitive", generated.recipe.get("function", generated.recipe.get("generator", "component")))
        source = generated.component
        recipe = generated.recipe

        component_id = str(uuid4())
        gap = self.snap_um_to_dbu(5.0)

        if gap <= 0:
            raise RuntimeError("The grid is too coarse for this example.")

        bbox = self.cell.bbox()

        if bbox.empty():
            x = 0
        else:
            source_left = self.um_to_dbu(source.kdb_cell.dbbox().left)
            x = (
                math.ceil((bbox.right + gap - source_left) / self.grid_dbu)
                * self.grid_dbu
            )

        view.cancel()
        view.transaction("ShapeForge: insert gdsfactory component")

        try:
            self.cell.set_property("shapeforge.pdk", recipe["pdk"])
            target = layout.create_cell(f"SF_{primitive}_{component_id[:8]}")

            # Copiar geometría y jerarquía al layout de la sesión.
            target.copy_tree(source.kdb_cell)

            # El vínculo forma parte de la celda de KLayout.
            target.set_property(
                "shapeforge.component_id",
                component_id,
            )
            target.set_property("shapeforge.component_recipe", json.dumps(recipe, allow_nan=False))

            instance = self.cell.insert(
                pya.CellInstArray(
                    target.cell_index(),
                    pya.Trans(x, 0),
                )
            )

            instance.set_property(
                "shapeforge.instance_id",
                str(uuid4()),
            )
            instance.set_property(
                "shapeforge.component_id",
                component_id,
            )
            self.set_geometry_definition(instance, geometry, recipe["provider"])
        finally:
            view.commit()

        self.components[component_id] = source
        self.component_recipes[component_id] = recipe

        self.cellview.cell = self.cell
        view.add_missing_layers()
        view.max_hier()
        view.zoom_fit()
        return instance

    def selected_primitive_instance(self):
        self.validate()
        selection = [obj for obj in self.cellview.view().object_selection
                     if obj.cv_index == self.cellview.index()]
        if len(selection) != 1:
            raise RuntimeError("Select exactly one primitive instance.")
        obj = selection[0]
        if not obj.is_cell_inst() or obj.top != self.cell.cell_index() or len(obj.path) != 1:
            raise RuntimeError("Select a direct primitive instance in the working cell.")
        instance = obj.inst()
        if instance.is_regular_array() or instance.size() != 1:
            raise RuntimeError("Instance arrays are not supported.")
        instance_id = instance.property("shapeforge.instance_id")
        if not instance_id:
            raise RuntimeError("The selected instance has no ShapeForge ID.")
        if sum(other.property("shapeforge.instance_id") == instance_id
               for other in self.cell.each_inst()) != 1:
            raise RuntimeError("Duplicate instance ID. Run Keep Selected Instance ID first.")
        component_id = self.component_id_for_instance(instance)
        recipe = self.component_recipes.get(component_id)
        if recipe is None:
            raw = self.layout.cell(instance.cell_index).property("shapeforge.component_recipe")
            recipe = None if raw is None else json.loads(raw)
        if recipe is None or recipe.get("provider") not in ("shapeic-cellkit", "external-python", "shapeic-cellkit-technology", "shapeforge-project") or component_id not in self.components:
            raise RuntimeError("The selected instance needs a recorded generator recipe and linked component.")
        return instance, recipe

    def edit_primitive_parameters(self, geometry, source=None):
        """Replace only the selected instance's component; keep its identity and transform."""
        instance, recipe = self.selected_primitive_instance()
        if instance.property("shapeforge.array_member") is not None:
            raise ValueError("Array repetitions inherit geometry. Edit the base instance or dissolve the array.")
        from shapeforge.project_ports import is_project_port, edit, POSITION_PROPERTY
        if is_project_port(recipe):
            raw = instance.property(POSITION_PROPERTY)
            edit(self, instance.property("shapeforge.instance_id"), geometry, json.loads(raw) if raw else None)
            return True
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        from shapeforge.project import _geometry_signature
        old_id = self.component_id_for_instance(instance)
        old_source = self.components[old_id]
        old_cell = self.layout.cell(instance.cell_index)
        if _geometry_signature(old_cell, self.layout.dbu) != _geometry_signature(old_source.kdb_cell, self.layout.dbu):
            raise RuntimeError("This component was edited manually. Its recipe cannot preserve those edits.")
        if source is not None:
            if recipe["provider"] != "shapeforge-project":
                raise ValueError("Relink requires a linked project instance.")
            recipe = dict(recipe, source=str(Path(source).resolve()))
        from shapeforge.technology import is_via_stack
        if recipe["provider"] == "shapeforge-project" or self.has_measurements():
            identity = instance.property("shapeforge.instance_id")
            planned = self.primitive_update_plan(input_replacements={identity: geometry}, recipe_replacements={identity: recipe})
            self.placement_plan(component_replacements=planned)
            generated = planned.get(identity)
            if generated is None:
                generated = generate_inputs(recipe, geometry, self.parameters())
        elif is_via_stack(recipe):
            identity = instance.property("shapeforge.instance_id")
            planned = self.primitive_update_plan(input_replacements={identity: geometry})
            generated = planned.get(identity) or generate_inputs(
                recipe, self.resolve_via_inputs(geometry, instance.dcplx_trans.mag, planned), self.parameters())
        else:
            generated = generate_inputs(recipe, geometry, self.parameters())
        new_definition = input_definition(geometry, recipe["provider"])
        raw = instance.property("shapeforge.geometry_parameters")
        old_definition = None if raw is None else json.loads(raw)
        if generated.recipe == recipe and generated.component is old_source and new_definition == old_definition:
            return False
        component_id = str(uuid4())
        serialized = json.dumps(generated.recipe, allow_nan=False)
        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: edit primitive parameters")
        try:
            target = self.layout.create_cell(f"SF_{recipe.get('primitive', recipe.get('function', recipe.get('generator', 'component')))}_{component_id[:8]}")
            target.copy_tree(generated.component.kdb_cell)
            target.set_property("shapeforge.component_id", component_id)
            target.set_property("shapeforge.component_recipe", serialized)
            # Repoint the existing instance, preserving its transform, ID,
            # placement and every other instance property. Shared source cells
            # and recipes stay alive so undo/redo can restore either variant.
            instance.cell_index = target.cell_index()
            instance.set_property("shapeforge.component_id", component_id)
            self.set_geometry_definition(instance, geometry, recipe["provider"])
        finally:
            view.commit()
        self.components[component_id] = generated.component
        self.component_recipes[component_id] = generated.recipe
        view.add_missing_layers()
        return True

    @staticmethod
    def geometry_definition(geometry):
        return input_definition(geometry, "shapeic-cellkit")

    def set_geometry_definition(self, instance, geometry, provider="shapeic-cellkit"):
        definition = input_definition(geometry, provider)
        if definition is None:
            instance.delete_property("shapeforge.geometry_parameters")
        else:
            instance.set_property("shapeforge.geometry_parameters", json.dumps(definition, allow_nan=False))

    def primitive_update_plan(self, input_replacements=None, recipe_replacements=None):
        if self.has_measurements():
            from shapeforge.evaluation import Evaluation
            return Evaluation(self, input_replacements=input_replacements, recipe_replacements=recipe_replacements).plan()[0]
        from shapeforge.project_links import project_scope
        with project_scope(self.project_path):
            return self._primitive_update_plan(input_replacements, recipe_replacements)

    def _primitive_update_plan(self, input_replacements=None, recipe_replacements=None):
        objects = self.placement_objects(resolve_dimensions=False)
        from shapeforge.arrays import collect
        collect(objects, self.parameters(), validate_values=False)
        updates = {}
        from shapeforge.project import _geometry_signature
        from shapeforge.dimensions import width_references
        visiting, done = set(), set()
        def visit(object_id):
            if object_id in done:
                return
            if object_id in visiting:
                raise ValueError(f"Dimension generation cycle detected at {object_id}.")
            if object_id not in objects or objects[object_id]["type"] != "instance":
                raise ValueError(f"Port width reference requires a linked instance: {object_id}.")
            visiting.add(object_id)
            item = objects[object_id]
            instance = item["instance"]
            member = item.get("array_member")
            if member is not None:
                owner = member["owner_id"]
                visit(owner)
                from shapeforge.components import GeneratedComponent
                source = objects[owner]["instance"]
                component_id = self.component_id_for_instance(source)
                generated = updates.get(owner) or GeneratedComponent(self.components[component_id], self.component_recipes[component_id])
                current_id = self.component_id_for_instance(instance)
                if generated.component is not self.components.get(current_id) or generated.recipe != self.component_recipes.get(current_id):
                    updates[object_id] = generated
                visiting.remove(object_id)
                done.add(object_id)
                return
            raw = instance.property("shapeforge.geometry_parameters")
            replacement = (input_replacements or {}).get(object_id)
            definition = json.loads(raw) if raw is not None else None
            component_id = self.component_id_for_instance(instance)
            recipe = (recipe_replacements or {}).get(object_id, self.component_recipes.get(component_id))
            if replacement is not None:
                definition = input_definition(replacement, recipe["provider"]) or {"version": 2, "values": replacement}
            if definition is not None:
                if recipe is None or recipe.get("provider") not in ("shapeic-cellkit", "external-python", "shapeic-cellkit-technology", "shapeforge-project"):
                    raise ValueError(f"Instance {object_id} needs a recorded generator recipe.")
                validate_input_definition(definition, recipe, self.parameters())
                for ref in width_references(definition["values"]):
                    visit(ref["reference_id"])
                values = definition["values"]
                if definition["version"] == 3:
                    values = self.resolve_via_inputs(values, instance.dcplx_trans.mag, updates)
                generated = generate_inputs(recipe, values, self.parameters())
                if generated.recipe != self.component_recipes.get(component_id) or generated.component is not self.components[component_id]:
                    if _geometry_signature(self.layout.cell(instance.cell_index), self.layout.dbu) != _geometry_signature(self.components[component_id].kdb_cell, self.layout.dbu):
                        raise ValueError(f"Instance {object_id} was edited manually; its recipe cannot preserve those edits.")
                    updates[object_id] = generated
            visiting.remove(object_id)
            done.add(object_id)
        for object_id, item in objects.items():
            if item["type"] != "instance":
                continue
            visit(object_id)
        return updates

    def regeneration_plan(self, via_center_replacement=None):
        from shapeforge.project_ports import validate_names
        validate_names(self)
        if self.has_measurements():
            from shapeforge.evaluation import Evaluation
            return Evaluation(self, via_center_replacement=via_center_replacement).plan()
        components = self.primitive_update_plan()
        updates, _ = self.placement_plan(component_replacements=components,
                                         via_center_replacement=via_center_replacement)
        return components, updates

    def define_via_center(self, position):
        """Set independent world X/Y centers and regenerate in one transaction."""
        from shapeforge.technology import is_via_stack
        from shapeforge.project_ports import validate_position
        instance, recipe = self.selected_primitive_instance()
        if instance.property("shapeforge.array_member") is not None:
            raise ValueError("Array repetitions inherit position. Define the base center or dissolve the array.")
        if not is_via_stack(recipe):
            raise ValueError("Select a registered via_stack instance.")
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        validate_position(position, self.parameters())
        position = dict(position) if position is not None else None
        if position is not None and not any(f"{axis}_center" in position for axis in ("x", "y")):
            position = None
        identity = instance.property("shapeforge.instance_id")
        serialized = None if position is None else json.dumps(position, allow_nan=False)
        components, updates = self.regeneration_plan(via_center_replacement=(identity, position))
        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: define via center")
        try:
            self.apply_regeneration_plan(components, updates)
            instance = next(i for i in self.cell.each_inst() if i.property("shapeforge.instance_id") == identity)
            if serialized is None:
                instance.delete_property("shapeforge.via_center")
            else:
                instance.set_property("shapeforge.via_center", serialized)
        finally:
            view.commit()
        view.add_missing_layers()

    def apply_regeneration_plan(self, components, updates):
        for object_id, generated in components.items():
            current = next(i for i in self.cell.each_inst() if i.property("shapeforge.instance_id") == object_id)
            if current.property("shapeforge.array_member") is not None:
                continue  # Reuse the updated base cell when applying repetitions.
            component_id = str(uuid4())
            target = self.layout.create_cell(f"SF_{generated.recipe.get('primitive', generated.recipe.get('function', generated.recipe.get('generator', 'component')))}_{component_id[:8]}")
            target.copy_tree(generated.component.kdb_cell)
            target.set_property("shapeforge.component_id", component_id)
            target.set_property("shapeforge.component_recipe", json.dumps(generated.recipe, allow_nan=False))
            instance = next(i for i in self.cell.each_inst() if i.property("shapeforge.instance_id") == object_id)
            instance.cell_index = target.cell_index()
            instance.set_property("shapeforge.component_id", component_id)
            self.components[component_id] = generated.component
            self.component_recipes[component_id] = generated.recipe
        self.apply_placement_updates(updates)

    def selection_info(self):
        self.validate()

        view = self.cellview.view()

        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
        ]

        if not selection:
            return "No shapes or instances selected in the session layout."

        lines = [f"Selected objects: {len(selection)}"]

        for index, obj in enumerate(selection, start=1):
            if obj.is_cell_inst():
                instance = obj.inst()
                component = self.layout.cell(instance.cell_index)

                lines.append(
                    f"{index}. Instance of cell: {component.name}"
                )
                instance_id = instance.property(
                    "shapeforge.instance_id"
                )

                lines.append(
                    f"   Instance ID: {instance_id or 'Not assigned'}"
                )

                # Limitar esta primera versión a instancias individuales
                # directamente seleccionadas en la celda de trabajo.
                if (
                    obj.top != self.cell.cell_index()
                    or len(obj.path) != 1
                ):
                    lines.append(
                        "   Select a direct instance in the working cell "
                        "to inspect transformed ports."
                    )
                    continue

                if instance.is_regular_array():
                    lines.append(
                        "   Array port inspection is not supported yet."
                    )
                    continue

                component_id = self.component_id_for_instance(instance)
                source = self.components.get(component_id)

                if source is None:
                    lines.append(
                        "   No linked gdsfactory component."
                    )
                    continue

                raw = instance.property("shapeforge.geometry_parameters")
                if raw is not None:
                    recipe = self.component_recipes.get(component_id, {})
                    for field, definition in json.loads(raw)["values"].items():
                        effective = recipe.get("geometry", recipe.get("arguments", {})).get(field)
                        si_length = recipe.get("provider") == "shapeic-cellkit" and field.endswith("_m")
                        tech_length = recipe.get("provider") == "shapeic-cellkit-technology" and field in ("length", "wf")
                        unit = " µm" if si_length or tech_length else ""
                        if effective is not None and si_length:
                            effective *= 1e6
                        if isinstance(definition, dict):
                            definition = definition.get("expression", definition)
                        lines.append(f"   {field}: {definition}; rendered={effective}{unit}")

                for port in self.transformed_ports(instance, source):
                    local_x, local_y = port["local_center"]
                    x, y = port["center"]

                    lines.append(
                        f"   Port {port['name']}:\n"
                        f"      local=({local_x:.3f}, {local_y:.3f}) µm\n"
                        f"      working cell=({x:.3f}, {y:.3f}) µm\n"
                        f"      orientation={port['orientation']:.1f}°\n"
                        f"      width={port['width']:.3f} µm"
                    )            
            else:
                shape = obj.shape
                layer = self.layout.get_info(obj.layer)

                if shape.is_box():
                    kind = "Box"
                elif shape.is_polygon():
                    kind = "Polygon"
                elif shape.is_path():
                    kind = "Path"
                elif shape.is_text():
                    kind = "Text"
                else:
                    kind = "Shape"

                lines.append(
                    f"{index}. {kind} on layer "
                    f"{layer.layer}/{layer.datatype}"
                )

        return "\n".join(lines)

    @property
    def grid_um(self):
        return self.grid_dbu * self.layout.dbu

    def um_to_dbu(self, value_um):
        """Convert micrometers to the nearest integer DBU."""
        if not math.isfinite(value_um):
            raise ValueError("The coordinate must be finite.")

        return round(value_um / self.layout.dbu)

    def dbu_to_um(self, value_dbu):
        """Convert DBU to micrometers."""
        return value_dbu * self.layout.dbu

    def snap_dbu(self, value_dbu):
        """Snap to the nearest grid point, measured from the origin."""
        return round(value_dbu / self.grid_dbu) * self.grid_dbu

    def snap_um_to_dbu(self, value_um):
        """Convert micrometers directly to a grid-aligned DBU value."""
        if not math.isfinite(value_um):
            raise ValueError("The coordinate must be finite.")

        position = value_um / self.grid_um
        # Transformed ports can land exactly halfway between grid points.
        # Floating point cancellation must not flip the chosen point after
        # another regeneration (or after reopening the same recipe).
        half = round(position * 2) / 2
        if math.isclose(position, half, rel_tol=0, abs_tol=1e-9):
            position = half
        return round(position) * self.grid_dbu

    def transformed_ports(self, instance, component, transform=None):
        """Return port coordinates in the instance's parent cell."""
        if transform is None:
            transform = instance.dcplx_trans
        result = []

        for port in component.ports:
            local_x, local_y = port.center

            center = transform * pya.DPoint(
                float(local_x),
                float(local_y),
            )

            # Transformar la dirección como un vector.
            # Restar el origen transformado elimina la traslación.
            angle = math.radians(float(port.orientation))

            origin = transform * pya.DPoint(0, 0)
            direction = transform * pya.DPoint(
                math.cos(angle),
                math.sin(angle),
            )

            orientation = math.degrees(
                math.atan2(
                    direction.y - origin.y,
                    direction.x - origin.x,
                )
            ) % 360

            result.append({
                "name": port.name,
                "local_center": (float(local_x), float(local_y)),
                "center": (center.x, center.y),
                "orientation": orientation,
                "width": float(port.width) * abs(transform.mag),
            })

        return result

    def hide_ports(self):
        for marker in self.port_markers:
            if not marker._destroyed():
                marker._destroy()

        self.port_markers.clear()

    def show_ports(self):
        self.validate()
        self.hide_ports()

        view = self.cellview.view()

        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
            and obj.is_cell_inst()
        ]

        if not selection:
            raise RuntimeError("Select an instance in the working cell.")

        # Validar todo antes de crear los marcadores.
        ports_to_show = []

        for obj in selection:
            if (
                obj.top != self.cell.cell_index()
                or len(obj.path) != 1
            ):
                raise RuntimeError(
                    "Select direct instances in the working cell."
                )

            instance = obj.inst()

            if instance.is_regular_array():
                raise RuntimeError("Instance arrays are not supported yet.")

            cell = self.layout.cell(instance.cell_index)
            component_id = self.component_id_for_instance(instance)
            component = self.components.get(component_id)

            if component is None:
                raise RuntimeError(
                    f"No linked gdsfactory component for {cell.name}."
                )

            ports_to_show.extend(
                self.transformed_ports(instance, component)
            )

        try:
            for port in ports_to_show:
                x, y = port["center"]
                angle = math.radians(port["orientation"])

                # Tamaños de visualización, expresados en µm.
                radius = 0.10
                direction_length = 0.50

                center = pya.Marker(view)
                center.color = 0x00FFFF
                center.line_width = 2
                center.dither_pattern = -1
                center.set(
                    pya.DBox(
                        x - radius,
                        y - radius,
                        x + radius,
                        y + radius,
                    )
                )
                self.port_markers.append(center)

                direction = pya.Marker(view)
                direction.color = 0x00FFFF
                direction.line_width = 2
                direction.set(
                    pya.DEdge(
                        x,
                        y,
                        x + direction_length * math.cos(angle),
                        y + direction_length * math.sin(angle),
                    )
                )
                self.port_markers.append(direction)

                label = pya.Marker(view)
                label.color = 0xFFFF00
                label.halo = 1
                label.set(
                    pya.DText(
                        port["name"],
                        x + 0.15,
                        y + 0.15,
                    )
                )
                self.port_markers.append(label)
        except Exception:
            self.hide_ports()
            raise
    def normalize_instance_ids(self):
        self.validate()

        # Materializar antes de cambiar propiedades:
        # set_property puede invalidar el iterador de KLayout.
        instances = list(self.cell.each_inst())

        managed = []

        for instance in instances:
            if instance.is_regular_array():
                continue

            component = self.layout.cell(instance.cell_index)
            component_id = self.component_id_for_instance(instance)

            instance_id = instance.property(
                "shapeforge.instance_id"
            )

            if component_id in self.components or instance_id:
                managed.append(instance)

        groups = {}
        protected_ids = self.measurement_reference_ids()

        for instance in managed:
            from shapeforge.arrays import protected_ids as array_protected_ids
            protected_ids.update(array_protected_ids(instance))
            instance_id = instance.property("shapeforge.instance_id")

            if instance_id:
                groups.setdefault(instance_id, []).append(instance)

            raw = instance.property("shapeforge.placement")

            if raw is not None:
                relation = json.loads(raw)

                # Proteger tanto el destino como la referencia.
                if instance_id:
                    protected_ids.add(instance_id)

                protected_ids.add(relation["reference_id"])

        for shape, definition in self.parametric_boxes():
            from shapeforge.arrays import protected_ids as array_protected_ids
            protected_ids.update(array_protected_ids(shape))
            protected_ids.update(ref["reference_id"]
                                 for ref in self.box_position_references(definition))
            raw = shape.property("shapeforge.placement")
            if raw is not None:
                protected_ids.add(json.loads(raw)["reference_id"])

        for instance_id, group in groups.items():
            if len(group) > 1 and instance_id in protected_ids:
                raise RuntimeError(
                    "A duplicated ID participates in placement or geometry references.\n"
                    "Select the instance that should keep its identity "
                    "and run Keep Selected Instance ID."
                )

        # Reservar todos los IDs existentes.
        reserved = {
            instance.property("shapeforge.instance_id")
            for instance in managed
        }
        reserved.discard(None)
        reserved.discard("")
        reserved.update(protected_ids)
        for layer in self.layout.layer_indexes():
            reserved.update(shape.property("shapeforge.object_id") for shape in self.cell.shapes(layer).each())

        seen = set()
        updates = []

        for instance in managed:
            instance_id = instance.property(
                "shapeforge.instance_id"
            )

            if not instance_id or instance_id in seen:
                new_id = str(uuid4())

                while new_id in reserved:
                    new_id = str(uuid4())

                reserved.add(new_id)
                updates.append((instance, new_id))
            else:
                seen.add(instance_id)

        if not updates:
            return 0

        view = self.cellview.view()
        view.cancel()
        view.transaction("ShapeForge: normalize instance IDs")

        try:
            for instance, new_id in updates:
                instance.set_property(
                    "shapeforge.instance_id",
                    new_id,
                )
        finally:
            view.commit()

        return len(updates)

    def component_id_for_instance(self, instance):
        component_id = instance.property(
            "shapeforge.component_id"
        )

        if component_id is None:
            # Compatibilidad con las inserciones anteriores.
            cell = self.layout.cell(instance.cell_index)
            component_id = cell.property(
                "shapeforge.component_id"
            )

        return component_id

    def placement_candidates(self, allow_boxes=False):
        self.validate()
        selection = [obj for obj in self.cellview.view().object_selection
                     if obj.cv_index == self.cellview.index()]
        if len(selection) != 2:
            raise RuntimeError("Select exactly two placement objects.")
        objects = self.placement_objects()
        candidates = [self.selected_placement_object(obj, objects)
                      for obj in selection]
        if candidates[0]["id"] == candidates[1]["id"]:
            raise RuntimeError("Select two different objects.")
        if not allow_boxes:
            for item in candidates:
                if item["type"] != "instance" or item["component"] is None:
                    raise RuntimeError("Port operations require two linked instances.")
                if not len(item["component"].ports):
                    raise RuntimeError("Both components must have ports.")
        return candidates

    def place_by_ports(self, a, port_a, b, port_b, dx, dy, kind="ports", direction=None, alignment=None):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        if a["id"] == b["id"]:
            raise RuntimeError("A and B must be different instances.")

        relation = {
            "version": 1,
            "kind": kind,
            "reference_id": a["id"],
            "reference_port": port_a,
            "target_port": port_b,
            "dx_um": dx,
            "dy_um": dy,
        }

        if kind == "edges":
            relation["direction"] = direction
            relation["alignment"] = alignment

        serialized = json.dumps(relation, allow_nan=False)

        # Validar y calcular con la relación propuesta,
        # sin guardarla todavía.
        components = {}
        updates, offsets = self.placement_plan(
            replacement=(b["id"], relation), component_replacements=components
        )

        instances = self.placement_instances()
        target = instances[b["id"]]["object"]

        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: define placement")

        try:
            self.apply_regeneration_plan(components, updates)
            target = self.placement_objects(raw=True)[b["id"]]["object"]

            target.set_property(
                "shapeforge.placement",
                serialized,
            )
        finally:
            view.commit()

        view.zoom_fit()
        return offsets[b["id"]]

    def placement_transform(
        self,
        a,
        port_a,
        b,
        port_b,
        dx,
        dy,
        transform_a=None,
        transform_b=None,
    ):
        dx = self.resolve_offset(dx)
        dy = self.resolve_offset(dy)

        if transform_a is None:
            transform_a = self.object_transform(a)

        if transform_b is None:
            transform_b = self.object_transform(b)

        ports_a = {
            port["name"]: port
            for port in self.transformed_ports(
                a["instance"], a["component"], transform_a
            )
        }
        ports_b = {
            port["name"]: port
            for port in self.transformed_ports(
                b["instance"], b["component"], transform_b
            )
        }

        if port_a not in ports_a:
            raise RuntimeError(f"Placement reference {a['id']} has no port '{port_a}'.")
        if port_b not in ports_b:
            raise RuntimeError(f"Placement target {b['id']} has no port '{port_b}'.")

        ax, ay = ports_a[port_a]["center"]
        bx, by = ports_b[port_b]["center"]

        requested_x = transform_b.disp.x + ax + dx - bx
        requested_y = transform_b.disp.y + ay + dy - by

        x = self.dbu_to_um(self.snap_um_to_dbu(requested_x))
        y = self.dbu_to_um(self.snap_um_to_dbu(requested_y))

        new_transform = pya.DCplxTrans(
            transform_b.mag,
            transform_b.angle,
            transform_b.is_mirror(),
            x,
            y,
        )

        actual_dx = bx + x - transform_b.disp.x - ax
        actual_dy = by + y - transform_b.disp.y - ay

        return new_transform, actual_dx, actual_dy

    def placement_instances(self):
        """Compatibility alias: return all managed placement objects."""
        return self.placement_objects()

    def regenerate_placement(self):
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        components, updates = self.regeneration_plan()
        if not updates and not components:
            return 0
        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: regenerate dimensions and placement")
        try:
            self.apply_regeneration_plan(components, updates)
        finally:
            view.commit()
        view.add_missing_layers()
        view.zoom_fit()
        return len({item["id"] for item, _ in updates} | set(components))

    def keep_selected_instance_id(self):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        view = self.cellview.view()
        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
        ]

        if len(selection) != 1:
            raise RuntimeError(
                "Select exactly one instance to keep its identity."
            )

        obj = selection[0]

        if (
            not obj.is_cell_inst()
            or obj.top != self.cell.cell_index()
            or len(obj.path) != 1
        ):
            raise RuntimeError(
                "Select a direct instance in the working cell."
            )

        keeper = obj.inst()

        if keeper.size() != 1:
            raise RuntimeError("Instance arrays are not supported.")

        instance_id = keeper.property("shapeforge.instance_id")

        if not instance_id:
            raise RuntimeError(
                "The selected instance has no ShapeForge ID."
            )

        # Terminar la iteración antes de modificar propiedades.
        instances = list(self.cell.each_inst())

        duplicates = [
            instance
            for instance in instances
            if instance.property("shapeforge.instance_id") == instance_id
            and instance != keeper
        ]

        if not duplicates:
            return 0

        if any(instance.size() != 1 for instance in duplicates):
            raise RuntimeError("Instance arrays are not supported.")

        reserved = {
            instance.property("shapeforge.instance_id")
            for instance in instances
        }
        reserved.update(self.measurement_reference_ids())
        from shapeforge.arrays import protected_ids as array_protected_ids
        for instance in instances:
            reserved.update(array_protected_ids(instance))
        for layer in self.layout.layer_indexes():
            for shape in self.cell.shapes(layer).each():
                reserved.add(shape.property("shapeforge.object_id"))
                reserved.update(array_protected_ids(shape))

        updates = []

        for instance in duplicates:
            new_id = str(uuid4())

            while new_id in reserved:
                new_id = str(uuid4())

            reserved.add(new_id)
            updates.append((instance, new_id))

        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: resolve duplicated instance ID")

        try:
            for instance, new_id in updates:
                instance.set_property(
                    "shapeforge.instance_id",
                    new_id,
                )

                # La copia queda libre; no hereda la relación del destino.
                instance.delete_property("shapeforge.placement")
                instance.delete_property("shapeforge.array")
                instance.delete_property("shapeforge.array_member")
        finally:
            view.commit()

        return len(updates)

    def measurement_reference_ids(self):
        from shapeforge.parameters import is_measure
        return {value["reference_id"] for value in self.parameter_definitions().values() if is_measure(value)}

    def has_measurements(self):
        from shapeforge.parameters import is_measure
        return any(is_measure(v) for v in self.parameter_definitions().values())

    def current_measurement(self, definition):
        """Read committed native geometry, without generating or moving anything."""
        from shapeforge.parameters import validate_measure
        from shapeforge.arrays import locate
        validate_measure(definition)
        obj = locate(self, definition["reference_id"])
        instance = hasattr(obj, "cell_index")
        if instance and (obj.is_regular_array() or obj.size() != 1):
            raise ValueError("Measurements require an individual registered instance.")
        if not instance and (not obj.is_box() or obj.property("shapeforge.box") is None):
            raise ValueError("Measurements require a parametric box.")
        quantity = definition["quantity"]
        if quantity == "port_width":
            if not instance:
                raise ValueError("Port width requires a linked instance.")
            return self.resolve_dimension({"kind": "port_width", "reference_id": definition["reference_id"], "port": definition["port"]})
        bounds = obj.dbbox() if instance else obj.box.to_dtype(self.layout.dbu)
        value = bounds.width() if quantity == "width" else bounds.height()
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Measured dimension must be positive and finite.")
        return value

    def parameters(self):
        self.validate()
        if self._evaluation is not None:
            return self._evaluation.parameters
        from shapeforge.parameters import ParameterValues
        return ParameterValues(self.parameter_definitions(), measurement=self.current_measurement)

    def parameter_definitions(self):
        self.validate()

        raw = self.cell.property("shapeforge.parameters")
        return {} if raw is None else json.loads(raw)

    def public_parameters(self):
        from shapeforge.project_links import PUBLIC_PROPERTY, validate_public
        raw = self.cell.property(PUBLIC_PROPERTY)
        return validate_public(json.loads(raw) if raw else [], self.parameters())

    def insert_project(self, source, overrides):
        from shapeforge.project_links import generate_project, project_scope
        with project_scope(self.project_path):
            generated = generate_project(source, overrides, self.pdk, self.parameters())
        return self.insert_generated(generated, overrides)

    def set_parameter(self, name, value_um, external=None):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        name = name.strip()

        parameters = self.parameter_definitions()
        parameters[name] = parameter_definition(value_um)
        from shapeforge.parameters import derived_parameters
        derived = derived_parameters(parameters)
        if derived:
            from shapeforge.evaluation import Evaluation
            Evaluation(self, definitions=parameters).plan()
        else:
            resolve_parameters(parameters)
        serialized = json.dumps(parameters, allow_nan=False)
        public = self.public_parameters()
        if external is True and name not in public:
            public.append(name)
        elif external is False and name in public:
            public.remove(name)
        from shapeforge.project_links import validate_public
        validate_public(public, parameters)

        view = self.cellview.view()
        view.cancel()
        view.transaction(f"ShapeForge: set parameter {name}")

        try:
            self.cell.set_property(
                "shapeforge.parameters",
                serialized,
            )
            self.cell.set_property("shapeforge.public_parameters", json.dumps(public))
        finally:
            view.commit()

    def resolve_offset(self, value):
        """Resolve a numeric value or safe arithmetic expression, in µm."""
        return resolve_value(value, self.parameters())

    def resolve_dimension(self, value, component_replacements=None, magnification=1):
        from shapeforge.dimensions import validate_port_width, positive_width
        if not isinstance(value, dict):
            return self.resolve_offset(value)
        validate_port_width(value)
        if self._evaluation is not None:
            return self._evaluation.measure({"version": 1, "kind": "geometry_measure",
                "reference_id": value["reference_id"], "quantity": "port_width", "port": value["port"]}) / positive_width(abs(magnification))
        matches = [i for i in self.cell.each_inst() if i.property("shapeforge.instance_id") == value["reference_id"]]
        if len(matches) != 1:
            raise ValueError(f"Missing or duplicate port width reference: {value['reference_id']}.")
        instance = matches[0]
        if instance.size() != 1 or instance.is_regular_array():
            raise ValueError("Port width references do not support instance arrays.")
        width_magnification = abs(instance.dcplx_trans.mag)
        member = instance.property("shapeforge.array_member")
        if member is not None and component_replacements is not None:
            from shapeforge.arrays import locate
            owner = locate(self, json.loads(member)["owner_id"])
            width_magnification = abs(owner.dcplx_trans.mag)
        generated = (component_replacements or {}).get(value["reference_id"])
        component = generated.component if generated is not None else self.components.get(self.component_id_for_instance(instance))
        if component is None:
            raise ValueError("Port width reference requires a linked component.")
        ports = {port.name: port for port in component.ports}
        if value["port"] not in ports:
            raise ValueError(f"Port width reference {value['reference_id']} has no port '{value['port']}'.")
        return positive_width(float(ports[value["port"]].width) * width_magnification
                              / positive_width(abs(magnification)))

    def resolve_via_inputs(self, values, magnification=1, component_replacements=None):
        from shapeforge.dimensions import is_port_width
        result = dict(values)
        for name, value in result.items():
            if is_port_width(value):
                if name not in ("width", "height"):
                    raise ValueError("Port width bindings require a via width or height.")
                result[name] = self.resolve_dimension(value, component_replacements, magnification)
        return result

    def resolved_parameters(self):
        return resolve_parameters(self.parameters())

    def parameter_usages(self, name):
        """Find active references before removing a parameter."""
        usages = []
        for other, value in self.parameters().items():
            if other != name and name in references(value):
                usages.append(f"parameter {other}")
        objects = list(self.cell.each_inst())
        for layer in self.layout.layer_indexes():
            objects.extend(self.cell.shapes(layer).each())
        for obj in objects:
            identity = obj.property("shapeforge.instance_id") or obj.property("shapeforge.object_id") or "object"
            raw_array = obj.property("shapeforge.array")
            if raw_array is not None:
                definition = json.loads(raw_array)
                for key in ("columns", "rows", "dx_um", "dy_um"):
                    if name in references(definition[key]):
                        usages.append(f"{identity}: array {key}")
            raw = obj.property("shapeforge.geometry_parameters")
            if raw is not None:
                for field, value in input_expressions(json.loads(raw)):
                    if name in references(value):
                        usages.append(f"{identity}: primitive {field}")
            for property_key, label in (("shapeforge.port_position", "project port"),
                                        ("shapeforge.via_center", "via stack")):
                raw = obj.property(property_key)
                if raw is not None:
                    for axis in ("x", "y"):
                        value = json.loads(raw).get(f"{axis}_center")
                        if value is not None:
                            expression = value.get("offset_um", 0) if isinstance(value, dict) else value
                            if name in references(expression):
                                usages.append(f"{identity}: {label} {axis.upper()} center")
            raw = obj.property("shapeforge.placement")
            if raw is not None:
                relation = json.loads(raw)
                for key in ("dx_um", "dy_um"):
                    if name in references(relation[key]):
                        usages.append(f"{identity}: placement {key}")
            raw = obj.property("shapeforge.box")
            if raw is not None:
                definition = json.loads(raw)
                for axis, dimension in (("x", "width_um"), ("y", "height_um")):
                    if (not definition.get(f"{axis}_extents") and not isinstance(definition[dimension], dict)
                            and name in references(definition[dimension])):
                        usages.append(f"{identity}: {dimension}")
                    center = definition.get(f"{axis}_center")
                    if center is not None and not isinstance(center, dict) and name in references(center):
                        usages.append(f"{identity}: {axis} center")
                for ref in self.box_position_references(definition):
                    if name in references(ref.get("offset_um", 0)):
                        usages.append(f"{identity}: box reference offset")
        return sorted(set(usages))

    def delete_parameter(self, name):
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        parameters = self.parameters()
        if name not in parameters:
            raise ValueError(f"Unknown parameter: {name}")
        usages = self.parameter_usages(name)
        if usages:
            raise ValueError(f"Cannot delete parameter {name}; used by:\n" + "\n".join(usages))
        public = [n for n in self.public_parameters() if n != name]
        del parameters[name]
        resolve_parameters(parameters)
        view = self.cellview.view()
        view.cancel()
        view.transaction(f"ShapeForge: delete parameter {name}")
        try:
            self.cell.set_property("shapeforge.parameters", json.dumps(parameters, allow_nan=False))
            self.cell.set_property("shapeforge.public_parameters", json.dumps(public))
        finally:
            view.commit()

    def parse_offset(self, text):
        """Accept a number or a safe arithmetic expression."""
        text = text.strip()

        try:
            value = float(text)
        except ValueError:
            value = text

        # Comprobar que pueda resolverse antes de guardar la relación.
        self.resolve_offset(value)
        return value

    def selected_placement_target(self):
        self.validate()
        selection = [obj for obj in self.cellview.view().object_selection
                     if obj.cv_index == self.cellview.index()]
        if len(selection) != 1:
            raise RuntimeError("Select exactly one placement target.")
        item = self.selected_placement_object(selection[0], self.placement_objects())
        raw = item["object"].property("shapeforge.placement")
        if raw is None:
            raise RuntimeError("The selected object has no placement relation.")
        relation = json.loads(raw)
        if relation.get("version") != 1:
            raise RuntimeError("Unsupported placement version.")
        return item, relation

    def placement_info(self):
        target, relation = self.selected_placement_target()
        instances = self.placement_instances()

        reference_id = relation["reference_id"]
        reference = instances.get(reference_id)

        if reference is None:
            reference_name = "Missing instance"
        else:
            reference_name = reference["name"]

        target_name = target["name"]

        def describe(value):
            try:
                resolved = self.resolve_offset(value)
            except ValueError as error:
                return f"{value} — {error}"

            if isinstance(value, str):
                return f"{value} = {resolved:g} µm"

            return f"{resolved:g} µm"

        kind = relation.get("kind", "ports")
        is_edges = kind in ("right_of", "edges")

        reference_port = (
            "Bounding box" if is_edges
            else relation["reference_port"]
        )
        target_port = (
            "Bounding box" if is_edges
            else relation["target_port"]
        )

        x_label = "Edge gap" if is_edges else "dx"
        y_label = "Perpendicular offset" if is_edges else "dy"

        description = kind

        if is_edges:
            direction = relation.get("direction", "right")
            alignment = relation.get("alignment", "center")
            description = f"{kind}: {direction}, align {alignment}"

        return (
            f"Type: {description}\n\n"
            f"A: {reference_name}\n"
            f"ID: {reference_id}\n"
            f"Reference: {reference_port}\n\n"
            f"B: {target_name}\n"
            f"ID: {target['id']}\n"
            f"Target: {target_port}\n\n"
            f"{x_label}: {describe(relation['dx_um'])}\n"
            f"{y_label}: {describe(relation['dy_um'])}"
        )

    def edit_placement(self, dx, dy):
        target, relation = self.selected_placement_target()
        instances = self.placement_instances()

        reference = instances.get(relation["reference_id"])

        if reference is None:
            raise RuntimeError("The reference instance no longer exists.")

        if relation.get("kind", "ports") == "ports" and (
            reference["component"] is None or target["component"] is None
        ):
            raise RuntimeError("A linked component is unavailable.")

        # Reutilizar la validación, el cálculo y la transacción existentes.
        return self.place_by_ports(
            reference,
            relation["reference_port"],
            target,
            relation["target_port"],
            dx,
            dy,
            kind=relation.get("kind", "ports"),
            direction=relation.get("direction"),
            alignment=relation.get("alignment"),
        )

    def remove_placement(self):
        target, _ = self.selected_placement_target()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        view = self.cellview.view()
        view.cancel()
        view.transaction("ShapeForge: remove placement")
        try:
            target["object"].delete_property("shapeforge.placement")
        finally:
            view.commit()

    def placement_plan(self, replacement=None, box_replacement=None, component_replacements=None,
                       via_center_replacement=None, array_replacement=None):
        """Plan dimensions, referenced extents and placement without mutation."""
        if self.has_measurements():
            from shapeforge.evaluation import Evaluation
            planner = Evaluation(self, replacement=replacement, box_replacement=box_replacement,
                component_replacements=component_replacements, via_center_replacement=via_center_replacement,
                array_replacement=array_replacement)
            components, updates = planner.plan()
            if component_replacements is not None:
                component_replacements.clear()
                component_replacements.update(components)
            return updates, planner.offsets
        objects = self.placement_objects(box_replacement=box_replacement, component_replacements=component_replacements or {})
        if via_center_replacement is not None:
            identity, position = via_center_replacement
            if identity not in objects:
                raise RuntimeError("The target via stack no longer exists.")
            objects[identity]["via_center"] = position
        for object_id, generated in (component_replacements or {}).items():
            objects[object_id]["component"] = generated.component
            objects[object_id]["local_box"] = generated.component.kdb_cell.dbbox()
        from shapeforge.arrays import prepare, member_transform
        removals = prepare(self, objects, array_replacement)
        relations = {}
        for object_id, item in objects.items():
            raw = item["object"].property("shapeforge.placement")
            if raw is not None:
                relation = json.loads(raw)
                if relation.get("version") != 1:
                    raise RuntimeError("Unsupported placement version.")
                relations[object_id] = relation
        if replacement is not None:
            target_id, relation = replacement
            if target_id not in objects:
                raise RuntimeError("The target object no longer exists.")
            relations[target_id] = relation
        if box_replacement is not None and box_replacement[0] not in objects:
            raise RuntimeError("The target box no longer exists.")

        dependencies = {key: set() for key in objects}
        for identity, item in objects.items():
            if item.get("array_member") is not None:
                if identity in relations:
                    raise ValueError("Array controls this repetition's position. Edit the base or dissolve the array.")
                dependencies[identity].add(item["array_member"]["owner_id"])
        for target_id, item in objects.items():
            if item["type"] == "box" and item.get("array_member") is None:
                definition = item["definition"]
                position_refs = self.box_position_references(definition)
                if (position_refs or any(f"{axis}_center" in definition for axis in ("x", "y"))) and target_id in relations:
                    raise RuntimeError(
                        "Box extents or center control its position. Remove its "
                        "placement relation before defining extents or center."
                    )
                dependencies[target_id].update(
                    ref["reference_id"] for ref in position_refs)
        for target_id, item in objects.items():
            position = item.get("via_center") or item.get("port_position")
            if position and any(f"{axis}_center" in position for axis in ("x", "y")):
                if target_id in relations:
                    raise ValueError("Center coordinates control this object's position. Remove placement or free both coordinates first.")
                dependencies[target_id].update(ref["reference_id"] for ref in self.box_position_references(position))
        for target_id, relation in relations.items():
            reference_id = relation["reference_id"]
            dependencies[target_id].add(reference_id)
            if relation.get("kind", "ports") == "ports":
                for object_id in (reference_id, target_id):
                    if object_id not in objects:
                        raise RuntimeError(f"Missing reference object: {object_id}")
                    item = objects[object_id]
                    if item["type"] != "instance" or item["component"] is None:
                        raise RuntimeError("Port placement requires linked instances.")
        for refs in dependencies.values():
            for reference_id in refs:
                if reference_id not in objects:
                    raise RuntimeError(f"Missing reference object: {reference_id}")

        transforms = {key: self.object_transform(item)
                      for key, item in objects.items()}
        visiting, completed, path, order, offsets = set(), set(), [], [], {}
        def visit(object_id):
            if object_id in completed:
                return
            if object_id in visiting:
                cycle = path[path.index(object_id):] + [object_id]
                raise RuntimeError("Placement cycle detected:\n" + " -> ".join(cycle))
            visiting.add(object_id)
            path.append(object_id)
            for reference_id in sorted(dependencies[object_id]):
                visit(reference_id)
            item = objects[object_id]
            if item.get("array_member") is not None:
                owner = item["array_member"]["owner_id"]
                if item["type"] == "box":
                    item["local_box"] = objects[owner]["local_box"]
                    item["definition"] = {"version": 1, "width_um": item["local_box"].width(), "height_um": item["local_box"].height()}
                transforms[object_id] = member_transform(self, item, objects[owner], transforms[owner])
            if item["type"] == "box" and item.get("array_member") is None:
                transforms[object_id] = self.box_extent_transform(
                    item, objects, transforms)
            position = item.get("via_center") or item.get("port_position")
            governed = position and any(f"{axis}_center" in position for axis in ("x", "y"))
            if governed:
                transform = transforms[object_id]
                coordinates = [transform.disp.x, transform.disp.y]
                # Via centers refer to the visible bounding box, independent of
                # the generator origin, rotation, reflection and magnification.
                center_offset = [0, 0]
                if item.get("via_center"):
                    linear = pya.DCplxTrans(transform.mag, transform.angle, transform.is_mirror(), 0, 0)
                    bounds = self.placement_bbox(item, linear)
                    center_offset = [(bounds.left + bounds.right) / 2, (bounds.bottom + bounds.top) / 2]
                for index, axis in enumerate(("x", "y")):
                    if f"{axis}_center" in position:
                        value = position[f"{axis}_center"]
                        coordinate = (self.extent_coordinate(value, axis, objects, transforms)
                                      if isinstance(value, dict) else self.resolve_offset(value))
                        coordinates[index] = self.dbu_to_um(self.snap_um_to_dbu(coordinate - center_offset[index]))
                transforms[object_id] = pya.DCplxTrans(transform.mag, transform.angle, transform.is_mirror(), *coordinates)
            relation = relations.get(object_id)
            if relation is not None:
                reference_id = relation["reference_id"]
                transform, dx, dy = self.relation_transform(
                    objects[reference_id], item, relation,
                    transform_a=transforms[reference_id],
                    transform_b=transforms[object_id])
                transforms[object_id] = transform
                offsets[object_id] = (dx, dy)
            if item["type"] == "box" or relation is not None or governed or item.get("array") is not None or item.get("array_member") is not None:
                order.append(object_id)
            path.pop()
            visiting.remove(object_id)
            completed.add(object_id)
        for object_id in sorted(objects):
            visit(object_id)
        updates = [(objects[key], transforms[key]) for key in order]
        updates.extend(({"type": "array_remove", "id": identity}, None) for identity in sorted(removals))
        return updates, offsets

    def placement_bbox(self, item, transform):
        if "local_box" in item:
            box = item["local_box"]
        else:
            box = self.layout.cell(item["instance"].cell_index).dbbox()
        if box.empty():
            raise RuntimeError("Cannot place an object with no geometry.")
        return pya.DPolygon(box).transformed(transform).bbox()

    def right_of_transform(
        self, a, b, gap, dy, transform_a=None, transform_b=None
    ):
        return self.edges_transform(
            a,
            b,
            gap,
            dy,
            direction="right",
            alignment="center",
            transform_a=transform_a,
            transform_b=transform_b,
        )

    def relation_transform(
        self, a, b, relation, transform_a=None, transform_b=None
    ):
        kind = relation.get("kind", "ports")

        if kind == "ports":
            return self.placement_transform(
                a,
                relation["reference_port"],
                b,
                relation["target_port"],
                relation["dx_um"],
                relation["dy_um"],
                transform_a=transform_a,
                transform_b=transform_b,
            )

        if kind == "right_of":
            return self.right_of_transform(
                a,
                b,
                relation["dx_um"],
                relation["dy_um"],
                transform_a=transform_a,
                transform_b=transform_b,
            )

        if kind == "edges":
            return self.edges_transform(
                a,
                b,
                relation["dx_um"],
                relation["dy_um"],
                relation["direction"],
                relation["alignment"],
                transform_a=transform_a,
                transform_b=transform_b,
            )

        raise RuntimeError(f"Unknown placement kind: {kind}")

    def place_right_of(self, a, b, gap, dy=0):
        return self.place_by_ports(
            a, None, b, None, gap, dy, kind="right_of"
        )

    def edges_transform(
        self,
        a,
        b,
        gap,
        offset,
        direction,
        alignment,
        transform_a=None,
        transform_b=None,
    ):
        gap = self.resolve_offset(gap)
        offset = self.resolve_offset(offset)

        if gap < 0:
            raise ValueError("The edge gap must be nonnegative.")

        horizontal = direction in ("right", "left")

        if direction not in ("right", "left", "above", "below"):
            raise ValueError("Invalid placement direction.")

        allowed = (
            ("bottom", "center", "top")
            if horizontal
            else ("left", "center", "right")
        )

        if alignment not in allowed:
            raise ValueError("Invalid alignment for this direction.")

        if transform_a is None:
            transform_a = self.object_transform(a)

        if transform_b is None:
            transform_b = self.object_transform(b)

        box_a = self.placement_bbox(a, transform_a)
        box_b = self.placement_bbox(b, transform_b)

        def anchor(box):
            if horizontal:
                return {
                    "bottom": box.bottom,
                    "center": (box.bottom + box.top) / 2,
                    "top": box.top,
                }[alignment]

            return {
                "left": box.left,
                "center": (box.left + box.right) / 2,
                "right": box.right,
            }[alignment]

        anchor_a = anchor(box_a)
        anchor_b = anchor(box_b)

        if direction == "right":
            delta_x = box_a.right + gap - box_b.left
            delta_y = anchor_a + offset - anchor_b
        elif direction == "left":
            delta_x = box_a.left - gap - box_b.right
            delta_y = anchor_a + offset - anchor_b
        elif direction == "above":
            delta_x = anchor_a + offset - anchor_b
            delta_y = box_a.top + gap - box_b.bottom
        else:
            delta_x = anchor_a + offset - anchor_b
            delta_y = box_a.bottom - gap - box_b.top

        requested_x = transform_b.disp.x + delta_x
        requested_y = transform_b.disp.y + delta_y

        def snap_outward(value, positive):
            position = value / self.grid_um
            nearest = round(position)

            if math.isclose(
                position, nearest, rel_tol=0, abs_tol=1e-9
            ):
                index = nearest
            else:
                index = (
                    math.ceil(position)
                    if positive
                    else math.floor(position)
                )

            return index * self.grid_um

        if horizontal:
            x = snap_outward(requested_x, direction == "right")
            y = self.dbu_to_um(self.snap_um_to_dbu(requested_y))
        else:
            x = self.dbu_to_um(self.snap_um_to_dbu(requested_x))
            y = snap_outward(requested_y, direction == "above")

        transform = pya.DCplxTrans(
            transform_b.mag,
            transform_b.angle,
            transform_b.is_mirror(),
            x,
            y,
        )

        moved_x = x - transform_b.disp.x
        moved_y = y - transform_b.disp.y

        if direction == "right":
            actual_gap = box_b.left + moved_x - box_a.right
        elif direction == "left":
            actual_gap = box_a.left - box_b.right - moved_x
        elif direction == "above":
            actual_gap = box_b.bottom + moved_y - box_a.top
        else:
            actual_gap = box_a.bottom - box_b.top - moved_y

        actual_offset = (
            anchor_b
            + (moved_y if horizontal else moved_x)
            - anchor_a
        )

        return transform, actual_gap, actual_offset


    def place_by_edges(self, a, b, gap, offset, direction, alignment):
        return self.place_by_ports(
            a,
            None,
            b,
            None,
            gap,
            offset,
            kind="edges",
            direction=direction,
            alignment=alignment,
        )

    def route_ports(self, a, port_a, b, port_b, width_um):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        if a["id"] == b["id"]:
            raise RuntimeError("Select two different instances.")

        if not math.isfinite(width_um) or width_um <= 0:
            raise ValueError("Route width must be positive.")

        source_a = a["component"].ports[port_a]
        source_b = b["component"].ports[port_b]

        layer_a = source_a.layer_info
        layer_b = source_b.layer_info

        if (
            layer_a.layer,
            layer_a.datatype,
        ) != (
            layer_b.layer,
            layer_b.datatype,
        ):
            raise RuntimeError(
                "The selected ports are on different layers."
            )

        ports_a = {
            port["name"]: port
            for port in self.transformed_ports(
                a["instance"], a["component"]
            )
        }
        ports_b = {
            port["name"]: port
            for port in self.transformed_ports(
                b["instance"], b["component"]
            )
        }

        ax, ay = ports_a[port_a]["center"]
        bx, by = ports_b[port_b]["center"]

        start = pya.Point(
            self.um_to_dbu(ax),
            self.um_to_dbu(ay),
        )
        end = pya.Point(
            self.um_to_dbu(bx),
            self.um_to_dbu(by),
        )

        if start == end:
            raise RuntimeError("The port centers coincide.")

        width = self.um_to_dbu(width_um)

        if width <= 0:
            raise RuntimeError(
                "The route width is smaller than the layout DBU."
            )

        # Una trayectoria horizontal y luego vertical.
        points = [start]
        corner = pya.Point(end.x, start.y)

        if corner != start and corner != end:
            points.append(corner)

        points.append(end)

        layer = self.layout.layer(
            layer_a.layer,
            layer_a.datatype,
        )

        route_id = str(uuid4())

        route = {
            "version": 1,
            "id": route_id,
            "start_instance_id": a["id"],
            "start_port": port_a,
            "end_instance_id": b["id"],
            "end_port": port_b,
            "width_um": width_um,
            "topology": "horizontal_first",
        }

        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: route ports")

        try:
            shape = self.cell.shapes(layer).insert(
                pya.Path(points, width)
            )
            shape.set_property(
                "shapeforge.route",
                json.dumps(route, allow_nan=False),
            )
        finally:
            view.commit()

        view.add_missing_layers()
        view.zoom_fit()

        return route_id

    def selected_box(self):
        self.validate()

        view = self.cellview.view()
        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
        ]

        if len(selection) != 1:
            raise RuntimeError("Select exactly one box.")

        obj = selection[0]

        if (
            obj.is_cell_inst()
            or obj.top != self.cell.cell_index()
            or len(obj.path) != 0
        ):
            raise RuntimeError(
                "Select a box directly in the working cell."
            )

        shape = obj.shape

        if not shape.is_box():
            raise RuntimeError("The selected shape must be a box.")

        return shape

    def parametric_boxes(self):
        self.validate()

        result = []
        seen = set()

        for layer_index in self.layout.layer_indexes():
            for shape in self.cell.shapes(layer_index).each():
                raw = shape.property("shapeforge.box")

                if raw is None:
                    continue

                if not shape.is_box():
                    raise RuntimeError(
                        "A parametric box was converted to another shape type."
                    )

                box_id = shape.property("shapeforge.object_id")

                if not box_id:
                    raise RuntimeError("A parametric box has no ID.")

                if box_id in seen:
                    raise RuntimeError(
                        "Duplicate box ID. Select the box that must keep its ID "
                        "and run Keep Selected Box ID."
                    )

                seen.add(box_id)
                definition = json.loads(raw)

                if definition.get("version") not in (1, 2, 3):
                    raise RuntimeError("Unsupported box definition version.")

                result.append((shape, definition))

        return result

    def sized_box(self, shape, width, height):
        width_um = self.resolve_dimension(width)
        height_um = self.resolve_dimension(height)

        if width_um <= 0 or height_um <= 0:
            raise ValueError("Box width and height must be positive.")

        width_dbu = self.snap_um_to_dbu(width_um)
        height_dbu = self.snap_um_to_dbu(height_um)

        if min(width_dbu, height_dbu) <= 0:
            raise ValueError("Box dimensions are too small for the grid.")

        current = shape.box

        return pya.Box(
            current.left,
            current.bottom,
            current.left + width_dbu,
            current.bottom + height_dbu,
        )

    def make_box_parametric(self, width, height):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        shape = self.selected_box()
        if shape.property("shapeforge.array_member") is not None:
            raise ValueError("Array boxes inherit size and position. Edit the base or dissolve the array.")
        raw = shape.property("shapeforge.box")
        if raw is not None and (self.box_extent_references(json.loads(raw))
                                or any(f"{axis}_center" in json.loads(raw) for axis in ("x", "y"))):
            raise RuntimeError(
                "This box has parametric extents or center. Use Define Box Extents "
                "to edit its dimensions and position."
            )
        new_box = self.sized_box(shape, width, height)

        definition = {
            "version": 1,
            "width_um": width,
            "height_um": height,
        }
        from shapeforge.dimensions import box_version
        definition["version"] = box_version(definition)
        serialized = json.dumps(definition, allow_nan=False)

        box_id = shape.property("shapeforge.object_id")

        # Una copia puede haber heredado el ID.
        # Aquí conservamos el de la otra figura y reasignamos el seleccionado.
        if box_id:
            matches = 0

            for layer_index in self.layout.layer_indexes():
                for other in self.cell.shapes(layer_index).each():
                    if other.property("shapeforge.object_id") == box_id:
                        matches += 1

            if matches > 1:
                raise RuntimeError(
                    "Duplicate box ID. Select the box that must keep its ID "
                    "and run Keep Selected Box ID."
                )

        if not box_id:
            box_id = str(uuid4())

        if self.has_measurements() and raw is not None:
            self.placement_plan(box_replacement=(box_id, definition))

        view = self.cellview.view()
        view.cancel()
        view.transaction("ShapeForge: define parametric box")

        try:
            shape.box = new_box
            shape.set_property("shapeforge.object_id", box_id)
            shape.set_property("shapeforge.box", serialized)
        finally:
            view.commit()

        return (
            self.dbu_to_um(new_box.width()),
            self.dbu_to_um(new_box.height()),
        )

    def regenerate_boxes(self):
        # Both menu actions use the same complete plan so dependent objects
        # never use stale box dimensions.
        return self.regenerate_placement()

    def placement_objects(self, box_replacement=None, resolve_dimensions=True, component_replacements=None, raw=False):
        """Collect instances and parametric boxes in the working cell."""
        self.validate()
        result = {}
        def add(item):
            if item["id"] in result:
                raise RuntimeError(
                    "Duplicate object ID. Resolve copied IDs before placement."
                )
            result[item["id"]] = item
        for instance in self.cell.each_inst():
            object_id = instance.property("shapeforge.instance_id")
            if not object_id:
                continue
            if instance.size() != 1:
                raise RuntimeError("Placement does not support instance arrays.")
            cell = self.layout.cell(instance.cell_index)
            component_id = self.component_id_for_instance(instance)
            position_raw = instance.property("shapeforge.port_position")
            position = json.loads(position_raw) if position_raw else None
            raw_center = instance.property("shapeforge.via_center")
            via_center = json.loads(raw_center) if raw_center else None
            from shapeforge.project_ports import is_project_port, validate_position
            from shapeforge.technology import is_via_stack
            recipe = self.component_recipes.get(component_id)
            if position is not None and not raw:
                if not is_project_port(recipe):
                    raise ValueError("Only project ports can have a port position definition.")
                validate_position(position, self.parameters())
            if via_center is not None and not raw:
                if not is_via_stack(recipe):
                    raise ValueError("Only via stacks can have a via center definition.")
                validate_position(via_center, self.parameters())
            add({"id": object_id, "type": "instance", "object": instance,
                 "instance": instance, "component": self.components.get(component_id),
                 "port_position": position,
                 "via_center": via_center,
                 "array": json.loads(instance.property("shapeforge.array")) if instance.property("shapeforge.array") else None,
                 "array_member": json.loads(instance.property("shapeforge.array_member")) if instance.property("shapeforge.array_member") else None,
                 "recipe": recipe,
                 "name": cell.name, "label": f"{'Project port' if is_project_port(recipe) else 'Instance'} {cell.name} | {object_id}"})
        for shape, definition in self.parametric_boxes():
            object_id = shape.property("shapeforge.object_id")
            if box_replacement is not None and object_id == box_replacement[0]:
                definition = box_replacement[1]
            if not raw:
                self.validate_box_definition(definition)
            current = shape.box
            layer = next(layer for layer in self.layout.layer_indexes()
                         if any(other == shape for other in self.cell.shapes(layer).each()))
            width = (self.dbu_to_um(current.width()) if definition.get("x_extents")
                     else definition["width_um"])
            height = (self.dbu_to_um(current.height()) if definition.get("y_extents")
                      else definition["height_um"])
            from shapeforge.dimensions import is_port_width
            if raw:
                width, height = self.dbu_to_um(current.width()), self.dbu_to_um(current.height())
            elif resolve_dimensions:
                width = self.resolve_dimension(width, component_replacements)
                height = self.resolve_dimension(height, component_replacements)
            else:
                width = self.dbu_to_um(current.width()) if is_port_width(width) else width
                height = self.dbu_to_um(current.height()) if is_port_width(height) else height
            box = current if raw else self.sized_box(shape, width, height)
            # Geometry is local to the box's lower-left corner. Translation
            # represents that corner in working-cell coordinates.
            local = pya.DBox(0, 0, self.dbu_to_um(box.width()),
                            self.dbu_to_um(box.height()))
            add({"id": object_id, "type": "box", "object": shape,
                 "shape": shape, "component": None, "local_box": local,
                 "definition": definition,
                 "layer": layer,
                 "array": json.loads(shape.property("shapeforge.array")) if shape.property("shapeforge.array") else None,
                 "array_member": json.loads(shape.property("shapeforge.array_member")) if shape.property("shapeforge.array_member") else None,
                 "name": "Parametric box", "label": f"Box | {object_id}"})
        return result

    def object_transform(self, item):
        if item["type"] == "instance":
            return item["instance"].dcplx_trans
        box = item["shape"].box
        return pya.DCplxTrans(1, 0, False,
                             self.dbu_to_um(box.left), self.dbu_to_um(box.bottom))

    def apply_placement_updates(self, updates):
        from shapeforge.arrays import apply_metadata
        apply_metadata(self, updates)
        # No iterators are active while mutating KLayout objects.
        for item, transform in updates:
            if item["type"] == "array_remove":
                continue
            if item["type"] == "instance":
                instance = next(i for i in self.cell.each_inst() if i.property("shapeforge.instance_id") == item["id"])
                instance.dcplx_trans = transform
            else:
                x = self.um_to_dbu(transform.disp.x)
                y = self.um_to_dbu(transform.disp.y)
                width = self.um_to_dbu(item["local_box"].width())
                height = self.um_to_dbu(item["local_box"].height())
                from shapeforge.arrays import locate_box
                shape, _ = locate_box(self, item["id"])
                shape.box = pya.Box(x, y, x + width, y + height)

    def selected_placement_object(self, obj, objects):
        if obj.top != self.cell.cell_index():
            raise RuntimeError("Select objects in the working cell.")
        if obj.is_cell_inst():
            if len(obj.path) != 1:
                raise RuntimeError("Select a direct instance in the working cell.")
            object_id = obj.inst().property("shapeforge.instance_id")
        else:
            if len(obj.path) != 0 or not obj.shape.is_box():
                raise RuntimeError("Select a direct box or instance.")
            object_id = obj.shape.property("shapeforge.object_id")
            if obj.shape.property("shapeforge.box") is None:
                raise RuntimeError("Run Make Box Parametric on this box first.")
        if not object_id or object_id not in objects:
            raise RuntimeError("The selected object has no registered ShapeForge ID.")
        return objects[object_id]

    def keep_selected_box_id(self):
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        keeper = self.selected_box()
        box_id = keeper.property("shapeforge.object_id")
        if not box_id or keeper.property("shapeforge.box") is None:
            raise RuntimeError("Select a parametric box with a ShapeForge ID.")
        duplicates = []
        for layer_index in self.layout.layer_indexes():
            for shape in self.cell.shapes(layer_index).each():
                if shape.property("shapeforge.object_id") == box_id and shape != keeper:
                    duplicates.append(shape)
        if not duplicates:
            return 0
        from shapeforge.arrays import protected_ids
        reserved = self.measurement_reference_ids()
        for layer in self.layout.layer_indexes():
            for shape in self.cell.shapes(layer).each():
                reserved.add(shape.property("shapeforge.object_id"))
                reserved.update(protected_ids(shape))
        for instance in self.cell.each_inst():
            reserved.add(instance.property("shapeforge.instance_id"))
            reserved.update(protected_ids(instance))
        updates = []
        for shape in duplicates:
            identity = str(uuid4())
            while identity in reserved:
                identity = str(uuid4())
            reserved.add(identity)
            updates.append((shape, identity))
        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: resolve duplicated box ID")
        try:
            for shape, new_id in updates:
                shape.set_property("shapeforge.object_id", new_id)
                shape.delete_property("shapeforge.placement")
                shape.delete_property("shapeforge.array")
                shape.delete_property("shapeforge.array_member")
        finally:
            view.commit()
        return len(updates)

    @staticmethod
    def box_extent_references(definition):
        refs = []
        for axis in ("x", "y"):
            extents = definition.get(f"{axis}_extents")
            if extents:
                refs.extend((extents["min"], extents["max"]))
        return refs

    @staticmethod
    def box_position_references(definition):
        refs = Session.box_extent_references(definition)
        refs.extend(definition[f"{axis}_center"] for axis in ("x", "y")
                    if isinstance(definition.get(f"{axis}_center"), dict))
        return refs

    def validate_box_definition(self, definition):
        if definition.get("version") not in (1, 2, 3):
            raise RuntimeError("Unsupported box definition version.")
        for axis, dimension, allowed in (
            ("x", "width_um", ("left", "center", "right", "port")),
            ("y", "height_um", ("bottom", "center", "top", "port")),
        ):
            extents = definition.get(f"{axis}_extents")
            if f"{axis}_center" in definition:
                if definition["version"] not in (2, 3):
                    raise ValueError("Parametric box centers require box definition version 2.")
                if extents is not None:
                    raise ValueError(f"{axis.upper()} center and two referenced extents cannot coexist.")
                center = definition[f"{axis}_center"]
                if isinstance(center, dict):
                    self.validate_box_reference(center, axis, allowed)
                else:
                    self.resolve_offset(center)
            if extents is None:
                if dimension not in definition:
                    raise ValueError(f"Missing {dimension}.")
                if isinstance(definition[dimension], dict):
                    from shapeforge.dimensions import validate_port_width
                    if definition["version"] != 3:
                        raise ValueError("Port width dimensions require box definition version 3.")
                    validate_port_width(definition[dimension])
                elif self.resolve_offset(definition[dimension]) <= 0:
                    raise ValueError("Box dimensions must be positive.")
                continue
            for end in ("min", "max"):
                ref = extents[end]
                self.validate_box_reference(ref, axis, allowed)

    def validate_box_reference(self, reference, axis, allowed):
        if not isinstance(reference.get("reference_id"), str):
            raise ValueError("A box reference needs an object ID.")
        if reference.get("anchor") not in allowed:
            raise ValueError(f"Invalid anchor for the {axis.upper()} axis.")
        if reference["anchor"] == "port" and not reference.get("port"):
            raise ValueError("A port reference needs a port name.")
        self.resolve_offset(reference.get("offset_um", 0))

    def extent_coordinate(self, reference, axis, objects, transforms):
        reference_id = reference["reference_id"]
        item = objects[reference_id]
        transform = transforms[reference_id]
        anchor = reference["anchor"]
        if anchor == "port":
            if item["type"] != "instance" or item["component"] is None:
                raise RuntimeError("A port extent requires a linked instance.")
            ports = {port["name"]: port for port in self.transformed_ports(
                item["instance"], item["component"], transform)}
            port_name = reference["port"]
            if port_name not in ports:
                raise RuntimeError(f"Extent reference {reference_id} has no port '{port_name}'.")
            value = ports[port_name]["center"][0 if axis == "x" else 1]
        else:
            box = self.placement_bbox(item, transform)
            if axis == "x":
                anchors = {"left": box.left, "center": (box.left + box.right) / 2,
                           "right": box.right}
            else:
                anchors = {"bottom": box.bottom, "center": (box.bottom + box.top) / 2,
                           "top": box.top}
            value = anchors[anchor]
        return value + self.resolve_offset(reference.get("offset_um", 0))

    def box_extent_transform(self, item, objects, transforms):
        definition = item["definition"]
        transform = transforms[item["id"]]
        x = self.um_to_dbu(transform.disp.x)
        y = self.um_to_dbu(transform.disp.y)
        width = self.um_to_dbu(item["local_box"].width())
        height = self.um_to_dbu(item["local_box"].height())
        for axis in ("x", "y"):
            extents = definition.get(f"{axis}_extents")
            if not extents:
                continue
            minimum = self.extent_coordinate(extents["min"], axis, objects, transforms)
            maximum = self.extent_coordinate(extents["max"], axis, objects, transforms)
            if maximum <= minimum:
                raise ValueError(f"Box {item['id']}: {axis.upper()} maximum must exceed minimum.")
            lower = self.snap_um_to_dbu(minimum)
            upper = self.snap_um_to_dbu(maximum)
            if upper <= lower:
                raise ValueError("Box extents collapse after grid snapping.")
            if axis == "x":
                x, width = lower, upper - lower
            else:
                y, height = lower, upper - lower
        for axis, size in (("x", width), ("y", height)):
            if f"{axis}_center" not in definition:
                continue
            center = definition[f"{axis}_center"]
            coordinate = (self.extent_coordinate(center, axis, objects, transforms)
                          if isinstance(center, dict) else self.resolve_offset(center))
            lower = self.snap_um_to_dbu(coordinate - self.dbu_to_um(size) / 2)
            if axis == "x":
                x = lower
            else:
                y = lower
        item["local_box"] = pya.DBox(0, 0, self.dbu_to_um(width), self.dbu_to_um(height))
        return pya.DCplxTrans(1, 0, False, self.dbu_to_um(x), self.dbu_to_um(y))

    def selected_box_definition(self):
        shape = self.selected_box()
        raw = shape.property("shapeforge.box")
        if raw is None:
            raise RuntimeError("Run Make Box Parametric first.")
        object_id = shape.property("shapeforge.object_id")
        objects = self.placement_objects()
        if object_id not in objects:
            raise RuntimeError("The box has no ShapeForge ID.")
        return shape, object_id, json.loads(raw), objects

    def define_box_extents(self, definition):
        definition = dict(definition)
        from shapeforge.dimensions import box_version
        definition["version"] = box_version(definition)
        self.validate()
        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")
        shape, object_id, _, _ = self.selected_box_definition()
        if shape.property("shapeforge.array_member") is not None:
            raise ValueError("Array boxes inherit size and position. Edit the base or dissolve the array.")
        self.validate_box_definition(definition)
        serialized = json.dumps(definition, allow_nan=False)
        components = self.primitive_update_plan()
        updates, _ = self.placement_plan(box_replacement=(object_id, definition), component_replacements=components)
        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: define box extents")
        try:
            self.apply_regeneration_plan(components, updates)
            from shapeforge.arrays import locate_box
            shape, _ = locate_box(self, object_id)
            shape.set_property("shapeforge.box", serialized)
        finally:
            view.commit()
        view.zoom_fit()

    def box_info(self):
        shape, object_id, definition, objects = self.selected_box_definition()
        lines = [f"Box ID: {object_id}"]
        member = shape.property("shapeforge.array_member")
        if member:
            member = json.loads(member)
            lines.append(f"Array repetition ({member['row']}, {member['column']}): inherits size and position from {member['owner_id']}.")
        raw_array = shape.property("shapeforge.array")
        if raw_array:
            lines.append("Array: " + json.dumps({key: value for key, value in json.loads(raw_array).items() if key != "members"}))
        for axis, dimension in (("x", "width_um"), ("y", "height_um")):
            extents = definition.get(f"{axis}_extents")
            if extents:
                for end in ("min", "max"):
                    ref = extents[end]
                    item = objects.get(ref["reference_id"])
                    label = item["label"] if item else f"MISSING: {ref['reference_id']}"
                    anchor = (f"port {ref['port']}" if ref["anchor"] == "port"
                              else ref["anchor"])
                    lines.append(f"{axis.upper()} {end}: {label}, {anchor}, "
                                 f"offset={ref.get('offset_um', 0)} µm")
            else:
                value = definition[dimension]
                from shapeforge.dimensions import is_port_width
                if is_port_width(value):
                    lines.append(f"{dimension}: width of port {value['port']} on {value['reference_id']}; "
                                 f"current={self.resolve_dimension(value):g} µm")
                else:
                    lines.append(f"{dimension}: {value} µm")
            center = definition.get(f"{axis}_center")
            if isinstance(center, dict):
                item = objects.get(center["reference_id"])
                label = item["label"] if item else f"MISSING: {center['reference_id']}"
                anchor = f"port {center['port']}" if center["anchor"] == "port" else center["anchor"]
                lines.append(f"{axis.upper()} center: {label}, {anchor}, offset={center.get('offset_um', 0)} µm")
            elif center is not None:
                lines.append(f"{axis.upper()} center: {center} µm")
            else:
                lines.append(f"{axis.upper()} center: automatic")
        box = shape.box
        lines.append(f"Current size: {self.dbu_to_um(box.width()):g} × "
                     f"{self.dbu_to_um(box.height()):g} µm")
        lines.append(f"Current center: ({self.dbu_to_um(box.left + box.right) / 2:g}, "
                     f"{self.dbu_to_um(box.bottom + box.top) / 2:g}) µm")
        return "\n".join(lines)

