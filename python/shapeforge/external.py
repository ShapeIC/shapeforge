"""Trusted local Python libraries; projects contain references, never Python code."""

import ast
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import inspect
from functools import wraps
import json
import math
from pathlib import Path
import sys
import traceback
from types import ModuleType

import gdsfactory as gf

from shapeforge.parameters import resolve_value

CONFIG_KEY = "shapeforge.external_libraries"
_libraries = {}
_modules = {}
_module_pdks = {}
_types = {"float": float, "int": int, "bool": bool, "str": str}


class ExternalGeneratorError(ValueError):
    """A trusted generator failed; retain its original exception as the cause."""


def configure_libraries(libraries):
    if not isinstance(libraries, dict):
        raise ValueError("External libraries configuration must be an object.")
    checked = {}
    for name, info in libraries.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("External library names must be Python identifiers.")
        if not isinstance(info, dict) or set(info) != {"root", "pdk"}:
            raise ValueError(f"Library {name}: expected root and pdk.")
        if not isinstance(info["root"], str) or not isinstance(info["pdk"], str) or not info["pdk"]:
            raise ValueError(f"Library {name}: root and pdk must be strings.")
        checked[name] = dict(info)
    _libraries.clear()
    _libraries.update(checked)


def libraries():
    return {name: dict(info) for name, info in _libraries.items()}


def register_library(name, root, pdk):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"External library directory is missing: {root}.")
    configure_libraries({**libraries(), name: {"root": str(root), "pdk": pdk}})


def library_root(name, pdk=None):
    if name not in _libraries:
        raise ValueError(f"External library {name!r} is not registered. Use External Libraries… to locate it.")
    info = _libraries[name]
    if pdk is not None and info["pdk"] != pdk:
        raise ValueError(f"Library {name} uses PDK {info['pdk']}, but the project uses {pdk}.")
    root = Path(info["root"]).resolve()
    if not root.is_dir():
        raise ValueError(f"External library {name}: directory is missing; register its new location.")
    return root


def source_path(root, file):
    if not isinstance(file, str) or Path(file).is_absolute() or ".." in Path(file).parts:
        raise ValueError("External source must be a relative Python file inside its library.")
    path = (root / file).resolve()
    if not path.is_relative_to(root) or path.suffix != ".py" or not path.is_file():
        raise ValueError(f"External source is missing or outside its library: {file}.")
    return path


def source_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source_path(root, path.relative_to(root).as_posix())
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def available_generators(name):
    """Discover public top-level functions without importing every script."""
    root = library_root(name)
    result = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        file = path.relative_to(root).as_posix()
        source_path(root, file)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=file)
        names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                 and not node.name.startswith("_")]
        if names:
            result[file] = names
    return result


def _local_module(module, root):
    file = getattr(module, "__file__", None)
    return file is not None and Path(file).resolve().is_relative_to(root)


@contextmanager
def generator_context(recipe):
    function_name = recipe["function"]
    if not isinstance(function_name, str) or not function_name.isidentifier() or function_name.startswith("_"):
        raise ValueError("External generators must be public function identifiers.")
    root = library_root(recipe["library"], recipe["pdk"])
    path = source_path(root, recipe["file"])
    digest = source_digest(root)
    key = (str(root), digest, recipe["file"], recipe["pdk"])
    # Absolute sibling imports (e.g. `from utils import ...`) belong to the
    # library during loading AND generation. Restore pre-existing modules after
    # the operation, including failures. Cached modules retain decorator caches.
    local_names = {p.stem for base in (root, path.parent) for p in base.glob("*.py")}
    local_names |= {p.name for base in (root, path.parent) for p in base.iterdir() if p.is_dir()}
    saved = {name: module for name, module in sys.modules.items()
             if name.split(".")[0] in local_names or _local_module(module, root)}
    old_path = sys.path[:]
    old_bytecode = sys.dont_write_bytecode
    old_pdk = gf.pdk._ACTIVE_PDK
    original_cell = gf.cell
    previous = {cell.cell_index() for cell in gf.kcl.layout.each_cell()}
    prefix = "sfext_" + hashlib.sha256(repr(key).encode()).hexdigest()[:16]
    scope = _modules.setdefault(key, {})
    @wraps(original_cell)
    def scoped_cell(function=None, **options):
        def decorate(func):
            source = inspect.getsourcefile(func)
            if source and Path(source).resolve().is_relative_to(root):
                relative = Path(source).resolve().relative_to(root).as_posix()
                tag = hashlib.sha256((str(root) + recipe["pdk"] + digest + relative).encode()).hexdigest()[:16]
                options_local = {**options, "basename": f"sfext_{tag}_{options.get('basename') or func.__name__}"}
                options_local.setdefault("register_factory", False)
                return original_cell(**options_local)(func)
            return original_cell(**options)(func)
        return decorate(function) if function is not None else decorate
    try:
        gf.cell = scoped_cell
        for name in saved:
            sys.modules.pop(name, None)
        sys.modules.update(scope)
        sys.path[:0] = [str(path.parent), str(root)]
        sys.dont_write_bytecode = True
        if prefix not in sys.modules:
            init = root / "__init__.py"
            if init.is_file():
                source_path(root, "__init__.py")
                package_spec = importlib.util.spec_from_file_location(prefix, init)
                package = importlib.util.module_from_spec(package_spec)
            else:
                package = ModuleType(prefix)
                package.__path__ = [str(root)]
                package.__package__ = prefix
                package.__file__ = str(init)
                package.__spec__ = importlib.util.spec_from_loader(prefix, loader=None, is_package=True)
            sys.modules[prefix] = package
            if init.is_file():
                try:
                    exec(compile(init.read_bytes(), str(init), "exec"), package.__dict__)
                except Exception:
                    sys.modules.pop(prefix, None)
                    raise
            scope[prefix] = package
            _module_pdks[key] = gf.pdk._ACTIVE_PDK
        relative = path.relative_to(root).with_suffix("").parts
        if relative[-1] == "__init__":
            relative = relative[:-1]
        module_name = prefix + "." + ".".join(relative)
        module_name = module_name.rstrip(".")
        parent = module_name.rpartition(".")[0]
        if parent:
            importlib.import_module(parent)
        if module_name in sys.modules and module_name not in scope:
            scope[module_name] = sys.modules[module_name]
        if module_name not in scope:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            # Read source directly: timestamp-based .pyc caches can miss edits
            # made in the same second and imports must not write into libraries.
            try:
                exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            scope[module_name] = module
            _module_pdks[key] = gf.pdk._ACTIVE_PDK
        elif _module_pdks.get(key) is not None:
            gf.pdk._ACTIVE_PDK = _module_pdks[key]
        module = scope[module_name]
        function = getattr(module, recipe["function"], None)
        if not callable(function) or getattr(function, "__module__", None) != module_name:
            raise ValueError(f"No public generator {recipe['function']!r} defined in {recipe['file']}.")
        yield function, digest
    finally:
        # Release fixed names of newly created, unlocked external cells. Never
        # rename shared locked PDK cells or cells that existed before this call.
        try:
            for cell in list(gf.kcl.layout.each_cell()):
                if cell.cell_index() not in previous and not cell.is_locked():
                    cell.name = gf.kcl.layout.unique_cell_name(f"{prefix}_{cell.cell_index()}_{cell.name}")
        finally:
            for name, module in list(sys.modules.items()):
                if _local_module(module, root):
                    scope[name] = module
                    sys.modules.pop(name, None)
                elif name.split(".")[0] in local_names and name not in saved:
                    sys.modules.pop(name, None)
            sys.modules.update(saved)
            sys.path[:] = old_path
            sys.dont_write_bytecode = old_bytecode
            gf.pdk._ACTIVE_PDK = old_pdk
            gf.cell = original_cell


def parameter_specs(function):
    specs = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise ValueError(f"Argument {name}: only fixed keyword-callable parameters are supported.")
        annotation = parameter.annotation
        kind = _types.get(annotation) if isinstance(annotation, str) else annotation
        if kind not in _types.values():
            raise ValueError(f"Argument {name}: an explicit float, int, bool or str annotation is required.")
        specs[name] = {"type": kind.__name__, "default": parameter.default}
        if parameter.default is not inspect.Parameter.empty:
            _literal(parameter.default, kind.__name__, name)
    return specs


def generator_specs(recipe):
    with generator_context(recipe) as (function, _):
        return parameter_specs(function)


def _literal(value, kind, name):
    if kind == "float":
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError(f"Argument {name}: expected a finite float.")
        return float(format(value, ".15g"))
    if kind == "int" and type(value) is not int:
        raise ValueError(f"Argument {name}: expected an integer.")
    if kind in ("bool", "str") and type(value) is not _types[kind]:
        raise ValueError(f"Argument {name}: expected {kind}.")
    return value


def resolve_arguments(values, types, parameters):
    if not isinstance(values, dict) or set(values) != set(types):
        raise ValueError("External arguments do not match the generator's parameters.")
    result = {}
    for name, kind in types.items():
        if kind not in _types:
            raise ValueError(f"Argument {name}: unsupported type {kind!r}.")
        value = values[name]
        if isinstance(value, dict):
            if set(value) != {"expression"} or not isinstance(value["expression"], str) or kind not in ("float", "int"):
                raise ValueError(f"Argument {name}: only numeric parameters accept expressions.")
            value = resolve_value(value["expression"], parameters)
            if kind == "int":
                if not value.is_integer():
                    raise ValueError(f"Argument {name}: expression must evaluate to an integer.")
                value = int(value)
        result[name] = _literal(value, kind, name)
    return result


def generate_external(reference, values, parameters=None):
    from shapeforge.components import GeneratedComponent, _generated, dependency_versions
    with generator_context(reference) as (function, digest):
        specs = parameter_specs(function)
        types = {name: spec["type"] for name, spec in specs.items()}
        effective = dict(values)
        for name, spec in specs.items():
            if name not in effective and spec["default"] is not inspect.Parameter.empty:
                effective[name] = spec["default"]
        arguments = resolve_arguments(effective, types, parameters or {})
        recipe = {"provider": "external-python", "library": reference["library"],
                  "file": reference["file"], "function": reference["function"], "pdk": reference["pdk"],
                  "arguments": arguments, "parameter_types": types,
                  "implementation_digest": digest, "versions": dependency_versions()}
        key = json.dumps(recipe, sort_keys=True, allow_nan=False)
        if key not in _generated:
            try:
                component = function(**arguments)
            except Exception as error:
                root = library_root(reference["library"], reference["pdk"])
                frames = traceback.extract_tb(error.__traceback__)
                local = [frame for frame in frames if Path(frame.filename).resolve().is_relative_to(root)]
                frame = local[-1] if local else frames[-1]
                file = Path(frame.filename).resolve()
                location = file.relative_to(root).as_posix() if file.is_relative_to(root) else file.name
                raise ExternalGeneratorError(
                    f"External PCell {reference['library']} / {reference['function']} failed:\n"
                    f"{location}:{frame.lineno} ({frame.name})\n"
                    f"{type(error).__name__}: {error}"
                ) from error
            if not isinstance(component, gf.Component):
                raise ValueError(f"Generator {reference['function']} must return a gdsfactory.Component.")
            _generated[key] = GeneratedComponent(component, recipe)
        return _generated[key]
