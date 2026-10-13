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
    else:
        raise ValueError(f"Unknown component provider: {recipe['provider']}")
    warnings = []
    for key in ("implementation_digest", "technology_digest", "technology_revision",
                "layout_policy"):
        if key in recipe and recipe[key] != generated.recipe.get(key):
            warnings.append(f"{recipe.get('primitive', recipe.get('function', 'test_block'))}: {key} differs from the saved recipe.")
    saved, current = recipe.get("versions", {}), generated.recipe.get("versions", {})
    for package in sorted(set(saved) | set(current)):
        if saved.get(package) != current.get(package):
            warnings.append(f"{recipe.get('primitive', recipe.get('function', 'test_block'))}: {package} "
                            f"saved={saved.get(package, 'unavailable')}, installed={current.get(package, 'unavailable')}.")
    return generated, warnings


def generate_inputs(recipe, values, parameters):
    """Generate a variant from provider-specific literals and safe bindings."""
    if recipe["provider"] == "external-python":
        from shapeforge.external import generate_external
        return generate_external(recipe, values, parameters)
    return generate_cellkit(recipe["primitive"], resolve_geometry(values, parameters),
                           recipe["pdk"], recipe["options"])


def input_definition(values, provider):
    if provider == "external-python":
        return {"version": 2, "values": dict(values)} if any(isinstance(v, dict) for v in values.values()) else None
    return {"version": 1, "values": dict(values)} if any(isinstance(v, str) for v in values.values()) else None


def validate_input_definition(definition, recipe, parameters):
    if not isinstance(definition, dict) or set(definition) != {"version", "values"}:
        raise ValueError("Invalid primitive input definition.")
    if recipe["provider"] == "external-python" and type(definition["version"]) is int and definition["version"] == 2:
        from shapeforge.external import resolve_arguments
        return resolve_arguments(definition["values"], recipe["parameter_types"], parameters)
    if recipe["provider"] == "shapeic-cellkit" and type(definition["version"]) is int and definition["version"] == 1:
        return resolve_geometry(definition["values"], parameters)
    raise ValueError("Unsupported primitive input definition version or provider.")


def input_expressions(definition):
    for name, value in definition["values"].items():
        if definition["version"] == 2:
            if isinstance(value, dict):
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
