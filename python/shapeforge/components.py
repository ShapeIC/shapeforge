import gdsfactory as gf
import os
from pathlib import Path
from dataclasses import asdict, dataclass, fields
from importlib.metadata import PackageNotFoundError, version
import json
import math
import tomllib
from typing import get_type_hints
from shapeforge.parameters import resolve_value


@dataclass(frozen=True)
class GeneratedComponent:
    component: gf.Component
    recipe: dict


# Keep generated objects alive and reuse them across sessions. CellKit providers
# may create explicitly named gdsfactory cells and cannot safely build twice.
_generated = {}


def dependency_versions():
    result = {}
    for package in ("shapeic-cellkit", "gdsfactory", "kfactory", "ihp-gdsfactory"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            pass
    return result


def cellkit_catalog(pdk):
    try:
        from shapeic_cellkit import CellKitCatalog
    except ImportError as error:
        raise RuntimeError("ShapeIC CellKit is unavailable in KLayout's Python environment.") from error
    root = Path(os.environ.get(
        "SHAPEFORGE_CELLKIT_ROOT", Path(__file__).resolve().parents[2] / "shapeic-cellkit"
    ))
    pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        raise RuntimeError("Set PDK_ROOT to the directory containing the installed PDKs.")
    return CellKitCatalog.open(root=root, pdk=pdk, pdk_root=pdk_root)


def geometry_fields():
    """Discover the public geometry contract without a per-primitive argument list."""
    from shapeic_cellkit import PrimitiveGeometry
    return fields(PrimitiveGeometry)


def resolve_geometry(geometry, parameters):
    """Numeric dimensions are SI; expressions use the project's µm values."""
    from shapeic_cellkit import PrimitiveGeometry
    expected = {field.name for field in geometry_fields()}
    if not isinstance(geometry, dict) or set(geometry) != expected:
        raise ValueError(f"CellKit geometry fields must be: {', '.join(sorted(expected))}.")
    types = get_type_hints(PrimitiveGeometry)
    values = dict(geometry)
    for name, value in geometry.items():
        if not isinstance(value, str):
            if types[name] is int and type(value) is not int:
                raise ValueError(f"CellKit geometry {name} must be an integer.")
            if types[name] is float and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"CellKit geometry {name} must be a finite number.")
            continue
        value = resolve_value(value, parameters)
        if types[name] is int:
            if not value.is_integer():
                raise ValueError(f"CellKit geometry {name} must evaluate to an integer.")
            value = int(value)
        elif name.endswith("_m"):
            value *= 1e-6
        values[name] = value
    return PrimitiveGeometry(**values)


def available_primitives(catalog):
    """List catalog primitives with a provider for the selected PDK."""
    from shapeic_cellkit.errors import ManifestValidationError, ProviderNotFoundError
    result = {}
    for name in catalog.primitive_names():
        try:
            result[name] = catalog.primitive(name)
        except (ProviderNotFoundError, ManifestValidationError):
            # Some catalog entries describe logical cells only and have no
            # physical descriptor/render contract yet (e.g. commonsource).
            continue
    return result


def generate_cellkit(primitive, geometry, pdk="ihp-sg13g2", options=None):
    try:
        from shapeic_cellkit import PrimitiveGeometry
    except ImportError as error:
        raise RuntimeError("ShapeIC CellKit is unavailable in KLayout's Python environment.") from error
    # The current public render contract accepts geometry only. Reject options
    # rather than silently ignoring inputs that might affect generation.
    if options is not None and not isinstance(options, dict):
        raise ValueError("CellKit generation options must be an object.")
    if options:
        raise ValueError("This CellKit render contract does not support additional options.")
    values = asdict(geometry) if isinstance(geometry, PrimitiveGeometry) else geometry
    expected = {field.name for field in geometry_fields()}
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError(f"CellKit geometry fields must be: {', '.join(sorted(expected))}.")
    values = dict(values)
    types = get_type_hints(PrimitiveGeometry)
    for name in expected:
        value, kind = values[name], types[name]
        if kind is int and type(value) is not int:
            raise ValueError(f"CellKit geometry {name} must be an integer.")
        if kind is float:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"CellKit geometry {name} must be a finite number.")
            # Unit conversion can yield 4.9999999999999996e-6 for 5 µm.
            # Normalize floating point noise before rendering and recording
            # effective values, so equivalent inputs share a generator cache.
            values[name] = float(format(value, ".15g"))
    geometry = PrimitiveGeometry(**values)
    catalog = cellkit_catalog(pdk)
    descriptor = catalog.primitive(primitive)
    versions = dependency_versions()
    if "shapeic-cellkit" not in versions:
        # Source checkouts can be imported through PYTHONPATH without installed
        # distribution metadata. Record their declared version when available.
        metadata = catalog.root / "pyproject.toml"
        if metadata.is_file():
            with metadata.open("rb") as handle:
                versions["shapeic-cellkit"] = tomllib.load(handle)["project"]["version"]
        else:
            versions["shapeic-cellkit"] = "source-checkout (version unavailable)"
    recipe = {
        "provider": "shapeic-cellkit", "pdk": pdk, "primitive": primitive,
        "geometry": asdict(geometry), "options": {},
        "versions": versions,
        "implementation_digest": descriptor.implementation_digest,
        "technology_digest": catalog.technology_digest,
        "technology_revision": catalog.technology().revision,
        "layout_policy": descriptor.provider.LAYOUT_POLICY,
    }
    key = json.dumps(recipe, sort_keys=True, allow_nan=False)
    if key not in _generated:
        rendered = descriptor.render(geometry)
        _generated[key] = GeneratedComponent(rendered.component, recipe)
    return _generated[key]


def generate_test_block():
    recipe = {"provider": "shapeforge", "generator": "test_block", "version": 1}
    key = json.dumps(recipe, sort_keys=True)
    if key not in _generated:
        gf.gpdk.PDK.activate()
        _generated[key] = GeneratedComponent(test_block(), recipe)
    return _generated[key]


def reconstruct_component(recipe):
    """Resolve only known providers; JSON never supplies Python or module paths."""
    if recipe["provider"] == "shapeforge":
        generated = generate_test_block()
    elif recipe["provider"] == "shapeic-cellkit":
        generated = generate_cellkit(recipe["primitive"], recipe["geometry"],
                                    recipe["pdk"], recipe["options"])
    elif recipe["provider"] == "external-python":
        from shapeforge.external import generate_external
        generated = generate_external(recipe, recipe["arguments"])
    elif recipe["provider"] == "shapeic-cellkit-technology":
        from shapeforge.technology import generate_technology
        generated = generate_technology(recipe["generator"], recipe["arguments"], recipe["pdk"])
    elif recipe["provider"] == "shapeforge-project":
        from shapeforge.project_links import generate_project
        generated = generate_project(recipe["source"], recipe["overrides"], recipe["pdk"])
    else:
        raise ValueError(f"Unknown component provider: {recipe['provider']}")
    warnings = []
    for key in ("implementation_digest", "technology_digest", "technology_revision",
                "layout_policy", "source_digest", "dependency_digest", "public_parameters"):
        if key in recipe and recipe[key] != generated.recipe.get(key):
            warnings.append(f"{recipe.get('primitive', recipe.get('function', recipe.get('generator', recipe.get('source', 'test_block'))))}: {key} differs from the saved recipe.")
    saved, current = recipe.get("versions", {}), generated.recipe.get("versions", {})
    for package in sorted(set(saved) | set(current)):
        if saved.get(package) != current.get(package):
            warnings.append(f"{recipe.get('primitive', recipe.get('function', recipe.get('generator', recipe.get('source', 'test_block'))))}: {package} "
                            f"saved={saved.get(package, 'unavailable')}, installed={current.get(package, 'unavailable')}.")
    return generated, warnings


def generate_inputs(recipe, values, parameters):
    """Generate a variant from provider-specific literals and safe bindings."""
    if recipe["provider"] == "shapeforge-project":
        from shapeforge.project_links import generate_project
        return generate_project(recipe["source"], values, recipe["pdk"], parameters)
    if recipe["provider"] == "external-python":
        from shapeforge.external import generate_external
        return generate_external(recipe, values, parameters)
    if recipe["provider"] == "shapeic-cellkit-technology":
        from shapeforge.technology import generate_technology
        return generate_technology(recipe["generator"], values, recipe["pdk"], parameters)
    return generate_cellkit(recipe["primitive"], resolve_geometry(values, parameters),
                           recipe["pdk"], recipe["options"])


def input_definition(values, provider):
    if provider == "shapeforge-project":
        return {"version": 4, "values": dict(values)}
    from shapeforge.dimensions import width_references
    if width_references(values):
        return {"version": 3, "values": dict(values)}
    if provider in ("external-python", "shapeic-cellkit-technology"):
        return {"version": 2, "values": dict(values)} if any(isinstance(v, dict) for v in values.values()) else None
    return {"version": 1, "values": dict(values)} if any(isinstance(v, str) for v in values.values()) else None


def validate_input_definition(definition, recipe, parameters):
    if not isinstance(definition, dict) or set(definition) != {"version", "values"}:
        raise ValueError("Invalid primitive input definition.")
    if recipe["provider"] == "shapeforge-project" and type(definition["version"]) is int and definition["version"] == 4:
        from shapeforge.project_links import resolve_overrides
        return resolve_overrides(definition["values"], parameters)
    if type(definition["version"]) is int and definition["version"] == 3:
        from shapeforge.dimensions import validate_port_width, is_port_width
        from shapeforge.technology import is_via_stack, resolve_inputs
        if not is_via_stack(recipe) or not isinstance(definition["values"], dict):
            raise ValueError("Port width input bindings are supported only for via_stack.")
        values = dict(definition["values"])
        for name, value in values.items():
            if is_port_width(value):
                if name not in ("width", "height"):
                    raise ValueError("Port width bindings require a via width or height.")
                validate_port_width(value)
                values[name] = 1.0  # Validate remaining fields without invoking a provider.
        return resolve_inputs(recipe["generator"], values, parameters, recipe["pdk"])
    if recipe["provider"] == "shapeic-cellkit-technology" and type(definition["version"]) is int and definition["version"] == 2:
        from shapeforge.technology import resolve_inputs
        return resolve_inputs(recipe["generator"], definition["values"], parameters, recipe["pdk"])
    if recipe["provider"] == "external-python" and type(definition["version"]) is int and definition["version"] == 2:
        from shapeforge.external import resolve_arguments
        return resolve_arguments(definition["values"], recipe["parameter_types"], parameters)
    if recipe["provider"] == "shapeic-cellkit" and type(definition["version"]) is int and definition["version"] == 1:
        return resolve_geometry(definition["values"], parameters)
    raise ValueError("Unsupported primitive input definition version or provider.")


def input_expressions(definition):
    for name, value in definition["values"].items():
        if definition["version"] in (2, 3):
            if isinstance(value, dict) and "expression" in value:
                yield name, value["expression"]
        else:
            yield name, value

def test_block() -> gf.Component:
    """Create a 10 × 5 µm block with two electrical ports."""
    c = gf.Component()

    layer = (1, 0)

    c.add_polygon(
        [(0, 0), (10, 0), (10, 5), (0, 5)],
        layer=layer,
    )

    c.add_port(
        name="left",
        center=(0, 2.5),
        width=5,
        orientation=180,
        layer=layer,
        port_type="electrical",
    )

    c.add_port(
        name="right",
        center=(10, 2.5),
        width=5,
        orientation=0,
        layer=layer,
        port_type="electrical",
    )

    return c

def simplediffpair() -> gf.Component:
    return generate_cellkit("simplediffpair", {
        "length_m": 0.4e-6, "finger_width_m": 5.0e-6, "nf": 4,
    }).component
