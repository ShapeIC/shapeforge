import pya
import math
from uuid import uuid4
import gdsfactory as gf
from shapeforge.components import simplediffpair, test_block

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

    def transformed_ports(self, instance, component):
        """Return port coordinates in the instance's parent cell."""
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
