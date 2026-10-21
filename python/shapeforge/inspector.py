"""Data and edit adapters for the selection inspector; no retained native handles."""

from copy import deepcopy
import math

from shapeforge.parameters import parameter_definition


def snapshot(session, context):
    if context.get("key") is None:
        return None
    obj = list(session.cellview.view().object_selection)[0]
    instance = obj.is_cell_inst()
    target = obj.inst() if instance else obj.shape
    import json
    def prop(key):
        value = target.property("shapeforge." + key)
        return None if value is None else json.loads(value)
    recipe = session.component_recipes.get(session.component_id_for_instance(target), {}) if instance else {}
    values = prop("geometry_parameters") if instance else None
    generation = values["values"] if values is not None else recipe.get("geometry", recipe.get("arguments", {}))
    if recipe.get("provider") == "shapeforge-project" and values is None:
        generation = recipe["overrides"]
    position = prop("via_center") if context["kind"] == "via_stack" else prop("port_position")
    definition = prop("box") if not instance else None
    actions = context["actions"]
    result = {"key": context["key"], "kind": context["kind"],
              "id": target.property("shapeforge.instance_id" if instance else "shapeforge.object_id"),
              "recipe": recipe, "values": generation, "definition": definition,
              "name": session.layout.cell(target.cell_index).name if instance else "Parametric box",
              "position": position, "angle": target.dcplx_trans.angle if instance else 0,
              "bounds": (target.dbbox() if instance else target.box.to_dtype(session.layout.dbu)).to_s(),
              "size": [target.bbox().width() * session.layout.dbu, target.bbox().height() * session.layout.dbu] if instance else
                      [target.box.width() * session.layout.dbu, target.box.height() * session.layout.dbu],
              "reasons": {"generation": actions.get("edit_primitive_parameters", actions.get("edit_project_port", "No editable generator.")),
                          "geometry": actions.get("define_box_extents", "Make Box Parametric first."),
                          "position": actions.get("define_via_center", actions.get("edit_project_port", "No parametric position."))},
              "tokens": {"generation": [recipe, values] if instance else None,
                         "geometry": definition,
                         "position": [position, target.dcplx_trans.angle if context["kind"] == "project_port" else None,
                                      prop("placement")]}}
    return deepcopy(result)


def generation_specs(state):
    recipe = state["recipe"]
    provider = recipe["provider"]
    if provider == "shapeic-cellkit":
        from typing import get_type_hints
        from shapeic_cellkit import PrimitiveGeometry
        from shapeforge.components import geometry_fields
        types = get_type_hints(PrimitiveGeometry)
        return {field.name: {"type": types[field.name].__name__,
                            "scale": 1e-6 if field.name.endswith("_m") else 1,
                            "unit": "µm" if field.name.endswith("_m") else ""} for field in geometry_fields()}
    if provider == "external-python":
        from shapeforge.external import generator_specs
        return generator_specs(recipe)
    if provider == "shapeic-cellkit-technology":
        from shapeforge.technology import parameter_specs
        return parameter_specs(recipe["generator"], recipe["pdk"])
    if provider == "shapeforge-project":
        from shapeforge.project_links import interface
        fields = interface(recipe["source"])
        specs = {name: {"type": "float", "unit": "µm", "child_default": definition,
                        "default_value": value} for name, definition, value in fields}
        for name in state["values"].keys() - specs.keys():
            specs[name] = {"type": "float", "orphan": True}
        return specs
    raise ValueError("This instance has no editable generator contract.")


def numeric(text, kind="float", scale=1, expression_dict=False):
    value = parameter_definition(text)
    if isinstance(value, str):
        return {"expression": value} if expression_dict else value
    if not math.isfinite(value):
        raise ValueError("Expected a finite number.")
    if kind == "int":
        if not value.is_integer():
            raise ValueError("Expected an integer or an integer-valued expression.")
        return int(value)
    return value * scale


def apply(session, base, section, values):
    """Reacquire and validate selection and the source token before calling Session."""
    from shapeforge.selection import selection_context
    current_context = selection_context(session)
    current = snapshot(session, current_context)
    if current is None or current["key"] != base["key"]:
        raise ValueError("Selection changed. Select the original object before applying this draft.")
    if current["tokens"][section] != base["tokens"][section]:
        raise ValueError("This section changed outside the inspector. Use Revert to reload it.")
    if current["reasons"][section]:
        raise ValueError(current["reasons"][section])
    if section == "generation":
        session.edit_primitive_parameters(values)
    elif section == "geometry":
        session.define_box_extents(values)
    elif section == "position":
        if current["kind"] == "via_stack":
            session.define_via_center(values["position"])
        else:
            from shapeforge.project_ports import edit
            edit(session, current["id"], current["values"], values["position"], values["angle"])
    else:
        raise ValueError("Unknown inspector section.")
    reselect(session, current["id"])


def reselect(session, identity):
    """Native wrappers can become invalid after a transaction; select by stable ID."""
    import pya
    item = session.placement_objects(raw=True)[identity]
    path = pya.ObjectInstPath()
    path.cv_index, path.top = session.cellview.index(), session.cell.cell_index()
    if item["type"] == "instance":
        path.append_path(pya.InstElement(item["instance"]))
    else:
        path.layer, path.shape = item["layer"], item["shape"]
    session.cellview.view().object_selection = [path]
