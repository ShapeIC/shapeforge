"""Dependency-aware numeric memoization, keyed by current native snapshots."""

from shapeforge import runtime


class EvaluationError(ValueError, RuntimeError):
    pass


class DependencyCycle(EvaluationError):
    pass


class Memo:
    def __init__(self, previous, intrinsic):
        self.entries = dict(previous)
        self.intrinsic = intrinsic
        self.values, self.validity, self.dependencies = {}, {}, {}
        self.path, self.observed = [], set()
        self.serial = max((entry["serial"] for entry in previous.values()), default=0)

    def valid(self, key):
        if key in self.validity:
            return self.validity[key]
        entry = self.entries.get(key)
        self.validity[key] = False
        if entry is None or entry["input"] != self.intrinsic(key):
            return False
        if any(not self.valid(dep) or self.entries[dep]["serial"] != serial
               for dep, serial in entry["deps"].items()):
            return False
        self.validity[key] = True
        return True

    def observe(self, key):
        if key in self.observed:
            return
        self.observed.add(key)
        for dep in self.entries.get(key, {}).get("deps", {}):
            self.observe(dep)

    def call(self, key, compute):
        if self.path:
            self.dependencies.setdefault(self.path[-1], set()).add(key)
        if key in self.values:
            return self.values[key]
        if key in self.path:
            cycle = self.path[self.path.index(key):] + [key]
            raise DependencyCycle("Geometry dependency cycle detected (generation cycle): " + " → ".join(": ".join(k) for k in cycle))
        if len(self.path) >= 128:
            raise ValueError("Geometry dependency chain is too deep.")
        if self.valid(key):
            runtime.count("nodes_reused")
            self.values[key] = self.entries[key]["value"]
            self.observe(key)
            return self.values[key]
        self.path.append(key)
        self.dependencies[key] = set()
        try:
            result = compute()
            self.serial += 1
            self.entries[key] = {"input": self.intrinsic(key), "value": result, "serial": self.serial,
                                 "deps": {dep: self.entries[dep]["serial"] for dep in self.dependencies[key]}}
            self.values[key] = result
            self.validity[key] = True
            self.observed.add(key)
            runtime.count("nodes_computed")
            runtime.count("computed_" + key[0].replace(" ", "_"))
            return result
        finally:
            self.path.pop()


class Components(dict):
    """Carry the validated plan to application without storing native handles."""
    evaluation = None


class Updates(list):
    evaluation = None
