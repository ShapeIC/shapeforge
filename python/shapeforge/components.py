import gdsfactory as gf
import os
from pathlib import Path
from dataclasses import asdict, dataclass, fields
from importlib.metadata import PackageNotFoundError, version
import json
import math
import tomllib
from typing import get_type_hints


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
    else:
        raise ValueError(f"Unknown component provider: {recipe['provider']}")
    warnings = []
    for key in ("implementation_digest", "technology_digest", "technology_revision",
                "layout_policy"):
        if key in recipe and recipe[key] != generated.recipe.get(key):
            warnings.append(f"{recipe.get('primitive', 'test_block')}: {key} differs from the saved recipe.")
    saved, current = recipe.get("versions", {}), generated.recipe.get("versions", {})
    for package in sorted(set(saved) | set(current)):
        if saved.get(package) != current.get(package):
            warnings.append(f"{recipe.get('primitive', 'test_block')}: {package} "
                            f"saved={saved.get(package, 'unavailable')}, installed={current.get(package, 'unavailable')}.")
    return generated, warnings

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
