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
from shapeforge import runtime

PROVIDER = "shapeic-cellkit-technology"
_constructors = {
    "ihp_mos_device": ("mos_core", "tech", "kind", "length", "wf", "nf"),
    "bussed_mos_device": ("gf", "mos_core", "tech", "kind", "length", "wf", "nf"),
    "interdigitated_mos_devices": ("gf", "cell_name", "mos_core", "tech", "kind", "length", "wf", "nf", "shared_gates"),
    "via_stack": ("c", "tech", "column_width", "row_width", "center", "bottom_layer", "top_layer"),
    "project_port": ("component", "pinName", "center", "width", "length", "orientation", "pinLayer", "pinTextLayer"),
}
_helpers = {"via_stack": "_populate_via_stack", "project_port": "_add_port"}


def is_via_stack(recipe):
    return bool(recipe and recipe.get("provider") == "shapeic-cellkit-technology"
                and recipe.get("generator") == "via_stack")
METALS = ["Metal1", "Metal2", "Metal3", "Metal4", "Metal5", "TopMetal1", "TopMetal2"]
_modules = {}


def parameter_specs(generator, pdk="ihp-sg13g2"):
    if pdk != "ihp-sg13g2" or generator not in _constructors:
        raise ValueError(f"Unsupported technology constructor: {pdk} / {generator}.")
    if generator == "via_stack":
        layers = ["Activ", "GatPoly"] + METALS
        return {"width": {"type": "float", "default": 10., "unit": "µm"},
                "height": {"type": "float", "default": 10., "unit": "µm"},
                "bottom_layer": {"type": "str", "default": "Metal1", "choices": layers},
                "top_layer": {"type": "str", "default": "Metal2", "choices": layers}}
    if generator == "project_port":
        return {"pinName": {"type": "str", "default": "port1"},
                "width": {"type": "float", "default": 1., "unit": "µm"},
                "length": {"type": "float", "default": 1., "unit": "µm"},
                "pinLayer": {"type": "str", "default": "Metal1pin", "choices": [metal + "pin" for metal in METALS]},
                "pinTextLayer": {"type": "str", "default": "Metal1text", "choices": [""] + [metal + "text" for metal in METALS],
                                 "choice_labels": {"": "None (no label)"}}}
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
    for name, spec in specs.items():
        if "choices" in spec and arguments[name] not in spec["choices"]:
            raise ValueError(f"Unsupported {name}: {arguments[name]!r}.")
    if generator in ("via_stack", "project_port"):
        dimensions = ("width", "height") if generator == "via_stack" else ("width", "length")
        if any(arguments[name] <= 0 for name in dimensions):
            raise ValueError("Via and pin dimensions must be positive.")
        if generator == "project_port" and not arguments["pinName"].strip():
            raise ValueError("Project port name must not be empty.")
        if generator == "via_stack":
            order = ["Activ", "GatPoly"] + METALS
            lower, upper = arguments["bottom_layer"], arguments["top_layer"]
            rank = lambda layer: 0 if layer in ("Activ", "GatPoly") else order.index(layer) - 1
            if rank(lower) >= rank(upper):
                raise ValueError("Via stack top layer must be above its bottom layer.")
        return arguments
    if arguments["kind"] not in ("nmos", "pmos"):
        raise ValueError("Technology MOS kind must be nmos or pmos.")
    if arguments["length"] <= 0 or arguments["wf"] <= 0 or arguments["nf"] <= 0:
        raise ValueError("Technology MOS length, wf and nf must be positive.")
    return arguments


def _module(catalog):
    return runtime.loaded(("technology", str(catalog.root), catalog.pdk), lambda: _load_module(catalog))


def _load_module(catalog):
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
    source_name = _helpers.get(generator, "_" + generator)
    function = getattr(module, source_name, None)
    expected = _constructors[generator]
    if not callable(function):
        raise ValueError(f"CellKit is missing technology constructor {source_name}.")
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
    return {"Technology — " + name: name for name in _constructors if name != "project_port"}


@runtime.provider
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
    key = runtime.cache_key(recipe, (str(catalog.root),))
    if key not in _generated:
        runtime.count("generator_executions")
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
            if generator in ("via_stack", "project_port"):
                component = backend_gf.Component(prefix)
                if generator == "via_stack":
                    pitch = tech.via1_size_rf + tech.via1_spacing_wide
                    if any(int((arguments[axis] - tech.via1_enc + tech.via1_spacing_wide) / pitch) < 1 for axis in ("width", "height")):
                        raise ValueError("Via dimensions are too small for at least one via row and column.")
                    ref = function(component, tech, column_width=arguments["height"], row_width=arguments["width"],
                                   center=(0, 0), bottom_layer=arguments["bottom_layer"], top_layer=arguments["top_layer"])
                    for port in ref.ports:
                        component.add_port(name=port.name, port=port)
                else:
                    function(component, arguments["pinName"], (0, 0), arguments["width"], arguments["length"],
                             orientation=0, pinLayer=arguments["pinLayer"], pinTextLayer=arguments["pinTextLayer"] or None)
                    if len(component.ports) != 1 or component.ports[arguments["pinName"]].orientation != 0:
                        raise ValueError("CellKit must create exactly one local project port with orientation 0.")
                    if component.dbbox().width() <= 0 or component.dbbox().height() <= 0:
                        raise ValueError("Pin dimensions collapse at the backend DBU.")
            else:
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
