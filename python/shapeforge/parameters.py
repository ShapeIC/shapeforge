"""Small arithmetic language for parameters; no Python code is executed."""

import ast
import keyword
import math


def _finite(value):
    if type(value) not in (int, float):
        raise ValueError("Expected a finite number or an arithmetic expression.")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("The parameter result must be finite.") from error
    if not math.isfinite(result):
        raise ValueError("The parameter result must be finite.")
    return result


def expression_tree(value):
    if not isinstance(value, str):
        return ast.Constant(_finite(value))
    if len(value) > 1000:
        raise ValueError("Parameter expressions must be at most 1000 characters.")
    try:
        tree = ast.parse(value.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ValueError("Invalid expression. Use numbers, parameter names, +, -, *, /, floor(expression) and parentheses.") from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 128:
        raise ValueError("The parameter expression is too complex.")
    for node in nodes:
        if isinstance(node, ast.Constant):
            _finite(node.value)
        elif isinstance(node, ast.Name):
            pass
        elif isinstance(node, ast.Attribute):
            if (not isinstance(node.value, ast.Name) or node.value.id.startswith("_")
                    or keyword.iskeyword(node.attr)):
                raise ValueError("Use only instance.parameter references to direct children.")
        elif isinstance(node, ast.Call):
            if (not isinstance(node.func, ast.Name) or node.func.id != "floor"
                    or len(node.args) != 1 or node.keywords):
                raise ValueError("Only floor(expression) with one positional argument is supported.")
        elif not isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
                                   ast.UAdd, ast.USub, ast.Load)):
            raise ValueError("Only numbers, parameter names, +, -, *, /, floor(expression) and parentheses are supported.")
    return tree


def references(value):
    if is_measure(value):
        validate_measure(value)
        return set()
    nodes = list(ast.walk(expression_tree(value)))
    excluded = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    excluded.update(id(node.value) for node in nodes if isinstance(node, ast.Attribute))
    return ({node.id for node in nodes if isinstance(node, ast.Name) and id(node) not in excluded}
            | {f"{node.value.id}.{node.attr}" for node in nodes if isinstance(node, ast.Attribute)})


def _evaluate(node, lookup):
    if isinstance(node, ast.Constant):
        return _finite(node.value)
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if isinstance(node, ast.Attribute):
        return lookup(f"{node.value.id}.{node.attr}")
    if isinstance(node, ast.Call):
        return _finite(math.floor(_evaluate(node.args[0], lookup)))
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, lookup)
        return _finite(-value if isinstance(node.op, ast.USub) else value)
    left, right = _evaluate(node.left, lookup), _evaluate(node.right, lookup)
    if isinstance(node.op, ast.Add):
        return _finite(left + right)
    if isinstance(node.op, ast.Sub):
        return _finite(left - right)
    if isinstance(node.op, ast.Div):
        if right == 0:
            raise ValueError("Division by zero in parameter expression.")
        return _finite(left / right)
    return _finite(left * right)


def is_measure(value):
    return isinstance(value, dict) and value.get("kind") == "geometry_measure"


def validate_measure(value):
    keys = {"version", "kind", "reference_id", "quantity"}
    if isinstance(value, dict) and value.get("quantity") == "port_width":
        keys.add("port")
    if (not isinstance(value, dict) or set(value) != keys or value.get("kind") != "geometry_measure"
            or type(value.get("version")) is not int or value["version"] != 1
            or value.get("quantity") not in ("width", "height", "port_width")
            or not isinstance(value.get("reference_id"), str) or not value["reference_id"]
            or "port" in keys and (not isinstance(value.get("port"), str) or not value["port"])):
        raise ValueError("Invalid geometry measurement: specify version, reference ID, quantity and port for port_width.")


def validate_definitions(definitions):
    if not isinstance(definitions, dict):
        raise ValueError("parameters must be an object.")
    for name, definition in definitions.items():
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f"Invalid parameter name: {name!r}.")
        validate_measure(definition) if is_measure(definition) else expression_tree(definition)
    done, path = set(), []
    def visit(name):
        if "." in name:
            return  # Resolved by the instance context, never a Python attribute.
        if name not in definitions:
            raise ValueError(f"Unknown parameter: {name}")
        if name in path:
            raise ValueError("Parameter cycle detected: " + " -> ".join(path + [name]))
        if name in done:
            return
        if len(path) >= 64:
            raise ValueError("Parameter dependency chains must contain fewer than 64 levels.")
        path.append(name)
        for source in references(definitions[name]):
            visit(source)
        path.pop()
        done.add(name)
    for name in definitions:
        visit(name)


def derived_parameters(definitions):
    validate_definitions(definitions)
    derived = {name for name, value in definitions.items()
               if is_measure(value) or any("." in ref for ref in references(value))}
    while True:
        extended = derived | {name for name, value in definitions.items() if references(value) & derived}
        if extended == derived:
            return derived
        derived = extended


class ParameterValues(dict):
    """Serializable definitions with an operation-local, lazy value resolver."""
    def __init__(self, definitions, measurement=None, lookup=None, structural=False, internal=None):
        super().__init__(definitions)
        self.measurement, self.lookup, self.structural = measurement, lookup, structural
        self.internal = internal


def _lookup(definitions):
    if isinstance(definitions, ParameterValues) and definitions.lookup is not None:
        return definitions.lookup
    values, path = {}, []
    def lookup(name):
        if name in values:
            return values[name]
        if "." in name:
            callback = getattr(definitions, "internal", None)
            if callback is None:
                raise ValueError(f"Parameter {name} requires a linked instance context.")
            return _finite(callback(name))
        if name not in definitions:
            raise ValueError(f"Unknown parameter: {name}")
        if name in path:
            raise ValueError("Parameter cycle detected: " + " -> ".join(path + [name]))
        if len(path) >= 64:
            raise ValueError("Parameter dependency chains must contain fewer than 64 levels.")
        path.append(name)
        try:
            definition = definitions[name]
            if is_measure(definition):
                validate_measure(definition)
                callback = getattr(definitions, "measurement", None)
                if callback is None:
                    raise ValueError(f"Parameter {name} requires a geometry measurement context.")
                result = callback(definition)
            else:
                result = _evaluate(expression_tree(definition), lookup)
            values[name] = _finite(result)
            return values[name]
        finally:
            path.pop()
    return lookup


def resolve_parameters(definitions):
    validate_definitions(definitions)
    lookup = _lookup(definitions)
    return {name: lookup(name) for name in sorted(definitions)}


def resolve_value(value, definitions):
    # Structural project validation cannot know future geometry. Validate syntax
    # and names now; numerical checks run on the fully reconstructed staged plan.
    if getattr(definitions, "structural", False):
        names = references(value)
        missing = {name for name in names if "." not in name} - definitions.keys()
        if missing:
            raise ValueError(f"Unknown parameter: {sorted(missing)[0]}")
        for name in names:
            if "." in name:
                _lookup(definitions)(name)
        if any("." in name for name in names) or names & derived_parameters(definitions):
            return 1.0
    return _evaluate(expression_tree(value), _lookup(definitions))


def parameter_definition(value):
    """Keep formulas as text and normalize numeric input for JSON storage."""
    if is_measure(value):
        validate_measure(value)
        return dict(value)
    if isinstance(value, str):
        value = value.strip()
        try:
            return _finite(float(value))
        except ValueError:
            expression_tree(value)
            return value
    return _finite(value)


def structural_parameters(definitions, namespaces=()):
    """Syntax/name validation scope; geometry-dependent numeric checks are staged."""
    derived = derived_parameters(definitions)
    ordinary = _lookup({name: 1.0 if name in derived else value for name, value in definitions.items()})
    def internal(name):
        if name.split(".")[0] not in namespaces:
            raise ValueError(f"Unknown linked instance: {name.split('.')[0]}")
        return 1.0
    def lookup(name):
        return internal(name) if "." in name else ordinary(name)
    for value in definitions.values():
        for name in references(value):
            if "." in name:
                internal(name)
    return ParameterValues(definitions, lookup=lookup, structural=True)


def describe_definition(definition):
    if not is_measure(definition):
        return str(definition)
    label = {"width": "Width", "height": "Height", "port_width": "Port Width"}[definition["quantity"]]
    return f"{label} · {definition['reference_id']}" + (f" / {definition['port']}" if "port" in definition else "")
