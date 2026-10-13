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
        elif isinstance(node, ast.Call):
            if (not isinstance(node.func, ast.Name) or node.func.id != "floor"
                    or len(node.args) != 1 or node.keywords):
                raise ValueError("Only floor(expression) with one positional argument is supported.")
        elif not isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
                                   ast.UAdd, ast.USub, ast.Load)):
            raise ValueError("Only numbers, parameter names, +, -, *, /, floor(expression) and parentheses are supported.")
    return tree


def references(value):
    nodes = list(ast.walk(expression_tree(value)))
    functions = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    return {node.id for node in nodes if isinstance(node, ast.Name) and id(node) not in functions}


def _evaluate(node, lookup):
    if isinstance(node, ast.Constant):
        return _finite(node.value)
    if isinstance(node, ast.Name):
        return lookup(node.id)
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


def resolve_parameters(definitions):
    if not isinstance(definitions, dict):
        raise ValueError("parameters must be an object.")
    trees = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f"Invalid parameter name: {name!r}.")
        trees[name] = expression_tree(definition)
    values, path = {}, []
    def lookup(name):
        if name in values:
            return values[name]
        if name not in trees:
            raise ValueError(f"Unknown parameter: {name}")
        if name in path:
            raise ValueError("Parameter cycle detected: " + " -> ".join(path[path.index(name):] + [name]))
        if len(path) >= 64:
            raise ValueError("Parameter dependency chains must contain fewer than 64 levels.")
        path.append(name)
        try:
            values[name] = _evaluate(trees[name], lookup)
        finally:
            path.pop()
        return values[name]
    for name in sorted(trees):
        lookup(name)
    return values


def resolve_value(value, definitions):
    values = resolve_parameters(definitions)
    def lookup(name):
        if name not in values:
            raise ValueError(f"Unknown parameter: {name}")
        return values[name]
    return _evaluate(expression_tree(value), lookup)


def parameter_definition(value):
    """Keep formulas as text and normalize numeric input for JSON storage."""
    if isinstance(value, str):
        value = value.strip()
        try:
            return _finite(float(value))
        except ValueError:
            expression_tree(value)
            return value
    return _finite(value)
