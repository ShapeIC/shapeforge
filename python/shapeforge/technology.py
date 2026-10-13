"""Explicit adapters for CellKit technology constructors, separate from helpers."""

import hashlib
import inspect
import json
from pathlib import Path
import sys
from types import ModuleType
import tomllib
import traceback

import gdsfactory as gf

from shapeforge.external import resolve_arguments

PROVIDER = "shapeic-cellkit-technology"
_constructors = {
    "ihp_mos_device": ("mos_core", "tech", "kind", "length", "wf", "nf"),
    "bussed_mos_device": ("gf", "mos_core", "tech", "kind", "length", "wf", "nf"),
    "interdigitated_mos_devices": ("gf", "cell_name", "mos_core", "tech", "kind", "length", "wf", "nf", "shared_gates"),
}
_modules = {}


def parameter_specs(generator, pdk="ihp-sg13g2"):
    if pdk != "ihp-sg13g2" or generator not in _constructors:
        raise ValueError(f"Unsupported technology constructor: {pdk} / {generator}.")
    specs = {
        "kind": {"type": "str", "default": "nmos", "choices": ["nmos", "pmos"]},
        "length": {"type": "float", "default": .4, "unit": "µm"},
        "wf": {"type": "float", "default": 5., "unit": "µm"},
        "nf": {"type": "int", "default": 4},
    }
    if generator == "interdigitated_mos_devices":
        specs["shared_gates"] = {"type": "bool", "default": False}
    return specs


def resolve_inputs(generator, values, parameters, pdk="ihp-sg13g2", defaults=False):
    specs = parameter_specs(generator, pdk)
    if defaults:
        values = {**{name: spec["default"] for name, spec in specs.items()}, **values}
    arguments = resolve_arguments(values, {name: spec["type"] for name, spec in specs.items()}, parameters)
    if arguments["kind"] not in ("nmos", "pmos"):
        raise ValueError("Technology MOS kind must be nmos or pmos.")
    if arguments["length"] <= 0 or arguments["wf"] <= 0 or arguments["nf"] <= 0:
        raise ValueError("Technology MOS length, wf and nf must be positive.")
    return arguments


def _module(catalog):
    path = catalog.root / "technologies" / catalog.pdk / "pcells.py"
    if not path.is_file():
        raise ValueError(f"CellKit technology PCells are missing: {path}.")
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    key = (str(path.resolve()), digest)
    if key not in _modules:
        name = "sftech_" + hashlib.sha256(repr(key).encode()).hexdigest()[:20]
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(source, str(path), "exec"), module.__dict__)
        except Exception:
            sys.modules.pop(name, None)
            raise
        _modules[key] = module
    return _modules[key], digest


def _function(module, generator):
    function = getattr(module, "_" + generator, None)
    expected = _constructors[generator]
    if not callable(function):
        raise ValueError(f"CellKit is missing technology constructor _{generator}.")
    signature = inspect.signature(function)
    if tuple(signature.parameters) != expected or any(
        p.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for p in signature.parameters.values()
    ):
        raise ValueError(f"Incompatible CellKit signature for _{generator}; expected {', '.join(expected)}.")
    if not callable(getattr(module, "_backend", None)) or not callable(getattr(module, "_cell_name", None)):
        raise ValueError("CellKit technology requires _backend() and _cell_name().")
    return function


def available_technology_pcells(catalog):
    if getattr(catalog, "pdk", None) != "ihp-sg13g2":
        return {}
    module, _ = _module(catalog)
    for generator in _constructors:
        _function(module, generator)
    return {"Technology — " + name: name for name in _constructors}


def generate_technology(generator, values, pdk="ihp-sg13g2", parameters=None):
    from shapeforge.components import GeneratedComponent, _generated, cellkit_catalog, dependency_versions
    arguments = resolve_inputs(generator, values, parameters or {}, pdk, defaults=True)
    catalog = cellkit_catalog(pdk)
    module, digest = _module(catalog)
    function = _function(module, generator)
    versions = dependency_versions()
    metadata = catalog.root / "pyproject.toml"
    if "shapeic-cellkit" not in versions:
        versions["shapeic-cellkit"] = (tomllib.loads(metadata.read_text())["project"]["version"]
                                       if metadata.is_file() else "source-checkout (version unavailable)")
    recipe = {"provider": PROVIDER, "pdk": pdk, "generator": generator,
              "arguments": arguments, "parameter_types": {name: spec["type"] for name, spec in parameter_specs(generator, pdk).items()},
              "versions": versions, "implementation_digest": digest,
              "technology_digest": catalog.technology_digest, "technology_revision": catalog.technology().revision,
              "layout_policy": module.LAYOUT_POLICY}
    key = json.dumps(recipe, sort_keys=True, allow_nan=False)
    if key not in _generated:
        prefix = "SFTECH_" + hashlib.sha256(key.encode()).hexdigest()[:20]
        original_name, previous_pdk = module._cell_name, gf.pdk._ACTIVE_PDK
        previous_cells = {cell.cell_index() for cell in gf.kcl.layout.each_cell()}
        # This is a dedicated module, never the module used by CellKit primitives.
        # Prefix names at creation time, before a fixed name can collide.
        module._cell_name = lambda *args: prefix + "_" + original_name(*args)
        try:
            backend = module._backend()
            if not isinstance(backend, tuple) or len(backend) != 4:
                raise ValueError("Incompatible IHP _backend(); expected gf, cells, mos_core, tech.")
            backend_gf, _, mos_core, tech = backend
            hidden = {"gf": backend_gf, "mos_core": mos_core, "tech": tech, "cell_name": prefix}
            call = {name: arguments[name] if name in arguments else hidden[name] for name in _constructors[generator]}
            component = function(**call)
            if not isinstance(component, gf.Component):
                raise ValueError("Technology constructor must return a gdsfactory.Component.")
            _generated[key] = GeneratedComponent(component, recipe)
        except Exception as error:
            frames = traceback.extract_tb(error.__traceback__)
            local = [frame for frame in frames if frame.filename == module.__file__]
            location = f"pcells.py:{local[-1].lineno}" if local else "IHP backend"
            raise ValueError(f"CellKit technology {pdk} / {generator} failed at {location}: {type(error).__name__}: {error}") from error
        finally:
            module._cell_name = original_name
            gf.pdk._ACTIVE_PDK = previous_pdk
            if key not in _generated:
                for cell in list(gf.kcl.layout.each_cell()):
                    if cell.cell_index() not in previous_cells and not cell.is_locked():
                        cell.name = gf.kcl.layout.unique_cell_name(f"FAILED_{prefix}_{cell.cell_index()}_{cell.name}")
    return _generated[key]
