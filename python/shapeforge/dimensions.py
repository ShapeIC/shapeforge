"""Explicit, serializable dimension bindings; values are expressed in µm."""

import math


def is_port_width(value):
    return isinstance(value, dict) and value.get("kind") == "port_width"


def validate_port_width(value):
    if (not is_port_width(value) or set(value) != {"kind", "reference_id", "port"}
            or any(not isinstance(value[key], str) or not value[key].strip()
                   for key in ("reference_id", "port"))):
        raise ValueError("A port width binding needs kind, reference_id and port.")


def width_references(values):
    return [value for value in values.values() if is_port_width(value)]


def positive_width(width):
    if not math.isfinite(width) or width <= 0:
        raise ValueError("Referenced port width must be positive and finite.")
    return width


def box_version(definition):
    if any(is_port_width(definition.get(key)) for key in ("width_um", "height_um")):
        return 3
    return 2 if any(f"{axis}_center" in definition for axis in ("x", "y")) else 1
