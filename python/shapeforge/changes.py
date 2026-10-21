"""Compare planned values to native committed objects before opening undo steps."""

import json
import pya


class NativeIndex:
    """Fresh native handles for one application; discarded at transaction end."""
    def __init__(self, session):
        self.session, self.entries = session, {}
        for instance in session.cell.each_inst():
            identity = instance.property("shapeforge.instance_id")
            if identity:
                self.add(identity, instance)
        for layer in session.layout.layer_indexes():
            for shape in session.cell.shapes(layer).each():
                identity = shape.property("shapeforge.object_id")
                if identity:
                    self.add(identity, shape, layer)

    def add(self, identity, target, layer=None):
        self.entries[identity] = (target, layer)

    def get(self, identity):
        if identity not in self.entries:
            raise ValueError(f"Missing reference object: {identity}.")
        target, layer = self.entries[identity]
        if not target.is_valid():
            if layer is None:
                target = next(i for i in self.session.cell.each_inst() if i.property("shapeforge.instance_id") == identity)
            else:
                target = next(s for s in self.session.cell.shapes(layer).each() if s.property("shapeforge.object_id") == identity)
            self.add(identity, target, layer)
        return target

    def box(self, identity):
        return self.get(identity), self.entries[identity][1]


def planned_box(session, item, transform):
    x, y = session.um_to_dbu(transform.disp.x), session.um_to_dbu(transform.disp.y)
    return pya.Box(x, y, x + session.um_to_dbu(item["local_box"].width()),
                   y + session.um_to_dbu(item["local_box"].height()))


def changed_updates(session, updates, components=None):
    result = []
    index = NativeIndex(session)
    for item, transform in updates:
        if item["type"] == "array_remove":
            result.append((item, transform))
            continue
        target = item["object"]
        if ((item["type"] == "instance" and not hasattr(target, "cell_index"))
                or (item["type"] == "box" and not hasattr(target, "is_box"))):
            result.append((item, transform))
            continue
        changed = (target.dcplx_trans != transform if item["type"] == "instance"
                   else target.box != planned_box(session, item, transform))
        for field in ("array", "array_member"):
            value = item.get(field)
            raw = target.property("shapeforge." + field)
            if value != (json.loads(raw) if raw else None):
                changed = True
        # An instance repetition may need to point to its base's new component.
        if item.get("array_member") is not None and item["type"] == "instance":
            owner = index.get(item["array_member"]["owner_id"])
            changed = (changed or target.cell_index != owner.cell_index
                       or item["array_member"]["owner_id"] in (components or {}))
        if item.get("array_member") is not None and item["type"] == "box":
            _, layer = index.box(item["id"])
            changed = changed or layer != item.get("layer", layer)
        if changed:
            result.append((item, transform))
    return result
