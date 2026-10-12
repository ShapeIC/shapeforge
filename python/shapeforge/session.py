import pya
import math
from uuid import uuid4
import gdsfactory as gf
from shapeforge.components import simplediffpair, test_block

import json

class Session:
    def __init__(self, cellview, cell, grid_um=0.005):
        self.cellview = cellview
        self.layout = cellview.layout()
        self.cell = cell
        self._test_component = None

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
        self.validate()

        layout = self.layout
        view = self.cellview.view()

        if not layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        # PDK genérico solamente para este componente de prueba.
        gf.gpdk.PDK.activate()
        if self._test_component is None:
            self._test_component = simplediffpair()

        source = self._test_component
        print("Generated object:", source)
        print("Generated type:", type(source))

        component_id = str(uuid4())
        gap = self.snap_um_to_dbu(5.0)

        if gap <= 0:
            raise RuntimeError("The grid is too coarse for this example.")

        bbox = self.cell.bbox()

        if bbox.empty():
            x = 0
        else:
            x = (
                math.ceil((bbox.right + gap) / self.grid_dbu)
                * self.grid_dbu
            )

        view.cancel()
        view.transaction("ShapeForge: insert gdsfactory component")

        try:
            target = layout.create_cell("SF_TEST_BLOCK")

            # Copiar geometría y jerarquía al layout de la sesión.
            target.copy_tree(source.kdb_cell)

            # El vínculo forma parte de la celda de KLayout.
            target.set_property(
                "shapeforge.component_id",
                component_id,
            )

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
        finally:
            view.commit()

        self.components[component_id] = source

        self.cellview.cell = self.cell
        view.add_missing_layers()
        view.max_hier()
        view.zoom_fit()

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

        return (
            round(value_um / self.grid_um)
            * self.grid_dbu
        )

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
        protected_ids = set()

        for instance in managed:
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

        for instance_id, group in groups.items():
            if len(group) > 1 and instance_id in protected_ids:
                raise RuntimeError(
                    "A duplicated ID participates in placement.\n"
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

    def placement_candidates(self):
        self.validate()

        view = self.cellview.view()
        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
        ]

        if len(selection) != 2:
            raise RuntimeError("Select exactly two instances.")

        candidates = []

        for obj in selection:
            if (
                not obj.is_cell_inst()
                or obj.top != self.cell.cell_index()
                or len(obj.path) != 1
            ):
                raise RuntimeError(
                    "Select two direct instances in the working cell."
                )

            instance = obj.inst()

            if instance.size() != 1:
                raise RuntimeError("Instance arrays are not supported.")

            instance_id = instance.property("shapeforge.instance_id")
            component_id = self.component_id_for_instance(instance)
            component = self.components.get(component_id)

            if not instance_id or component is None:
                raise RuntimeError(
                    "Both instances need ShapeForge IDs and components."
                )

            if not len(component.ports):
                raise RuntimeError("Both components must have ports.")

            # El ID debe ser único en toda la celda de trabajo.
            matches = sum(
                other.property("shapeforge.instance_id") == instance_id
                for other in self.cell.each_inst()
            )

            if matches != 1:
                raise RuntimeError(
                    "Duplicate instance ID. Run Normalize Instance IDs."
                )

            cell = self.layout.cell(instance.cell_index)

            candidates.append({
                "id": instance_id,
                "label": f"{cell.name} | {instance_id}",
                "instance": instance,
                "component": component,
            })

        return candidates

    def place_by_ports(self, a, port_a, b, port_b, dx, dy):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        if a["id"] == b["id"]:
            raise RuntimeError("A and B must be different instances.")

        relation = {
            "version": 1,
            "reference_id": a["id"],
            "reference_port": port_a,
            "target_port": port_b,
            "dx_um": dx,
            "dy_um": dy,
        }

        serialized = json.dumps(relation, allow_nan=False)

        # Validar y calcular con la relación propuesta,
        # sin guardarla todavía.
        updates, offsets = self.placement_plan(
            replacement=(b["id"], relation)
        )

        instances = self.placement_instances()
        target = instances[b["id"]]["instance"]

        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: define placement")

        try:
            for instance, transform in updates:
                instance.dcplx_trans = transform

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
            transform_a = a["instance"].dcplx_trans

        if transform_b is None:
            transform_b = b["instance"].dcplx_trans

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

        if port_a not in ports_a or port_b not in ports_b:
            raise RuntimeError("A placement port no longer exists.")

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
        self.validate()

        result = {}

        for instance in self.cell.each_inst():
            instance_id = instance.property("shapeforge.instance_id")

            if not instance_id:
                continue

            if instance_id in result:
                raise RuntimeError(
                    "Duplicate instance ID. Run Normalize Instance IDs."
                )

            if instance.size() != 1:
                raise RuntimeError(
                    "Placement does not support instance arrays."
                )

            component_id = self.component_id_for_instance(instance)
            component = self.components.get(component_id)

            result[instance_id] = {
                "id": instance_id,
                "instance": instance,
                "component": component,
            }

        return result

    def regenerate_placement(self):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        updates, _ = self.placement_plan()

        if not updates:
            return 0

        view = self.cellview.view()
        self.hide_ports()
        view.cancel()
        view.transaction("ShapeForge: regenerate placement")

        try:
            for instance, transform in updates:
                instance.dcplx_trans = transform
        finally:
            view.commit()

        view.zoom_fit()
        return len(updates)

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
        finally:
            view.commit()

        return len(updates)

    def parameters(self):
        self.validate()

        raw = self.cell.property("shapeforge.parameters")
        return {} if raw is None else json.loads(raw)

    def set_parameter(self, name, value_um):
        self.validate()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        name = name.strip()

        if not name.isidentifier():
            raise ValueError(
                "Use a valid parameter name, for example: spacing."
            )

        if not math.isfinite(value_um):
            raise ValueError("The parameter value must be finite.")

        parameters = self.parameters()
        parameters[name] = float(value_um)
        serialized = json.dumps(parameters, allow_nan=False)

        view = self.cellview.view()
        view.cancel()
        view.transaction(f"ShapeForge: set parameter {name}")

        try:
            self.cell.set_property(
                "shapeforge.parameters",
                serialized,
            )
        finally:
            view.commit()

    def resolve_offset(self, value):
        """Resolve a numeric offset or a parameter name, in µm."""
        if isinstance(value, str):
            parameters = self.parameters()

            if value not in parameters:
                raise ValueError(f"Unknown parameter: {value}")

            result = float(parameters[value])
        else:
            result = float(value)

        if not math.isfinite(result):
            raise ValueError("The offset must be finite.")

        return result

    def parse_offset(self, text):
        """Accept a number or an existing parameter name."""
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

        view = self.cellview.view()
        selection = [
            obj
            for obj in view.object_selection
            if obj.cv_index == self.cellview.index()
        ]

        if len(selection) != 1:
            raise RuntimeError("Select exactly one target instance B.")

        obj = selection[0]

        if (
            not obj.is_cell_inst()
            or obj.top != self.cell.cell_index()
            or len(obj.path) != 1
        ):
            raise RuntimeError(
                "Select a direct instance in the working cell."
            )

        instance = obj.inst()

        if instance.size() != 1:
            raise RuntimeError("Instance arrays are not supported.")

        instance_id = instance.property("shapeforge.instance_id")

        if not instance_id:
            raise RuntimeError("The instance has no ShapeForge ID.")

        instances = self.placement_instances()
        target = instances[instance_id]

        raw = instance.property("shapeforge.placement")

        if raw is None:
            raise RuntimeError("The selected instance has no placement relation.")

        relation = json.loads(raw)

        if relation.get("version") != 1:
            raise RuntimeError("Unsupported placement version.")

        return target, relation

    def placement_info(self):
        target, relation = self.selected_placement_target()
        instances = self.placement_instances()

        reference_id = relation["reference_id"]
        reference = instances.get(reference_id)

        if reference is None:
            reference_name = "Missing instance"
        else:
            reference_name = self.layout.cell(
                reference["instance"].cell_index
            ).name

        target_name = self.layout.cell(
            target["instance"].cell_index
        ).name

        def describe(value):
            try:
                resolved = self.resolve_offset(value)
            except ValueError as error:
                return f"{value} — {error}"

            if isinstance(value, str):
                return f"{value} = {resolved:g} µm"

            return f"{resolved:g} µm"

        return (
            f"A: {reference_name}\n"
            f"ID: {reference_id}\n"
            f"Port: {relation['reference_port']}\n\n"
            f"B: {target_name}\n"
            f"ID: {target['id']}\n"
            f"Port: {relation['target_port']}\n\n"
            f"dx: {describe(relation['dx_um'])}\n"
            f"dy: {describe(relation['dy_um'])}"
        )

    def edit_placement(self, dx, dy):
        target, relation = self.selected_placement_target()
        instances = self.placement_instances()

        reference = instances.get(relation["reference_id"])

        if reference is None:
            raise RuntimeError("The reference instance no longer exists.")

        if reference["component"] is None or target["component"] is None:
            raise RuntimeError("A linked component is unavailable.")

        # Reutilizar la validación, el cálculo y la transacción existentes.
        return self.place_by_ports(
            reference,
            relation["reference_port"],
            target,
            relation["target_port"],
            dx,
            dy,
        )

    def remove_placement(self):
        target, _ = self.selected_placement_target()

        if not self.layout.is_editable():
            raise RuntimeError("Open KLayout in editor mode: klayout -e")

        view = self.cellview.view()
        view.cancel()
        view.transaction("ShapeForge: remove placement")

        try:
            target["instance"].delete_property("shapeforge.placement")
        finally:
            view.commit()

    def placement_plan(self, replacement=None):
        """
        Calculate all placements without modifying KLayout.

        replacement: optional (target_id, proposed_relation).
        """
        instances = self.placement_instances()
        relations = {}

        for instance_id, item in instances.items():
            raw = item["instance"].property("shapeforge.placement")

            if raw is not None:
                relation = json.loads(raw)

                if relation.get("version") != 1:
                    raise RuntimeError("Unsupported placement version.")

                relations[instance_id] = relation

        if replacement is not None:
            target_id, relation = replacement

            if target_id not in instances:
                raise RuntimeError("The target instance no longer exists.")

            relations[target_id] = relation

        # Validar referencias antes de recorrer las dependencias.
        for target_id, relation in relations.items():
            reference_id = relation["reference_id"]

            if reference_id not in instances:
                raise RuntimeError(
                    f"Missing reference instance: {reference_id}"
                )

            for instance_id in (reference_id, target_id):
                if instances[instance_id]["component"] is None:
                    raise RuntimeError(
                        f"Unavailable component for instance: {instance_id}"
                    )

        transforms = {
            instance_id: item["instance"].dcplx_trans
            for instance_id, item in instances.items()
        }

        visiting = set()
        completed = set()
        path = []
        order = []
        offsets = {}

        def visit(instance_id):
            if instance_id in completed:
                return

            if instance_id in visiting:
                start = path.index(instance_id)
                cycle = path[start:] + [instance_id]

                raise RuntimeError(
                    "Placement cycle detected:\n" + " -> ".join(cycle)
                )

            visiting.add(instance_id)
            path.append(instance_id)

            relation = relations.get(instance_id)

            if relation is not None:
                reference_id = relation["reference_id"]

                # Calcular primero la referencia.
                visit(reference_id)

                transform, dx, dy = self.placement_transform(
                    instances[reference_id],
                    relation["reference_port"],
                    instances[instance_id],
                    relation["target_port"],
                    relation["dx_um"],
                    relation["dy_um"],
                    transform_a=transforms[reference_id],
                    transform_b=transforms[instance_id],
                )

                transforms[instance_id] = transform
                offsets[instance_id] = (dx, dy)
                order.append(instance_id)

            path.pop()
            visiting.remove(instance_id)
            completed.add(instance_id)

        for target_id in sorted(relations):
            visit(target_id)

        updates = [
            (instances[instance_id]["instance"], transforms[instance_id])
            for instance_id in order
        ]

        return updates, offsets
