"""Stable names and committed read-only outputs of direct linked projects."""

import json
import keyword
import re
from pathlib import Path

NAME = "shapeforge.instance_name"
OUTPUTS = "shapeforge.internal_parameters"


def valid_name(name):
    return (isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)
            and not name.startswith("_") and name != "floor")


def default_name(identity):
    return "child_" + re.sub(r"\W", "_", identity)


def name_of(instance):
    return instance.property(NAME) or default_name(instance.property("shapeforge.instance_id"))


def instances(session, definitions=None):
    local = session.parameter_definitions() if definitions is None else definitions
    result, ids = {}, set()
    for instance in session.cell.each_inst():
        recipe = session.component_recipes.get(session.component_id_for_instance(instance), {})
        if recipe.get("provider") != "shapeforge-project":
            continue
        identity = instance.property("shapeforge.instance_id")
        if not identity:
            continue
        name = name_of(instance)
        if identity in ids:
            raise ValueError("Duplicate instance ID. Run Keep Selected Instance ID first.")
        if not valid_name(name) or name in result or name in local:
            raise ValueError(f"Invalid or duplicate linked instance name: {name}. Rename the instance explicitly.")
        ids.add(identity)
        if instance.is_regular_array() or instance.size() != 1:
            raise ValueError("Linked parameter references require individual instances, not native arrays.")
        result[name] = instance
    return result


def new_name(session, source):
    base = Path(source).name.removesuffix(".shapeforge.json").removesuffix(".json")
    base = re.sub(r"\W", "_", base)
    if not valid_name(base):
        base = "child"
    taken = set(instances(session)) | set(session.parameter_definitions())
    index = 1
    while f"{base}{index}" in taken:
        index += 1
    return f"{base}{index}"


def outputs(instance):
    raw = instance.property(OUTPUTS)
    return json.loads(raw) if raw is not None else {}


def store(instance, generated):
    if generated.internal_parameters is not None:
        value = json.dumps(generated.internal_parameters, allow_nan=False)
        if instance.property(OUTPUTS) != value:
            instance.set_property(OUTPUTS, value)


def value(session, reference):
    alias, parameter = reference.split(".")
    instance = instances(session).get(alias)
    if instance is None:
        raise ValueError(f"Unknown linked instance: {alias}")
    values = outputs(instance)
    if parameter not in values:
        raise ValueError(f"Instance {alias} has no internal parameter '{parameter}'. Regenerate after updating its source.")
    return values[parameter]


def rows(session):
    return {f"{alias}.{name}": value for alias, instance in instances(session).items()
            for name, value in outputs(instance).items()}
