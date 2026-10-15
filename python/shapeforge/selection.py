"""Read-only selection snapshots and capabilities; never retain native targets."""

import json


def selection_context(session):
    result = {"key": None, "kind": None, "summary": "Select one direct box or instance.", "actions": {}}
    try:
        if session is None:
            raise ValueError("Create or open a ShapeForge project.")
        session.validate()
        selected = list(session.cellview.view().object_selection)
        if len(selected) != 1:
            raise ValueError("Select exactly one box or instance.")
        obj = selected[0]
        if obj.cv_index != session.cellview.index() or obj.top != session.cell.cell_index():
            raise ValueError("Select an object in the ShapeForge working cell.")
        instance = obj.is_cell_inst()
        if len(obj.path) != (1 if instance else 0):
            raise ValueError("Select a direct box or instance in the working cell.")
        target = obj.inst() if instance else obj.shape
        if not instance and not target.is_box():
            raise ValueError("The selected shape is not a box.")
        kind = "instance" if instance else "box"
        object_id = target.property("shapeforge.instance_id" if instance else "shapeforge.object_id")
        result["kind"] = kind
        lines = [kind.capitalize(), f"ID: {object_id or 'unregistered'}"]
        bbox = target.bbox() if instance else target.box
        lines.append(f"Bounds (µm): {bbox.to_dtype(session.layout.dbu).to_s()}")
        actions = result["actions"]
        actions["selection_info"] = None
        actions["hide_ports"] = None
        arrays = instance and (target.is_regular_array() or target.size() != 1)
        copies = []
        for other in session.cell.each_inst():
            if other.property("shapeforge.instance_id") == object_id and object_id:
                copies.append(other)
        for layer in session.layout.layer_indexes():
            for other in session.cell.shapes(layer).each():
                if other.property("shapeforge.object_id") == object_id and object_id:
                    copies.append(other)
        duplicate = len(copies) > 1
        # Instance wrappers have Python identity hashes, even when two wrappers
        # reference the same native instance. Use registered IDs; for copies or
        # foreign instances, use their current native position in the cell.
        if object_id and not duplicate:
            identity = object_id
        elif instance:
            identity = (object_id, next(index for index, other in enumerate(session.cell.each_inst()) if other == target))
        else:
            identity = hash(target)  # Shape hashes are stable across wrappers.
        result["key"] = (id(session), kind, identity)
        reason = ("Instance arrays are not supported." if arrays else
                  "Duplicate object ID: choose the object that keeps its identity first." if duplicate else None)
        raw_box = None if instance else target.property("shapeforge.box")
        definition = None if raw_box is None else json.loads(raw_box)
        if instance:
            component_id = session.component_id_for_instance(target)
            component = session.components.get(component_id)
            recipe = session.component_recipes.get(component_id, {})
            lines += [f"Component: {session.layout.cell(target.cell_index).name}",
                      f"Provider: {recipe.get('provider', 'unregistered')}",
                      f"Transform: {target.dcplx_trans.to_s()}",
                      "Arguments: " + json.dumps(recipe.get("arguments", recipe.get("geometry", {})), ensure_ascii=False)]
            bindings = target.property("shapeforge.geometry_parameters")
            if bindings:
                lines.append("Bindings: " + bindings)
            editable_recipe = recipe.get("provider") in ("shapeic-cellkit", "external-python", "shapeic-cellkit-technology") and component is not None
            actions["edit_primitive_parameters"] = reason or (None if object_id and editable_recipe else "Requires a registered generator recipe and component.")
            actions["show_ports"] = reason or (None if component is not None and len(component.ports) else "No linked component ports.")
            actions["keep_selected_instance_id"] = (None if duplicate and not arrays else "Available for copied instance IDs.")
            from shapeforge.project_ports import is_project_port
            array_raw = target.property("shapeforge.array")
            member_raw = target.property("shapeforge.array_member")
            in_array = array_raw is not None or member_raw is not None
            actions["create_array"] = reason or ("Already belongs to an array. Use Edit Array." if in_array else
                                                 "Project interface ports cannot be repeated." if is_project_port(recipe) else
                                                 None if object_id and component is not None else "Requires a registered component.")
            for name in ("edit_array", "dissolve_array"):
                actions[name] = reason or (None if in_array else "Select an array base or repetition.")
            if array_raw:
                definition_array = json.loads(array_raw)
                lines.append("Array: " + json.dumps({key: value for key, value in definition_array.items() if key != "members"}))
            if member_raw:
                member = json.loads(member_raw)
                lines.append(f"Array repetition ({member['row']}, {member['column']}); base: {member['owner_id']}")
                actions["edit_primitive_parameters"] = "Inherits geometry from the array base. Edit the base or dissolve the array."
            from shapeforge.technology import is_via_stack
            if is_via_stack(recipe):
                result["kind"] = "via_stack"
                lines[0] = "Via stack"
                lines.append("Via center: " + (target.property("shapeforge.via_center") or "free"))
                actions["define_via_center"] = actions["edit_primitive_parameters"]
            if is_project_port(recipe):
                result["kind"] = "project_port"
                lines[0] = "Project port: " + recipe["arguments"]["pinName"]
                lines.append("Position: " + (target.property("shapeforge.port_position") or "free"))
                actions["edit_project_port"] = actions.pop("edit_primitive_parameters")
                actions["delete_project_port"] = reason
        else:
            lines.append(f"Layer: {session.layout.get_info(obj.layer).to_s()}")
            lines.append("Definition: " + (json.dumps(definition, ensure_ascii=False) if definition else "not parametric"))
            has_extents = definition and any(definition.get(key) is not None for key in ("x_extents", "y_extents", "x_center", "y_center"))
            actions["make_box_parametric"] = reason or ("Use Define Box Extents to edit dimensions governed by extents or center." if has_extents else None)
            for name in ("define_box_extents", "define_box_center", "box_info"):
                actions[name] = reason or (None if definition and object_id else "Make Box Parametric first.")
            actions["keep_selected_box_id"] = None if duplicate else "Available for copied box IDs."
            array_raw = target.property("shapeforge.array")
            member_raw = target.property("shapeforge.array_member")
            in_array = array_raw is not None or member_raw is not None
            actions["create_array"] = reason or ("Already belongs to an array. Use Edit Array." if in_array else
                                                 None if definition and object_id else "Make Box Parametric first.")
            for name in ("edit_array", "dissolve_array"):
                actions[name] = reason or (None if in_array else "Select an array base or repetition.")
            if array_raw:
                lines.append("Array: " + json.dumps({key: value for key, value in json.loads(array_raw).items() if key != "members"}))
            if member_raw:
                member = json.loads(member_raw)
                lines.append(f"Array repetition ({member['row']}, {member['column']}); base: {member['owner_id']}")
                for name in ("make_box_parametric", "define_box_extents", "define_box_center"):
                    actions[name] = "Inherits size and position from the array base. Edit the base or dissolve the array."
        registered = object_id and (instance or definition)
        base = reason or (None if registered else "Register this object with ShapeForge first.")
        raw = target.property("shapeforge.placement")
        lines.append("Placement: " + (raw or "none"))
        for name in ("placement_info", "edit_placement", "remove_placement"):
            actions[name] = base or (None if raw else "No placement relation.")
        governed = definition and any(definition.get(key) is not None for key in ("x_extents", "y_extents", "x_center", "y_center"))
        actions["panel_place_by_edges"] = base or ("Extents or centers already govern this box's position." if governed else None)
        if instance:
            actions["panel_place_by_ports"] = base or actions["show_ports"]
            raw_position = target.property("shapeforge.via_center") or target.property("shapeforge.port_position")
            if raw_position and any(f"{axis}_center" in json.loads(raw_position) for axis in ("x", "y")):
                for name in ("panel_place_by_edges", "panel_place_by_ports"):
                    actions[name] = "Centers control its position. Free both coordinates to use placement."
        if target.property("shapeforge.array_member") is not None:
            for name in ("panel_place_by_edges", "panel_place_by_ports", "edit_placement", "remove_placement"):
                if name in actions:
                    actions[name] = "Array controls this repetition's position. Edit the base or dissolve the array."
        actions["regenerate_placement"] = base
        # Full dependency checks can fail elsewhere in the layout. Keep inspection
        # and ID recovery usable instead of making the whole panel disappear.
        try:
            objects = session.placement_objects()
            references = [item for key, item in objects.items() if key != object_id]
            if not references:
                actions["panel_place_by_edges"] = actions["panel_place_by_edges"] or "No other registered reference object."
            if instance and not any(item["type"] == "instance" and item["component"] is not None and len(item["component"].ports) for item in references):
                actions["panel_place_by_ports"] = actions["panel_place_by_ports"] or "No other linked reference with ports."
        except Exception as error:
            for name in ("panel_place_by_edges", "panel_place_by_ports", "edit_placement", "define_box_extents", "define_box_center", "define_via_center", "regenerate_placement"):
                if name in actions:
                    actions[name] = actions[name] or str(error)
        if reason:
            lines.append(reason)
        if not session.layout.is_editable():
            for name in actions:
                if name not in ("selection_info", "box_info", "placement_info", "show_ports", "hide_ports"):
                    actions[name] = "Open KLayout in editor mode."
        disabled = sorted({message for message in actions.values() if message})
        if disabled:
            lines.append("Unavailable actions:\n" + "\n".join(disabled))
        result["summary"] = "\n".join(lines)
    except Exception as error:
        result.update(key=None, kind=None, summary=str(error), actions={})
    return result
