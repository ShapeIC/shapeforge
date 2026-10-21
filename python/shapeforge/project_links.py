"""Known provider for linked ShapeForge recipes; never executes code from JSON."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import keyword
from pathlib import Path
from copy import deepcopy
from shapeforge import runtime

from shapeforge.parameters import resolve_parameters, resolve_value

PROVIDER = "shapeforge-project"
PUBLIC_PROPERTY = "shapeforge.public_parameters"
_ancestors = ContextVar("shapeforge_project_ancestors", default=())
_cache = {}
_variants = {}


def validate_public(names, parameters):
    if (not isinstance(names, list) or any(not isinstance(n, str) or not n.isidentifier()
            or keyword.iskeyword(n) or n not in parameters for n in names)
            or len(names) != len(set(names))):
        raise ValueError("External parameters must be unique existing parameter names valid as Python arguments.")
    from shapeforge.parameters import derived_parameters
    derived = set(names) & derived_parameters(parameters)
    if derived:
        raise ValueError("Parameters derived from geometry or child instances must be internal. Uncheck External for: " + ", ".join(sorted(derived)))
    return list(names)


def resolve_overrides(values, parameters, names=None):
    if not isinstance(values, dict) or any(not isinstance(n, str) for n in values):
        raise ValueError("Project overrides must be a parameter/value object.")
    if names is not None and set(values) - set(names):
        raise ValueError("Parameters are no longer external in the linked project; remove these overrides explicitly: "
                         + ", ".join(sorted(set(values) - set(names))))
    return {name: resolve_value(value, parameters) for name, value in values.items()}


@contextmanager
def project_scope(path):
    previous = _ancestors.get()
    canonical = Path(path).resolve() if path else None
    token = _ancestors.set(previous if canonical is None or previous and previous[-1] == canonical else previous + (canonical,))
    try:
        yield
    finally:
        _ancestors.reset(token)


def read_tree(path, ancestors=()):
    path = Path(path).resolve()
    if path in ancestors:
        from shapeforge.project import ProjectError
        raise ProjectError("Recursive project inclusion: " + " → ".join(str(p) for p in (*ancestors, path)))
    document, digest = runtime.loaded(("project_tree", str(path)), lambda: _read_tree(path, ancestors))
    return deepcopy(document), digest


def _read_tree(path, ancestors=()):
    """Validate the entire include graph and dependencies before any generation."""
    from shapeforge.project import validate_document, _reject_constant, _unique_keys, ProjectError
    path = Path(path).resolve()
    if path in ancestors:
        raise ProjectError("Recursive project inclusion: " + " → ".join(str(p) for p in (*ancestors, path)))
    try:
        raw = runtime.loaded(("project_bytes", str(path)), path.read_bytes)
        document = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
        validate_document(document)
        fingerprints = [hashlib.sha256(raw).hexdigest()]
        for entry in document["components"]:
            recipe = entry["recipe"]
            if recipe["provider"] == PROVIDER:
                source = (path.parent / recipe["source"]).resolve()
                child, digest = read_tree(source, (*ancestors, path))
                child_pdk = child["environment"]["pdk"]
                if child_pdk is not None and child_pdk != recipe["pdk"]:
                    raise ProjectError(f"Linked project PDK {child_pdk} differs from parent PDK {recipe['pdk']}.")
                recipe["source"] = str(source)
                fingerprints.append(digest)
            elif recipe["provider"] == "external-python":
                from shapeforge.external import source_snapshot
                fingerprints.append(source_snapshot(recipe)["digest"])
            elif recipe["provider"] in ("shapeic-cellkit", "shapeic-cellkit-technology"):
                from shapeforge.components import provider_stamp
                fingerprints.append(provider_stamp(recipe))
        digest = hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
        return document, digest
    except ProjectError:
        raise
    except Exception as error:
        raise ProjectError(f"Could not read linked project '{path}': {error}") from error


def interface(path):
    document, _ = read_tree(path, _ancestors.get())
    names = document.get("public_parameters", [])
    values = {name: resolve_value(name, document["parameters"]) for name in names}
    return [(name, document["parameters"][name], values[name]) for name in names]


@runtime.provider
def generate_project(source, overrides, pdk, parameters=None):
    from shapeforge.components import GeneratedComponent, dependency_versions
    from shapeforge.project import prepare_document, _geometry_signature
    from shapeforge.project_ports import rows
    import gdsfactory as gf

    source = Path(source).resolve()
    document, digest = read_tree(source, _ancestors.get())
    child_pdk = document["environment"]["pdk"]
    if child_pdk is not None and child_pdk != pdk:
        raise ValueError(f"Linked project PDK {child_pdk} differs from parent PDK {pdk}.")
    names = document.get("public_parameters", [])
    effective = resolve_overrides(overrides, parameters or {}, names)
    document["parameters"].update(effective)
    arguments = {name: resolve_value(name, document["parameters"]) for name in names}
    versions = dependency_versions()
    variant = runtime.cache_key([str(source), digest, pdk, arguments, versions])
    if variant in _variants:
        runtime.count("generation_cache_hits")
        previous = _variants[variant]
        return GeneratedComponent(previous.component, dict(previous.recipe, overrides=effective), previous.internal_parameters)
    runtime.count("child_reconstructions")
    prepared = prepare_document(document, source)
    session = prepared.session
    internal = {name: value for name, value in session.resolved_parameters().items() if name not in names}
    if session.cell.dbbox().empty():
        raise ValueError(f"Linked project '{source}' has no geometry.")
    recipe = {"provider": PROVIDER, "source": str(source), "pdk": pdk,
              "public_parameters": names, "overrides": effective,
              "arguments": {n: arguments[n] for n in names},
              "source_digest": hashlib.sha256(runtime.loaded(("project_bytes", str(source)), source.read_bytes)).hexdigest(),
              "dependency_digest": digest, "versions": versions}
    pins = rows(session)
    # Build after staging so changes to providers as well as linked files invalidate reuse.
    geometry_recipe = {name: value for name, value in recipe.items() if name != "overrides"}
    active_recipes = sorted({json.dumps(session.component_recipes[session.component_id_for_instance(i)],
                                       sort_keys=True, allow_nan=False) for i in session.cell.each_inst()})
    key = json.dumps([geometry_recipe, active_recipes, _geometry_signature(session.cell, session.layout.dbu), pins],
                     sort_keys=True, allow_nan=False)
    if key not in _cache:
        component = gf.Component()
        component.kdb_cell.copy_tree(session.cell)
        previous_pdk = gf.pdk._ACTIVE_PDK
        try:
            if previous_pdk is None:
                gf.gpdk.PDK.activate()
            for pin in pins:
                instance = next(i for i in session.cell.each_inst() if i.property("shapeforge.instance_id") == pin["id"])
                original = next(iter(session.components[session.component_id_for_instance(instance)].ports))
                info = original.layer_info
                component.add_port(name=pin["name"], center=pin["center"], width=pin["width"],
                                   orientation=pin["orientation"], layer=(info.layer, info.datatype),
                                   port_type=original.port_type)
        finally:
            gf.pdk._ACTIVE_PDK = previous_pdk
        _cache[key] = component
    result = GeneratedComponent(_cache[key], recipe, internal)
    _variants[variant] = result
    return result
